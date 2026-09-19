#!/usr/bin/env python3
"""Turn the next file into five minute chunks, copy only. A service.

An hour of a video at a time, the video drawn at random and the hour drawn at
random inside it. An hour that has been on the wire is never sent again: the
ledger that says so is keyed by video id and appended to, so it survives the
file being retired, evicted to make room, and fetched back.

Material is drawn in three tiers, and the wire decides the order, not taste:
  1. an unaired hour of something in queue/
  2. an unaired hour of something in aired/, the reserve on disk
  3. only when every hour of everything on disk has been on the wire, the one
     aired longest ago. A repeat is worse than new material and better than a
     loading card, and this tier is the one that says the channel is starving.

One ffmpeg segment job per file. Chunks are only moved into chunks/ once the
muxer has listed them as complete, so the feeder never reads a half-written
one. The job is paused (SIGSTOP) once AHEAD_SECONDS of chunks are waiting and
resumed below that, which bounds the disk the chunks take whatever the length
of the file: v1 cut whole items ahead and held 3.58 h of chunks against a 2 h cap.

A job interrupted by a restart is cut again from the start and its first N
chunks, already sent, are dropped: segmenting a copy is deterministic.
"""
import os
import pathlib
import random
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402

JOB = chan.STATE / "job.json"
# the chat's two levers, written by bot.py and consumed here exactly once
SKIP = chan.STATE / "skip"
PICK = chan.STATE / "pick"
# what stays on the wire when an hour is cut short, so a skip is a change of
# picture and never a gap: ten minutes to cut and move the next one in
SKIP_KEEP_CHUNKS = 2
SEQ = chan.STATE / "seq"
LIST = chan.WORK / "job.list"
PARTS = chan.STATE / "parts.json"
# every hour ever put on the wire: epoch, video id, hour number. Appended to and
# never rewritten, because what the wire has shown cannot be taken back.
HOURS = chan.STATE / "hours.tsv"
POLL = 2
# a slice that would leave less than this behind takes the rest of the file
# with it, rather than coming back for ninety seconds
TAIL_SECONDS = 300


def ahead_seconds():
    return len(list(chan.CHUNKS.glob("*.ts"))) * chan.CHUNK_SECONDS


def slices_in(seconds):
    """How many slices a file of this length holds, the stub folded into the last.

    A 5 h 02 file at an hour a slice holds five, the last one running 1 h 02,
    rather than five and a two minute offcut nobody wants on the wire.
    """
    return max(1, int(seconds // chan.PART_SECONDS)) if chan.PART_SECONDS else 1


def ledger():
    """{video id: {hour number: when it was on the wire}}, the whole history.

    Reads the two formats that came before it, so no hour is forgotten across
    the change: a list of hour numbers, and before that a cursor saying how far
    into the file the sequential pass had reached.
    """
    out = {}
    try:
        for line in HOURS.read_text().splitlines():
            fields = line.split("\t")
            if len(fields) >= 3:
                try:
                    out.setdefault(fields[1], {})[int(fields[2])] = int(fields[0])
                except ValueError:
                    continue
    except OSError:
        pass
    for name, value in chan.read_json(PARTS, {}).items():
        vid = chan.video_id(name) or name
        if isinstance(value, list):
            numbers = [int(n) for n in value]
        elif isinstance(value, (int, float)) and chan.PART_SECONDS:
            numbers = list(range(int(float(value) // chan.PART_SECONDS)))
        else:
            continue
        for n in numbers:
            out.setdefault(vid, {}).setdefault(n, 0)
    return out


def played(name, book=None):
    """The hour numbers of this video that have already been on the wire."""
    book = ledger() if book is None else book
    return set(book.get(chan.video_id(name) or str(name), {}))


def record_hour(name, number):
    chan.STATE.mkdir(parents=True, exist_ok=True)
    with HOURS.open("a") as fh:
        fh.write("%d\t%s\t%d\n" % (int(time.time()), chan.video_id(name) or name, number))


def set_part(name, value):
    """Kept for the tests and for a hand repair; the ledger is the truth."""
    data = chan.read_json(PARTS, {})
    if value is None:
        data.pop(name, None)
    else:
        data[name] = sorted(value) if isinstance(value, set) else value
    chan.write_json(PARTS, data)


def remaining(path, seconds):
    """Seconds of a file still to air, the hours already on the wire removed.

    supply.py and kickfetch.py both size the queue with this: a file sits in it
    between two of its hours, and counting those hours again would have the
    channel believe it holds a window it has already spent.
    """
    if not chan.PART_SECONDS:
        return seconds
    return max(0.0, seconds - len(played(pathlib.Path(path).name)) * chan.PART_SECONDS)


# a part id minted by kick.part_id: k, eight hex of the recording, its number
KICK_ID = re.compile(r"^k[0-9a-f]{8}[0-9]{2}$")


def from_board(path):
    """True for what the board brought back, false for this server's Kick line.

    Kick is the supply that answers when the wire is running out, so it ends up
    in the queue beside material that was asked for on purpose. Preferring the
    board here is what stops one emergency part from taking turns for days, and
    it is the difference between a channel that holds its shape by itself and
    one where somebody moves files out of the queue by hand, which is what I
    did twice on 2026-09-19.
    """
    return not KICK_ID.match(chan.video_id(path) or "")


def unaired(path, book, durations):
    """The hours of this file that have never been on the wire."""
    seconds = chan.duration(path, durations)
    if not seconds:
        return set()
    return set(range(slices_in(seconds))) - played(path.name, book)


def next_source():
    """(path, origin) of what airs next, moved into current/, or (None, None).

    Three tiers, taken in order, and the wire decides the order:

      queue     something delivered and never aired. Drawn at random, because
                the point of slicing is that two hours of the channel are not
                two hours of the same stream.
      reserve   something in aired/ that still holds an hour nobody has seen.
                It is on the disk already, so it beats a loading card by the
                whole time a delivery would take.
      repeat    every hour of everything on disk has been on the wire. The file
                holding the hour aired longest ago goes back on. This tier is
                the channel starving, and watch.py is what says so.

    Unsliced, the tiers collapse to the oldest file in the queue, as before.
    """
    if not chan.PART_SECONDS:
        files = chan.media(chan.QUEUE)
        if files:
            return claim(files[0], "queue")
        # an unsliced channel has no hours to account for, but it has the same
        # reserve and the same reason to prefer it to a loading card
        spare = sorted(chan.media(chan.AIRED), key=lambda p: p.stat().st_mtime)
        return claim(spare[0], "repeat") if spare else (None, None)

    book, durations = ledger(), chan.read_json(chan.STATE / "durations.json", {})
    wanted = chan.read_json(PICK, {}).get("name")
    PICK.unlink(missing_ok=True)
    if wanted:
        for folder in (chan.QUEUE, chan.AIRED):
            path = folder / wanted
            if path.exists() and unaired(path, book, durations):
                chan.log(f"choix du chat: {wanted[:60]}")
                return claim(path, "chat")
        chan.log(f"choix du chat introuvable ou deja vu: {str(wanted)[:60]}")
    for folder, origin in ((chan.QUEUE, "queue"), (chan.AIRED, "reserve")):
        fresh = [p for p in chan.media(folder) if unaired(p, book, durations)]
        if fresh:
            return claim(random.choice([p for p in fresh if from_board(p)] or fresh), origin)

    oldest, held = None, None
    for folder in (chan.QUEUE, chan.AIRED):
        for path in chan.media(folder):
            when = min(book.get(chan.video_id(path) or path.name, {0: 0}).values())
            if held is None or when < held:
                oldest, held = path, when
    if oldest is None:
        return None, None
    chan.log(f"plus une heure inedite sur le disque: {oldest.name[:60]} repasse")
    return claim(oldest, "repeat")


def claim(chosen, origin):
    target = chan.CURRENT / chosen.name
    try:
        chosen.replace(target)
    except OSError:
        return None, None
    return target, origin


def next_seq():
    try:
        value = int(SEQ.read_text().strip()) + 1
    except (OSError, ValueError):
        value = 1
    SEQ.write_text(str(value))
    return value


def completed(listing):
    try:
        return [line.strip() for line in listing.read_text().splitlines() if line.strip()]
    except OSError:
        return []


def settle(source, origin):
    """The file has aired: remembered, and set aside in aired/.

    kil, 2026-09-17: a video that has been on air never goes back on air, and
    the ledger keeps its id out of the catalogue for REFETCH_DAYS, set long
    enough to mean never. The file itself is kept as a reserve, which costs
    disk and nothing else: supply.py evicts it, oldest first, when the room it
    offers the board needs it. With an empty queue the channel shows its
    standby clip rather than repeating, and the cure is supply, not memory.
    """
    with (chan.STATE / "aired.tsv").open("a") as fh:
        fh.write(f"{int(time.time())}\t{chan.video_id(source) or source.name}\n")
    target = chan.AIRED / source.name
    source.replace(target)
    os.utime(target)


def reject(source, reason, forever=True):
    """Drop a file, and say whether its video is worth fetching again.

    A shape the wire cannot carry, or a copy that loses its timestamps, is a
    property of the source: asking for it again wastes an hour of the board's
    paced budget for the same answer. An ffmpeg that fell over is not: that one
    goes to failed.tsv, where supply.py forgives it once.
    """
    chan.log(f"refuse {source.name}: {reason}")
    book = "rejected.tsv" if forever else "failed.tsv"
    with (chan.STATE / book).open("a") as fh:
        fh.write(f"{int(time.time())}\t{chan.video_id(source) or source.name}\t{reason}\n")
    set_part(source.name, None)
    source.unlink(missing_ok=True)
    chan.telegram(f"video refusee ({reason}): {source.name[:80]}")


def window(source, seconds):
    """(start, length, slice number) of the hour to air now, drawn at random.

    kil, 2026-09-19: "tu pick aleatoirement dans la video, par exemple tu mets
    directement a 3h en plein milieu". So an hour is taken from anywhere in the
    file, not from where the last one stopped: the file is drawn at random and
    the hour inside it is drawn at random too.

    An hour already aired is not drawn again. When every hour of a file has
    been on the wire the file retires, which is what keeps a random draw from
    becoming a loop. Unsliced, the whole file is the one slice.

    The marks are nominal. A copy can only start on a keyframe, so a slice can
    open up to one GOP early, about two seconds on this material. Making it
    exact would mean encoding.
    """
    if not chan.PART_SECONDS:
        return 0.0, seconds, 0
    total = slices_in(seconds)
    book = ledger()
    free = [n for n in range(total) if n not in played(source.name, book)]
    if free:
        number = random.choice(free)
    else:
        # nothing unseen left in this file: the hour that has been off the wire
        # longest is the one a viewer is least likely to recognise
        seen = book.get(chan.video_id(source) or source.name, {})
        number = min(range(total), key=lambda n: seen.get(n, 0))
    start = float(number * chan.PART_SECONDS)
    # the last slice runs to the end of the file, stub included
    length = (seconds - start if number == total - 1 else float(chan.PART_SECONDS))
    return start, max(0.0, length), number


def run_job(source, origin, skip):
    info = chan.probe(source)
    if info is None:
        chan.log(f"sonde impossible pour {source.name}, nouvel essai plus tard")
        return False
    problem = chan.shape_problem(info)
    if problem is None:
        safe = chan.remux_is_safe(source)
        if safe is None:
            chan.log(f"essai de copie impossible pour {source.name}, nouvel essai plus tard")
            return False
        if not safe:
            problem = "paquets sans PTS apres copie"
    if problem:
        reject(source, problem)
        return True

    audio = ["-c:a", "copy"] if chan.audio_copies(info) else \
        ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"]
    start, length, number = window(source, info["seconds"])
    # -ss and -t before -i: seeking on the input costs nothing on a copy, and
    # the same pair after it would decode everything up to the mark
    seek = ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"] if chan.PART_SECONDS else []
    chan.WORK.mkdir(parents=True, exist_ok=True)
    for stale in chan.WORK.iterdir():
        stale.unlink(missing_ok=True)
    chan.write_json(JOB, {"source": source.name, "origin": origin, "done": skip,
                          "number": number, "seconds": info["seconds"],
                          "started": int(time.time())})
    SKIP.unlink(missing_ok=True)
    job = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error"] + seek + ["-i", str(source),
         "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy"] + audio + chan.DROP_SEI
        + ["-f", "segment", "-segment_time", str(chan.CHUNK_SECONDS),
           "-segment_format", "mpegts", "-segment_list", str(LIST),
           "-reset_timestamps", "1", str(chan.WORK / "job_%05d.ts")],
        stderr=subprocess.PIPE)
    started_at, skipped = int(time.time()), False
    chan.log(f"decoupe {source.name} ({origin}, {info['seconds'] / 3600:.1f} h, "
             + (f"tranche {start / 3600:.1f}-{(start + length) / 3600:.1f} h, " if seek else "")
             + f"son {'copie' if audio[1] == 'copy' else 'transcode'}, saut {skip})")
    moved, paused = 0, False

    def collect():
        nonlocal moved
        names = completed(LIST)
        while moved < len(names):
            piece = chan.WORK / names[moved]
            if moved < skip:
                piece.unlink(missing_ok=True)
            else:
                piece.replace(chan.CHUNKS / f"{next_seq():010d}.ts")
                chan.write_json(JOB, {"source": source.name, "origin": origin,
                                      "done": moved + 1, "number": number,
                                      "seconds": info["seconds"], "started": started_at})
            moved += 1

    while job.poll() is None:
        collect()
        if SKIP.exists():
            # the chat has voted this hour off. Cutting more of it is wasted work
            # and the chunks already waiting are the hour itself, so both go; two
            # are kept so the wire has something while the next hour is cut.
            reason = chan.read_json(SKIP, {}).get("reason", "demande")
            SKIP.unlink(missing_ok=True)
            if paused:
                # a stopped process does not act on SIGTERM, it just stays
                # stopped, and the buffer is full exactly when a skip is asked
                job.send_signal(signal.SIGCONT)
                paused = False
            job.terminate()
            for stale in sorted(chan.CHUNKS.glob("*.ts"))[SKIP_KEEP_CHUNKS:]:
                stale.unlink(missing_ok=True)
            chan.log(f"passage saute ({reason}): {source.name[:60]}")
            skipped = True
            break
        full = ahead_seconds() >= chan.AHEAD_SECONDS + 2 * chan.CHUNK_SECONDS
        if full and not paused:
            job.send_signal(signal.SIGSTOP)
            paused = True
        elif paused and ahead_seconds() < chan.AHEAD_SECONDS:
            job.send_signal(signal.SIGCONT)
            paused = False
        time.sleep(POLL)
    collect()
    error = job.stderr.read().decode(errors="replace").strip()
    for stale in chan.WORK.iterdir():
        stale.unlink(missing_ok=True)
    JOB.unlink(missing_ok=True)
    if job.returncode != 0 and moved == 0 and not skipped:
        reject(source, (error.splitlines() or ["ffmpeg a echoue"])[-1][:120], forever=False)
        return True
    if job.returncode != 0 and not skipped:
        chan.log(f"decoupe interrompue apres {moved} chunks: {error[-200:]}")
    if chan.PART_SECONDS:
        record_hour(source.name, number)
        done = played(source.name)
        total = slices_in(info["seconds"])
        if len(done) < total:
            source.replace(chan.QUEUE / source.name)
            chan.log(f"fini {source.name}: {moved} chunks, {len(done)}/{total} heures diffusees")
            return True
    settle(source, origin)
    chan.log(f"fini {source.name}: {moved} chunks")
    return True


def recover():
    """What a stop left behind: a job to resume, or files to put back in line."""
    job = chan.read_json(JOB, None)
    resumed = None
    if job and (chan.CURRENT / job["source"]).exists():
        resumed = (chan.CURRENT / job["source"], job.get("origin", "queue"), int(job.get("done", 0)))
    for leftover in chan.media(chan.CURRENT):
        if resumed is None or leftover != resumed[0]:
            leftover.replace(chan.QUEUE / leftover.name)
    return resumed


def main():
    for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED, chan.CHUNKS, chan.WORK, chan.STATE):
        folder.mkdir(parents=True, exist_ok=True)
    resumed = recover()
    if resumed:
        if not run_job(*resumed):
            resumed[0].replace(chan.QUEUE / resumed[0].name)
    while True:
        if ahead_seconds() >= chan.AHEAD_SECONDS:
            time.sleep(10)
            continue
        source, origin = next_source()
        if source is None:
            time.sleep(30)
            continue
        if not run_job(source, origin, 0):
            # could not measure: back where it came from, and give the box a minute
            source.replace((chan.QUEUE if origin == "queue" else chan.AIRED) / source.name)
            time.sleep(60)


if __name__ == "__main__":
    main()
