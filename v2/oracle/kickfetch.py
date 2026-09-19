#!/usr/bin/env python3
"""Fetch one part of a Kick VOD straight onto this server. From cron.

    python3 kickfetch.py            say what it would fetch
    python3 kickfetch.py --apply    fetch one part into the queue

It answers the same want.json the board answers, so the two supply lines cannot
overrun the share between them: whatever this takes, the next supply.py run
subtracts from what the board is offered.

Nothing is encoded. The rendition Kick already serves is copied, the data
stream it carries is dropped, and the sound is left as it is: cut.py converts
the 48 kHz Kick keeps into the 44.1 kHz the wire takes, once, when it chunks.
"""
import pathlib
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402
import cut  # noqa: E402
import kick  # noqa: E402
import supply  # noqa: E402

# The push has priority on this box: two live channels and two vCPU. At this
# rate a two hour part lands in about twenty minutes and nothing else notices.
RATE = "2M"
SEGMENT_TRIES = 3


def pick(now=None):
    """(id, url, start, end, name, seconds, bandwidth) of the next part."""
    table = kick.read_table()
    if not table:
        return None
    skip = supply.excluded(time.time() if now is None else now)
    for vid, secs in supply.newest_first(supply.read_catalog()):
        if vid in table and vid not in skip:
            url, start, end, name, bandwidth = table[vid]
            return vid, url, start, end, name, secs, bandwidth
    return None


def kick_hours_queued(table):
    """Seconds of Kick material waiting, a started file counted as what is left."""
    durations = chan.read_json(chan.STATE / "durations.json", {})
    return sum(cut.remaining(path, chan.duration(path, durations))
               for path in chan.media(chan.QUEUE) if chan.video_id(path) in table)


def fetch(url, start, end, target):
    """Stream the part's segments through one ffmpeg. True when it lands."""
    media = kick.get(url)
    segments = kick.parse_media(media)
    wanted = kick.segments_for(segments, start, end)
    if not wanted:
        chan.log("aucun segment dans la tranche demandee")
        return False
    job = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "mpegts", "-i", "pipe:0",
         "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-f", "matroska", "-y", str(target)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    done = 0
    try:
        for name in wanted:
            piece = None
            for attempt in range(SEGMENT_TRIES):
                got = subprocess.run(
                    ["curl", "-sS", "-m", "120", "--limit-rate", RATE, "-A", kick.UA,
                     urllib.parse.urljoin(url, name)],
                    capture_output=True, timeout=180)
                if got.returncode == 0 and got.stdout:
                    piece = got.stdout
                    break
                time.sleep(2 * (attempt + 1))
            if piece is None:
                chan.log(f"segment perdu apres {SEGMENT_TRIES} essais: {name}")
                job.stdin.close()
                job.wait(timeout=60)
                return False
            job.stdin.write(piece)
            done += 1
    except (OSError, subprocess.SubprocessError, BrokenPipeError) as problem:
        chan.log(f"transfert interrompu: {problem}")
        job.kill()
        return False
    finally:
        try:
            job.stdin.close()
        except OSError:
            pass
    job.wait(timeout=600)
    if job.returncode != 0:
        chan.log(f"ffmpeg a refuse la tranche: {job.stderr.read().decode(errors='replace')[-200:]}")
        return False
    chan.log(f"{done} segments repris")
    return True


def main(argv):
    apply = "--apply" in argv
    for folder in (chan.QUEUE, chan.UPLOAD, chan.STATE):
        folder.mkdir(parents=True, exist_ok=True)
    want = chan.read_json(chan.STATE / "want.json", {})
    if time.time() - want.get("at", 0) > 3600:
        chan.log("want.json perime, supply.py d'abord")
        return 0
    need, offer = want.get("need_seconds", 0), want.get("offer_bytes", 0)
    # This line runs four times an hour and lands a part in half an hour; the
    # board is paced at one download every ninety minutes and only ships when
    # the queue is short. So Kick refilled the window every time and the board
    # was never asked: nanatty247 aired twenty-four hours of Kick and nothing
    # from YouTube on 2026-09-18, which is not the mix it is configured for.
    # Kick fills its share and stops, and what is left is the board's to fill.
    ceiling = chan.conf_num("KICK_SHARE", 1.0) * chan.WINDOW_SECONDS
    held = kick_hours_queued(kick.read_table())
    if held >= ceiling:
        chan.log(f"part Kick en file {held / 3600:.1f} h sur {ceiling / 3600:.1f} h "
                 f"autorisees: la place restante est a la carte")
        return 0
    chosen = pick()
    if not need or chosen is None:
        chan.log(f"rien a prendre (besoin {need / 3600:.1f} h, "
                 f"{'aucun candidat Kick' if chosen is None else 'file pleine'})")
        return 0
    vid, url, start, end, name, secs, bandwidth = chosen
    # what this part really weighs, from the rung Kick serves it at
    weight = int(secs * bandwidth / 8) if bandwidth else int(secs * want.get("rate_bps", 250_000))
    if weight > offer:
        chan.log(f"{vid} pese environ {weight / chan.GIB:.1f} Go pour une offre de "
                 f"{offer / chan.GIB:.1f} Go: rien pris")
        return 0
    target_name = f"{int(time.time())}-{name}-{vid}.mkv"
    chan.log(f"{'prend' if apply else 'prendrait'} {vid} ({name[:50]}, "
             f"{(end - start) / 3600:.1f} h, tranche {start}-{end})")
    if not apply:
        return 0
    staging = chan.UPLOAD / target_name
    if not fetch(url, start, end, staging):
        staging.unlink(missing_ok=True)
        with (chan.STATE / "failed.tsv").open("a") as ledger:
            ledger.write(f"{int(time.time())}\t{vid}\tkick\n")
        return 1
    info = chan.probe(staging)
    if info is None or not info["seconds"] or info["seconds"] < (end - start) * 0.8:
        chan.log(f"tronque: {0 if info is None else int(info['seconds'])}s pour "
                 f"{end - start}s attendues, jete")
        staging.unlink(missing_ok=True)
        with (chan.STATE / "failed.tsv").open("a") as ledger:
            ledger.write(f"{int(time.time())}\t{vid}\ttronque\n")
        return 1
    staging.replace(chan.QUEUE / target_name)
    chan.log(f"en file {target_name} ({chan.size_of(chan.QUEUE / target_name) / chan.GIB:.2f} Go)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
