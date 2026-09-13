#!/usr/bin/env python3
"""Fetch this channel's Kick VODs onto the server itself. From cron.

The board downloads from YouTube because YouTube refuses Oracle's datacenter
IP outright, and there is one board for every channel here. Kick refuses
nothing: measured 2026-09-13 from the server, the API answers a plain curl
with 200 and the CDN served a 9 Mo segment at 33 Mbps. So the streams a Kick
channel is built on skip the board entirely, which is what stops two channels
sharing one board from halving each other's supply.

    python3 bin/kickfetch.py            report what it would fetch
    python3 bin/kickfetch.py --apply    fetch one part

One part per run, on purpose. A two hour part is about two gigabytes and a
quarter of an hour at the rate below, and cron comes back every twenty
minutes. A run that took several would hold the disk, the CPU and the uplink
for an hour at a time, on the box that is also pushing the live.

Nothing here re-encodes a picture. Kick already serves h264 in MPEG-TS at the
size this channel wants, so the segments are copied and only the sound is
touched: the source is AAC at 48 kHz and the rest of this tree works at
44100.
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import collector
import common
import prep

LIBRARY = common.LIBRARY_DIR
# The board stages its own uploads here. A name it does not recognise is a
# name it leaves alone, and nothing in here is ever removed by this module
# except its own unfinished work, which carries the suffix below.
ARRIVALS = LIBRARY / ".arrivee"
WORKING_SUFFIX = ".kickpart"
LEDGER = common.STATE / "kick.json"

API = "https://kick.com/api/v2/channels/%s/videos"
# Kick answers this. curl is on the box, it is what the API was proven
# against, and it is also the downloader below, so there is one HTTP client
# here rather than two.
UA = "curl/7.81.0"
# Bytes per second curl is allowed. About 16 Mbps, seven times real time on
# the 2.3 Mbps the 720p rung measured, so a two hour part lands in roughly a
# quarter of an hour. The ceiling is not politeness to the CDN: this box
# pushes a live stream out of the same interface on two vCPU, and an
# unthrottled fetch is the thing that would show up on air.
RATE = "2M"
# A part has to finish well inside a cron period even when the CDN is slow.
FETCH_TIMEOUT = 45 * 60
# Below this a VOD is not a rerun, it is a clip. Measured 2026-09-13: one of
# the 25 VODs listed was 90 seconds long.
MIN_SECONDS = collector.MIN_USEFUL_SECONDS
# What the finished file may differ from the video its segments held. They are
# ten seconds each, so this is a truncation check and not a precision one:
# what it catches is curl giving up half way through and ffmpeg muxing
# whatever had already arrived without a word.
DURATION_TOLERANCE = 0.02
# A failed part is worth trying again the same day, in case the CDN hiccuped,
# and not worth trying every twenty minutes for ever.
FAIL_COOLDOWN_SECONDS = 6 * 3600
# Kick is one source among several and must not become the library. Past this
# share of what the channel holds, nothing more is taken from it, unless the
# channel is about to run out of air and nothing else matters.
KICK_MAX_SHARE = 0.5
# A key this module minted, as opposed to a YouTube id that happens to start
# with the same letter. Ten hex digits behind a k is not a shape YouTube
# produces, and a collision would at worst miscount a few megabytes of quota.
KICK_KEY = re.compile(r"^k[0-9a-f]{10}$")
# What a title may keep. What is dropped either breaks a path or is not text:
# a slash is a directory, and emoji are not letters. Letters of any script
# stay, as they already do on the files the board delivers.
TITLE_ALLOWED = re.compile(r"[^\w .,'!()&+-]", re.UNICODE)


def get(url, timeout=120):
    """The body of a URL, or None. curl, and never an exception."""
    try:
        out = subprocess.run(
            ["curl", "-sS", "-f", "--retry", "3", "--retry-delay", "2",
             "-A", UA, url], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def slugs():
    """The Kick channels declared in sources.json.

    They sit in the same file as the YouTube sources, as an entry with a
    "kick" key and no url, which is the shape the collector already skips.
    """
    return [entry["kick"] for entry in collector.load(collector.SOURCES_FILE, [])
            if isinstance(entry, dict) and entry.get("kick")]


def key_of(uuid):
    """An eleven character key, in the shape the rest of the tree parses.

    Every module here reads a video id back off a filename with an eleven
    character pattern borrowed from YouTube. A Kick uuid is thirty six.
    Hashed to ten hex digits behind a k it fits that pattern, stays unique,
    and asks nothing of the janitor, prep, the collector or the chat bot.
    """
    return "k" + hashlib.sha1(uuid.encode("utf-8")).hexdigest()[:10]


def videos(slug):
    """The finished VODs of a channel, newest first.

    A live stream is listed while it is still running, with the duration it
    has reached so far, and its playlist grows under whatever is reading it.
    Left in, a part would be cut from a video that is not the length it will
    be.
    """
    try:
        items = json.loads(get(API % slug) or b"[]")
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict) or item.get("is_live"):
            continue
        uuid = (item.get("video") or {}).get("uuid")
        source = item.get("source")
        # the API reports milliseconds, and reports 0 for a VOD with nothing
        # behind it yet
        secs = int((item.get("duration") or 0) / 1000)
        if not uuid or not source or secs < MIN_SECONDS:
            continue
        out.append({
            "key": key_of(uuid),
            "secs": secs,
            # every rendition, and every segment, hangs off the master's folder
            "base": source.rsplit("/", 1)[0],
            "title": item.get("session_title") or "",
            "date": str(item.get("start_time") or item.get("created_at") or "")[:10],
        })
    return out


def rendition(base):
    """The tallest rung this channel is allowed, as a playlist URL.

    Two filters, and both were paid for once already. The height is what the
    channel broadcasts, and pulling 1080p to send 720p spends three times the
    disk and the bandwidth on a picture nobody sees. The frame rate is Kick's
    own ingest ladder: fed a rung it does not have, it re-encodes the stream
    and what the viewer gets is its work rather than ours.

    Returns (playlist url, height, bytes per second) or None.
    """
    text = (get(base + "/master.m3u8") or b"").decode("utf-8", "replace")
    lines = text.splitlines()
    best = None
    for n, line in enumerate(lines[:-1]):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        path = lines[n + 1].strip()
        height = re.search(r"RESOLUTION=\d+x(\d+)", line)
        fps = re.search(r"FRAME-RATE=([\d.]+)", line)
        rate = re.search(r"BANDWIDTH=(\d+)", line)
        if not path or path.startswith("#") or not height:
            continue
        height = int(height.group(1))
        if collector.MAX_HEIGHT and height > collector.MAX_HEIGHT:
            continue
        if not common.fps_supported(float(fps.group(1)) if fps else None):
            continue
        if best is None or height > best[1]:
            best = (join(base, path), height, int(rate.group(1)) / 8 if rate else 0)
    return best


def join(base, path):
    """A playlist's own link, resolved against where the playlist was found."""
    return path if path.startswith("http") else base.rstrip("/") + "/" + path


def segments(playlist):
    """The segment links of one rung, in order, each with its own length."""
    text = (get(playlist) or b"").decode("utf-8", "replace")
    base = playlist.rsplit("/", 1)[0]
    out, secs = [], 0.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                secs = float(line[len("#EXTINF:"):].strip().rstrip(","))
            except ValueError:
                secs = 0.0
        elif line and not line.startswith("#"):
            out.append((join(base, line), secs))
    return out


def window(segs, span):
    """The segments covering a part, and the video they really hold.

    Cut on segment boundaries rather than on the second asked for: a segment
    is ten seconds of one stream and half of one is not a file. Every segment
    belongs to the part its start falls in, so the parts of a video tile it
    exactly, with no second fetched twice and none missed. What they add up
    to is returned with them, because that, and not the range the collector
    computed, is what the finished file has to match.
    """
    start, end = span if span else (0, float("inf"))
    at, links, total = 0.0, [], 0.0
    for link, secs in segs:
        if start <= at < end:
            links.append(link)
            total += secs
        at += secs
    return links, total


def filename(video, key):
    """The name the rest of the tree expects from a downloaded file.

    The date leads because a rerun channel shows the same streamer for months
    and the day is most of what tells two evenings apart. The key trails,
    where every other module looks for it. The part, when there is one, is
    kept by pretty_title on purpose, so chat can say which piece is on air.
    """
    vid, _, part = key.partition(".")
    title = TITLE_ALLOWED.sub(" ", video["title"])
    title = " ".join(title.split())[:120].strip(" .-") or "kick"
    return f"{video['date']}_{title}-{vid}{'.' + part if part else ''}.mkv"


def fetch(links, dest):
    """Stream those segments into one file. True if ffmpeg was happy.

    curl takes its URLs from a config file rather than a command line a
    thousand segments long, keeps to a rate this box can spare, and retries a
    segment on its own. What it writes is one MPEG-TS stream, which is what
    the segments already are.

    The two maps are not decoration: Kick carries a timed_id3 data track
    alongside the picture and the sound, and matroska has nowhere to put it.

    A happy ffmpeg is not a whole file. curl stops at the first segment it
    cannot get and ffmpeg muxes what had already arrived without a word, so
    the caller checks the length of what landed.
    """
    conf = dest.with_name(dest.name + ".conf")
    conf.write_text("\n".join(
        [f'user-agent = "{UA}"', f"limit-rate = {RATE}", "retry = 3",
         "retry-delay = 2", "fail", "silent", "show-error"]
        + [f'url = "{link}"' for link in links]) + "\n")
    curl = None
    try:
        curl = subprocess.Popen(["curl", "--config", str(conf)],
                                stdout=subprocess.PIPE)
        done = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "mpegts",
             "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0"]
            + common.REMUX + ["-f", "matroska", "-y", str(dest)],
            stdin=curl.stdout, capture_output=True, text=True,
            timeout=FETCH_TIMEOUT)
        curl.stdout.close()
        curl.wait(timeout=60)
        if done.returncode:
            last = done.stderr.strip().splitlines()
            print(f"  ffmpeg a rendu {done.returncode}: {last[-1] if last else ''}")
            return False
        return True
    except (OSError, subprocess.SubprocessError) as err:
        print(f"  telechargement interrompu: {err}")
        return False
    finally:
        if curl and curl.poll() is None:
            curl.kill()
        conf.unlink(missing_ok=True)


def kick_bytes():
    """What this channel's Kick VODs already weigh, unfinished ones included."""
    total = 0
    for path in LIBRARY.rglob("*"):
        key = collector.key_of(path)
        mine = (key and KICK_KEY.match(key.partition(".")[0])
                or path.name.endswith(WORKING_SUFFIX))
        if not mine:
            continue
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue  # removed between the listing and the stat
    return total


def pending(vids, known):
    """Every part not held yet, part one of each video before part two of any.

    Round robin on the part number, not video by video. The channel plays one
    part at a time out of a shuffled library, so six parts of one stream on
    the disk is the same evening six times over, while one part each of six
    streams is six evenings. Within a rank the API order is kept, which is
    newest first.
    """
    out = []
    for video in vids:
        for rank, (key, span) in enumerate(
                collector.parts_of(video["key"], video["secs"])):
            if key not in known:
                out.append((rank, video, key, span))
    out.sort(key=lambda item: item[0])
    return out


def save_ledger(done, tried, now):
    """What has been taken and what was attempted, pruned as it is written.

    A ledger nothing ever removes from grows for as long as the channel runs.
    """
    keep = max(7 * 24 * 3600, collector.REFETCH_SECONDS)
    collector.save(LEDGER, {
        "done": {k: at for k, at in done.items() if now - at < keep},
        "tried": {k: at for k, at in tried.items()
                  if now - at < FAIL_COOLDOWN_SECONDS}})


def main(argv):
    apply = "--apply" in argv
    now = time.time()
    ledger = collector.load(LEDGER, {})
    done, tried = ledger.get("done", {}), ledger.get("tried", {})
    # prep reads its library out of the environment; importing the collector
    # is what points it at this one, and the runway below is measured on it.
    runway = prep.runway_seconds()
    urgent = runway < collector.URGENT_RUNWAY_SECONDS
    free = shutil.disk_usage(LIBRARY).free
    budget = common.BUDGET_BYTES
    used = common.bytes_used(LIBRARY) if budget else 0
    mine = kick_bytes()

    # the collector's rule, for the same reason: a part is played and retired
    # while its neighbours are still to come, so a short cooldown loops the
    # channel on part one and it never reaches part two
    cooldown = collector.REFETCH_SECONDS or collector.HANDED_COOLDOWN_SECONDS
    known = (collector.library_ids()
             | {k for k, at in done.items() if now - at < cooldown}
             | {k for k, at in tried.items() if now - at < FAIL_COOLDOWN_SECONDS})

    share = f" part={used / 1024 ** 3:.1f}/{budget / 1024 ** 3:.1f}G" if budget else ""
    print(f"libre={free / 1024 ** 3:.1f}G{share} kick={mine / 1024 ** 3:.1f}G "
          f"antenne={runway / 3600:.1f}h{' URGENT' if urgent else ''} "
          f"sources={len(slugs())} tenus={len(known)}")

    room = free - common.SHARED_FREE_FLOOR_BYTES
    if budget:
        room = min(room, budget - used)
    if room <= 0:
        print("part servie ou disque trop juste: rien telecharge")
        return 0

    vids = [video for slug in slugs() for video in videos(slug)]
    if not vids:
        print("aucune VOD listee (API muette ou chaine vide)")
        return 0
    waiting = pending(vids, known)
    if not waiting:
        print(f"{len(vids)} VOD(s), rien a prendre: tout est tenu ou en attente")
        return 0

    _, video, key, span = waiting[0]
    rung = rendition(video["base"])
    if not rung:
        print(f"aucune definition utilisable pour {key}")
        return 0
    playlist, height, weight = rung
    links, length = window(segments(playlist), span)
    if not links:
        print(f"playlist illisible ou tranche vide pour {key}")
        return 0
    size = int(weight * length)
    part = key.partition(".")[2]

    print(f"  {key} {video['date']} {height}p part={part or 'entier'} "
          f"{length / 3600:.1f}h {size / 1024 ** 3:.2f}G ({len(links)} segments) "
          f"sur {len(waiting)} en attente")

    if size > room:
        print(f"il manque {(size - room) / 1024 ** 3:.1f}G dans la part: rien telecharge")
        return 0
    # Kick is one source among several. Without this it becomes the library:
    # it is the only source that arrives without waiting for the board, so it
    # would win every race until the disk held nothing but this channel's own
    # archive of itself.
    if budget and not urgent and mine + size > budget * KICK_MAX_SHARE:
        print(f"kick tient deja {mine / 1024 ** 3:.1f}G sur "
              f"{budget * KICK_MAX_SHARE / 1024 ** 3:.1f}G: rien telecharge")
        return 0

    if not apply:
        print("essai a blanc, rien n'a ete telecharge. --apply pour agir.")
        return 0

    ARRIVALS.mkdir(parents=True, exist_ok=True)
    # a run killed mid part leaves its working file behind, and cron holds a
    # lock, so anything still here belongs to nobody
    for stale in ARRIVALS.glob("*" + WORKING_SUFFIX + "*"):
        stale.unlink(missing_ok=True)
    # Written before the download and not after. A part that takes the process
    # down with it would otherwise be picked again on the next run, for ever.
    tried[key] = now
    save_ledger(done, tried, now)

    working = ARRIVALS / (key + WORKING_SUFFIX)
    started = time.time()
    if not fetch(links, working):
        working.unlink(missing_ok=True)
        return 1
    got = prep.duration_of(working)
    if abs(got - length) > max(length * DURATION_TOLERANCE, 10):
        print(f"  tronque: {got:.0f}s pour {length:.0f}s attendues, jete")
        working.unlink(missing_ok=True)
        return 1

    final = LIBRARY / filename(video, key)
    weight = working.stat().st_size
    os.replace(working, final)
    done[key] = now
    tried.pop(key, None)
    save_ledger(done, tried, now)
    print(f"  {final.name} {weight / 1024 ** 3:.2f}G en "
          f"{(time.time() - started) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
