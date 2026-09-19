#!/usr/bin/env python3
"""Turn the next file into five minute chunks, copy only. A service.

An hour of a video at a time, the video drawn at random and the hour drawn at
random inside it. An hour that has been on the wire is never sent again: the
ledger that says so is keyed by video id and appended to, so it survives the
file being retired, evicted to make room, and fetched back.

Material is drawn in two tiers, and there is deliberately no third:
  1. an unaired hour of something in queue/
  2. an unaired hour of something in aired/, the reserve on disk
An hour is spent the moment its first chunk reaches the wire, so a cutter killed
half way through does not leave it drawable again. With nothing unseen on the
disk the channel shows its standby clip and watch.py raises the alarm, because
a disk with nothing left on it is a supply that failed, and showing the same
hour twice hides that instead of fixing it.

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
# Nothing is kept: the chunk being sent right now is interrupted too, because a
# skip somebody paid for that shows up ten minutes later is not a skip. The
# cutter has the next hour ready in about thirty seconds, well inside the one
# already in flight, so the wire does not go quiet.
SKIP_KEEP_CHUNKS = 0
# tells the feeder to drop what it is sending, not just what is waiting
FLUSH = chan.STATE / "flush"
SEQ = chan.STATE / "seq"
LIST = chan.WORK / "job.list"
PARTS = chan.STATE / "parts.json"
# every hour ever put on the wire: epoch, video id, hour number. Appended to and
# never rewritten, because what the wire has shown cannot be taken back.
HOURS = chan.STATE / "hours.tsv"
# which hour of which file each waiting chunk came from. The cutter is an hour
# ahead of the wire, so this is the only way the feeder, and through it the
# title, can say what is actually going out rather than what is being prepared.
CHUNKMAP = chan.STATE / "chunkmap.json"
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


def remember_chunk(chunk, source, number, seconds):
    """Tie a chunk to the hour it came from, and forget the ones already sent."""
    data = chan.read_json(CHUNKMAP, {})
    data[chunk] = [source, number, seconds]
    alive = {p.name for p in chan.CHUNKS.glob("*.ts")} | {chunk}
    chan.write_json(CHUNKMAP, {k: v for k, v in data.items() if k in alive})


def record_hour(name, number):
    """Write an hour down once. A resume walks the same branch a second time."""
    vid = chan.video_id(name) or str(name)
    if number in ledger().get(vid, {}):
        return
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


def recording_of(path):
    """What a piece belongs to. Two parts of one stream are one recording.

    kick.slice_parts cuts a long stream into parts that each carry its title, so
    a fifteen hour Kick VOD becomes seven files all called the same thing. They
    are different content and the draw treated them as unrelated, which is how
    the channel showed "Thailand Day 3" over and over on 2026-09-19: no rule was
    broken and it still looked like a loop, which is the only thing a viewer can
    judge. The first nine characters of a part id are the recording's own.
    """
    vid = chan.video_id(path) or ""
    return vid[:9] if KICK_ID.match(vid) else vid


def last_recording(book):
    """The recording whose hour left the wire most recently, or an empty string."""
    latest, when = "", 0
    for vid, hours in book.items():
        top = max(hours.values()) if hours else 0
        if top > when:
            latest, when = vid, top
    return latest[:9] if KICK_ID.match(latest) else latest


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
    There is no third tier. An hour that has been on the wire is spent, and a
    disk with nothing unseen on it means the supply failed, which is a thing to
    fix and not a thing to paper over by showing the same hour twice.

    Unsliced, the tiers collapse to the oldest file in the queue, as before.
    """
    if not chan.PART_SECONDS:
        files = chan.media(chan.QUEUE)
        # No reserve here either. An unsliced channel has no hour ledger, so
        # anything it drew from aired/ would be a whole video shown twice, and
        # the rule is the same rule for both channels: what has been on the
        # wire does not go back on it. One rule with an exception hidden in a
        # branch nobody reads is not a rule.
        return claim(files[0], "queue") if files else (None, None)

    book, durations = ledger(), chan.read_json(chan.STATE / "durations.json", {})
    # A file with every hour spent is never drawn again, and the queue is not
    # where supply.py looks for room, so one left there would hold its gigabytes
    # until somebody noticed. It belongs in the reserve, where eviction can see
    # it and where it is still the last thing to go while it holds anything.
    for spent in chan.media(chan.QUEUE):
        if not unaired(spent, book, durations) and chan.duration(spent, durations):
            spent.replace(chan.AIRED / spent.name)
            chan.log(f"entierement diffuse, passe en reserve: {spent.name[:60]}")
    just_played = last_recording(book)
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
        if not fresh:
            continue
        # two turns in a row from the same stream read as a repeat whatever the
        # hours say, so another recording wins whenever one is available
        elsewhere = [p for p in fresh if recording_of(p) != just_played] or fresh
        return claim(random.choice([p for p in elsewhere if from_board(p)] or elsewhere), origin)

    # No third tier. kil, 2026-09-19: "tu me repasses PAS 2 fois le meme chunk
    # d'une heure". An hour that has been on the wire is spent for good, so when
    # the disk holds nothing unseen the answer is the standby clip and an alarm,
    # never the same hour again. The cure is supply, and watch.py is what says
    # the supply failed.
    return None, None


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


def window(source, seconds, number=None):
    """(start, length, slice number) of the hour to air now, drawn at random,
    or None when the file has nothing unseen left in it.

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
    if number is None:
        # No fallback to the least recently aired hour. That branch is what put
        # hour 0 of one file back on the wire on 2026-09-19 at 16:03, four
        # hours after hour 0 of the same file, which is the one thing this
        # channel promises not to do. Nothing unseen means the file is spent,
        # and a spent file retires instead of going round again.
        free = [n for n in range(total) if n not in played(source.name)]
        if not free:
            return None
        number = random.choice(free)
    start = float(number * chan.PART_SECONDS)
    # the last slice runs to the end of the file, stub included
    length = (seconds - start if number == total - 1 else float(chan.PART_SECONDS))
    return start, max(0.0, length), number


def run_job(source, origin, skip, number=None):
    """Cut one hour of a file into chunks. number is set only on a resume.

    A restart used to come back through here with the chunks of one hour
    already on the wire and draw a different hour to follow them, skipping as
    many chunks of the new hour as the old one had produced. That is how a
    deploy at 16:03 on 2026-09-19 reaired an hour: the file was spent, the
    draw fell through to the replay branch, and the skip count belonged to
    nothing. The hour is part of the job, so it is read back with the job.
    """
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
    drawn = window(source, info["seconds"], number)
    if drawn is None:
        chan.log(f"plus rien d'inedit dans {source.name[:60]}, passe en reserve")
        settle(source, origin)
        return True
    start, length, number = drawn
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
                if moved == skip:
                    record_hour(source.name, number)
                chunk = f"{next_seq():010d}.ts"
                piece.replace(chan.CHUNKS / chunk)
                remember_chunk(chunk, source.name, number, info["seconds"])
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
            FLUSH.write_text(str(int(time.time())))
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
        if moved <= skip:
            record_hour(source.name, number)  # nothing aired, but never retry it blind
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
        number = job.get("number")
        resumed = (chan.CURRENT / job["source"], job.get("origin", "queue"),
                   int(job.get("done", 0)),
                   None if number is None else int(number))
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
