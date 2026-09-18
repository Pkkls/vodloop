#!/usr/bin/env python3
"""Turn the next file into five minute chunks, copy only. A service.

What airs next is the oldest file in queue/, and nothing else. A video that has
been on air moves to aired/, where it is kept as a reserve but never drawn
again, and its id leaves the catalogue: the channel does not repeat itself, and
an empty queue means the standby clip until the board delivers. That is kil's
call, taken 2026-09-17 with the cost stated.

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
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402

JOB = chan.STATE / "job.json"
SEQ = chan.STATE / "seq"
LIST = chan.WORK / "job.list"
POLL = 2


def ahead_seconds():
    return len(list(chan.CHUNKS.glob("*.ts"))) * chan.CHUNK_SECONDS


def next_source():
    """(path, origin) of what airs next, moved into current/, or (None, None).

    The oldest file waiting, and nothing else: a channel that has run out plays
    its standby clip until the board delivers, it never goes back over what it
    has already shown.
    """
    files = chan.media(chan.QUEUE)
    if not files:
        return None, None
    chosen = files[0]
    target = chan.CURRENT / chosen.name
    try:
        chosen.replace(target)
    except OSError:
        return None, None
    return target, "queue"


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
    with (chan.STATE / "aired.tsv").open("a") as ledger:
        ledger.write(f"{int(time.time())}\t{chan.video_id(source) or source.name}\n")
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
    ledger = "rejected.tsv" if forever else "failed.tsv"
    with (chan.STATE / ledger).open("a") as fh:
        fh.write(f"{int(time.time())}\t{chan.video_id(source) or source.name}\t{reason}\n")
    source.unlink(missing_ok=True)
    chan.telegram(f"video refusee ({reason}): {source.name[:80]}")


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
    chan.WORK.mkdir(parents=True, exist_ok=True)
    for stale in chan.WORK.iterdir():
        stale.unlink(missing_ok=True)
    chan.write_json(JOB, {"source": source.name, "origin": origin, "done": skip})
    job = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy"] + audio + chan.DROP_SEI
        + ["-f", "segment", "-segment_time", str(chan.CHUNK_SECONDS),
           "-segment_format", "mpegts", "-segment_list", str(LIST),
           "-reset_timestamps", "1", str(chan.WORK / "job_%05d.ts")],
        stderr=subprocess.PIPE)
    chan.log(f"decoupe {source.name} ({origin}, {info['seconds'] / 3600:.1f} h, "
             f"son {'copie' if audio[1] == 'copy' else 'transcode'}, saut {skip})")
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
                chan.write_json(JOB, {"source": source.name, "origin": origin, "done": moved + 1})
            moved += 1

    while job.poll() is None:
        collect()
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
    if job.returncode != 0 and moved == 0:
        reject(source, (error.splitlines() or ["ffmpeg a echoue"])[-1][:120], forever=False)
        return True
    if job.returncode != 0:
        chan.log(f"decoupe interrompue apres {moved} chunks: {error[-200:]}")
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
