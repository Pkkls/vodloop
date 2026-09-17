#!/usr/bin/env python3
"""Turn the next file into five minute chunks, copy only. A service.

What airs next is the oldest file in queue/. When queue/ is empty the channel
replays: a random file among the half of aired/ that aired longest ago, so a
supply outage costs repeats and never dead air.

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


def pick_replay(aired, rng=random):
    """A random file among the least recently aired half, [(path, mtime)]."""
    if not aired:
        return None
    oldest = sorted(aired, key=lambda pm: pm[1])[:max(1, len(aired) // 2)]
    return rng.choice(oldest)[0]


def next_source():
    """(path, origin) of what airs next, moved into current/, or (None, None)."""
    for origin, folder in (("queue", chan.QUEUE), ("aired", chan.AIRED)):
        files = chan.media(folder)
        if not files:
            continue
        if origin == "queue":
            chosen = files[0]
        else:
            stamped = []
            for f in files:
                try:
                    stamped.append((f, f.stat().st_mtime))
                except OSError:
                    continue
            chosen = pick_replay(stamped)
            if chosen is None:
                continue
        target = chan.CURRENT / chosen.name
        try:
            chosen.replace(target)
        except OSError:
            continue  # supply evicted it a moment ago
        return target, origin
    return None, None


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
    """The file has aired: into aired/, stamped now, and remembered once."""
    target = chan.AIRED / source.name
    source.replace(target)
    os.utime(target)
    if origin == "queue":
        with (chan.STATE / "aired.tsv").open("a") as ledger:
            ledger.write(f"{int(time.time())}\t{chan.video_id(target) or target.name}\n")


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
