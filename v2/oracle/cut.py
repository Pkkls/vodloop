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
import json
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
# An hour cut ahead of time and held aside, so a skip has something to put on
# the wire in the same second. Without it the chat votes, everything waiting is
# dropped, and the channel shows its standby clip for as long as ffmpeg needs
# to write a first segment of a new hour.
READY = chan.ROOT / "ready"
READY_JOB = chan.STATE / "ready.json"
# what main() must know about a skip that was served from the ready set: which
# hour it is now on, and how much of it is already on the wire
PRIMED = chan.STATE / "primed.json"
PRIMER_SECONDS = chan.CHUNK_SECONDS + 30
# tells the feeder to drop what it is sending, not just what is waiting
FLUSH = chan.STATE / "flush"
SEQ = chan.STATE / "seq"
LIST = chan.WORK / "job.list"
PARTS = chan.STATE / "parts.json"
# every hour ever put on the wire: epoch, video id, hour number. Appended to and
# never rewritten, because what the wire has shown cannot be taken back.
HOURS = chan.STATE / "hours.tsv"
# the same ledger at the granularity the wire actually works in. An hour is
# spent the moment its first chunk goes out, so skipping ten minutes in used
# to destroy fifty that nobody had seen and nobody ever would. A chunk is
# spent when it is sent, so a skip now costs what was watched and the rest
# of the hour goes back in the draw. Separate file on purpose: hour 3 and
# chunk 3 are the same three and nothing may confuse them. Old rows keep
# being read as hours, twelve chunks each, so this cannot un-spend anything.
UNITS = chan.STATE / "units.tsv"
# kil, 2026-09-19: never repeating is too strong. The library holds about 1740
# reachable hours against 24 aired a day and 5 published, so it empties in
# three months and then the channel has nothing to show at all. A chunk older
# than this may go round again, but only once nothing unseen is left anywhere,
# so it changes nothing until the day it is the difference between a repeat
# and a standby clip. It has to be shorter than a full pass of the library,
# which is 72 days, or at that moment nothing would be eligible either.
REPEAT_AFTER = int(chan.conf_num("REPEAT_AFTER_DAYS", 45) * 86400)
# next_source found nothing unseen and opened the window; main() reads this to
# tell run_job which moment counts as "already aired". A file, not a return
# value, because next_source has nine callers in the tests and none of them
# care. Only ever set on a fresh draw: a resume carries its own chunk number
# and never asks the draw anything.
REPLAY = chan.STATE / "replay.json"
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
    rather than five and a two minute offcut nobody wants on the wire. This is
    still what a title counts in; the ledger counts in chunks.
    """
    return max(1, int(seconds // chan.PART_SECONDS)) if chan.PART_SECONDS else 1


def unit_seconds():
    """The ledger's grain: a chunk, or the whole block when a block is the
    smaller of the two. They only invert where a slice is seconds long, which
    is the tests, and the ledger must not have a grain coarser than what it
    is measuring."""
    return min(chan.CHUNK_SECONDS, chan.PART_SECONDS) if chan.PART_SECONDS else 0


def units_in(seconds):
    """How many grains a file of this length holds, the stub folded in."""
    if not chan.PART_SECONDS:
        return 1
    return max(1, int(seconds // unit_seconds()))


def per_slice():
    """Grains in one aired block."""
    if not chan.PART_SECONDS:
        return 1
    return max(1, int(chan.PART_SECONDS // unit_seconds()))


def ledger():
    """{video id: {chunk number: when it was on the wire}}, the whole history.

    Reads every shape that came before it, so nothing is forgotten and nothing
    is handed back as unseen: an hour written by the old cutter counts as the
    twelve chunks it covered, a list of hour numbers the same, and before that
    a cursor saying how far the sequential pass had reached. Each older shape
    can only mark more spent, never less, which is the direction to be wrong in.
    """
    out, span = {}, per_slice()
    try:
        for line in UNITS.read_text().splitlines():
            fields = line.split("\t")
            if len(fields) >= 3:
                try:
                    out.setdefault(fields[1], {})[int(fields[2])] = int(fields[0])
                except ValueError:
                    continue
    except OSError:
        pass
    try:
        for line in HOURS.read_text().splitlines():
            fields = line.split("\t")
            if len(fields) >= 3:
                try:
                    when, first = int(fields[0]), int(fields[2]) * span
                except ValueError:
                    continue
                for unit in range(first, first + span):
                    out.setdefault(fields[1], {}).setdefault(unit, when)
    except OSError:
        pass
    for name, value in chan.read_json(PARTS, {}).items():
        vid = chan.video_id(name) or name
        if isinstance(value, list):
            hours = [int(h) for h in value]
        elif isinstance(value, (int, float)) and chan.PART_SECONDS:
            hours = list(range(int(float(value) // chan.PART_SECONDS)))
        else:
            continue
        for hour in hours:
            for unit in range(hour * span, (hour + 1) * span):
                out.setdefault(vid, {}).setdefault(unit, 0)
    return out


def played(name, book=None, since=0):
    """The chunk numbers of this video that have already been on the wire.

    With since set, only what went out after that moment counts, which is how
    the draw falls back: everything older is treated as unseen again. Rows
    carried over from the old hour ledger have no usable timestamp of their
    own and read as aired at that hour's time, which is right.
    """
    book = ledger() if book is None else book
    rows = book.get(chan.video_id(name) or str(name), {})
    return {u for u, when in rows.items() if when >= since}


def reserved(folder=None):
    """{video id: {chunk number}} for everything cut but not yet sent.

    The cutter runs an hour ahead of the wire and nothing it cuts is spent
    until the feeder sends it, so without this the draw would hand it the
    same minutes twice. A skip deletes those chunks, which releases them.
    """
    out = {}
    alive = {q.name for q in (folder or chan.CHUNKS).glob("*.ts")}
    for name, row in chan.read_json(CHUNKMAP, {}).items():
        if name in alive and len(row) >= 4:
            out.setdefault(chan.video_id(row[0]) or row[0], set()).add(int(row[3]))
    return out


def remember_chunk(chunk, source, number, seconds, unit=0):
    """Tie a chunk to the hour and the minute it came from, and forget the sent."""
    data = chan.read_json(CHUNKMAP, {})
    data[chunk] = [source, number, seconds, unit]
    alive = {p.name for p in chan.CHUNKS.glob("*.ts")} | {chunk}
    chan.write_json(CHUNKMAP, {k: v for k, v in data.items() if k in alive})


def record_units(name, units, book=None):
    """Write down the chunks that have been on the wire. Called by the feeder.

    Once each: a resume walks the same branch a second time, and the feeder
    may see the same chunk twice if it restarts mid-send.
    """
    vid = chan.video_id(name) or str(name)
    seen = set(ledger().get(vid, {}) if book is None else book.get(vid, {}))
    fresh = [u for u in units if u not in seen]
    if not fresh:
        return
    chan.STATE.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    with UNITS.open("a") as fh:
        for unit in fresh:
            fh.write("%d\t%s\t%d\n" % (now, vid, unit))


def record_hour(name, number):
    """Spend a whole block without airing it. Only for a slice that failed to
    cut, so the draw does not come back to it for ever."""
    span = per_slice()
    record_units(name, range(number * span, (number + 1) * span))


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
    return max(0.0, seconds - len(played(pathlib.Path(path).name)) * unit_seconds())


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


def unaired(path, book, durations, held=None, since=0):
    """The chunks of this file that have never been on the wire and are not
    already cut and waiting to go. With since, chunks older than it count as
    unseen again."""
    seconds = chan.duration(path, durations)
    if not seconds:
        return set()
    vid = chan.video_id(path.name) or path.name
    taken = played(path.name, book, since) | set((held or {}).get(vid, ()))
    return set(range(units_in(seconds))) - taken


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
    held = reserved()
    for spent in chan.media(chan.QUEUE):
        if not unaired(spent, book, durations, held) and chan.duration(spent, durations):
            # through settle, not a bare move: aired.tsv is what keeps the id
            # out of the catalogue, and the feeder spends the last chunk long
            # after the job that cut it has finished, so this sweep is now the
            # only place a file is seen to be done.
            settle(spent, "queue")
            chan.log(f"entierement diffuse, passe en reserve: {spent.name[:60]}")
    just_played = last_recording(book)
    wanted = chan.read_json(PICK, {}).get("name")
    PICK.unlink(missing_ok=True)
    if wanted:
        for folder in (chan.QUEUE, chan.AIRED):
            path = folder / wanted
            if path.exists() and unaired(path, book, durations, held):
                chan.log(f"choix du chat: {wanted[:60]}")
                return claim(path, "chat")
        chan.log(f"choix du chat introuvable ou deja vu: {str(wanted)[:60]}")
    path, origin, since = draw_or_repeat(book, durations, just_played, held=held)
    REPLAY.unlink(missing_ok=True)
    if path is not None:
        if since:
            chan.write_json(REPLAY, {"name": path.name, "since": since})
        return claim(path, origin)

    # No third tier. kil, 2026-09-19: "tu me repasses PAS 2 fois le meme chunk
    # d'une heure". An hour that has been on the wire is spent for good, so when
    # the disk holds nothing unseen the answer is the standby clip and an alarm,
    # never the same hour again. The cure is supply, and watch.py is what says
    # the supply failed.
    return None, None


def draw(book, durations, just_played, avoid=(), held=None, since=0):
    """(path, origin) the draw would take, moving nothing. None when spent."""
    for folder, origin in ((chan.QUEUE, "queue"), (chan.AIRED, "reserve")):
        fresh = [p for p in chan.media(folder)
                 if p.name not in avoid and unaired(p, book, durations, held, since)
                 and not (chan.NO_KICK and not from_board(p))]
        if not fresh:
            continue
        # two turns in a row from the same stream read as a repeat whatever the
        # hours say, so another recording wins whenever one is available
        elsewhere = [p for p in fresh if recording_of(p) != just_played] or fresh
        return random.choice([p for p in elsewhere if from_board(p)] or elsewhere), origin
    return None, None


def draw_or_repeat(book, durations, just_played, avoid=(), held=None, now=None):
    """The draw, and what to fall back on when nothing on the disk is unseen.

    (path, origin, since). since is 0 while anything unseen is left, which is
    the normal case and the one that must never change. Below that the window
    opens: material older than REPEAT_AFTER is offered again, oldest first,
    and only if even that finds nothing does anything recent come back.

    The fallback lives here and not in window(), which is where it used to be
    and where it put hour 0 of a file back on the wire four hours after hour 0
    of the same file on 2026-09-19: down there it fired whenever one *file*
    was spent, with plenty unseen elsewhere. A repeat is only ever the right
    answer when the whole disk has nothing new on it.
    """
    now = time.time() if now is None else now
    path, origin = draw(book, durations, just_played, avoid, held)
    if path is not None:
        return path, origin, 0
    # One window and no last resort. A tier below this one would have to
    # accept something aired minutes ago, which is the complaint that started
    # all of this, and it would make the window mean nothing. If everything on
    # the disk is younger than REPEAT_AFTER the answer is the standby clip and
    # the alarm in watch.py, because that is supply having failed and it is a
    # thing to fix rather than to paper over.
    since = now - REPEAT_AFTER
    path, origin = draw(book, durations, just_played, avoid, held, since)
    if path is not None:
        chan.log(f"plus rien d'inedit, rediffusion de ce qui a plus de "
                 f"{REPEAT_AFTER // 86400} jours: {path.name[:52]}")
        return path, origin, since
    return None, None, 0


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


def window(source, seconds, number=None, since=0):
    """(start, length, slice number) of the hour to air now, drawn at random,
    or None when the file has nothing unseen left in it.

    kil, 2026-09-19: "tu pick aleatoirement dans la video, par exemple tu mets
    directement a 3h en plein milieu". So an hour is taken from anywhere in the
    file, not from where the last one stopped: the file is drawn at random and
    the hour inside it is drawn at random too.

    A minute already aired is not drawn again. When every minute of a file has
    been on the wire the file retires, which is what keeps a random draw from
    becoming a loop. Unsliced, the whole file is the one slice.

    The block is measured in chunks, not in hours, so a skipped hour leaves
    its unwatched minutes behind instead of burning them: they are still one
    run of consecutive chunks and come back as a shorter block later. The
    number returned is the first chunk, and everything downstream counts in
    chunks with it.

    The marks are nominal. A copy can only start on a keyframe, so a block can
    open up to one GOP early, about two seconds on this material. Making it
    exact would mean encoding.
    """
    if not chan.PART_SECONDS:
        return 0.0, seconds, 0
    total, span = units_in(seconds), per_slice()
    count = span
    if number is None:
        # No fallback to the least recently aired block. That branch is what
        # put hour 0 of one file back on the wire on 2026-09-19 at 16:03, four
        # hours after hour 0 of the same file, which is the one thing this
        # channel promises not to do. Nothing unseen means the file is spent,
        # and a spent file retires instead of going round again.
        free = sorted(set(range(total)) - played(source.name, since=since))
        if not free:
            return None
        runs, run = [], [free[0]]
        for unit in free[1:]:
            if unit == run[-1] + 1:
                run.append(unit)
            else:
                runs.append(run)
                run = [unit]
        runs.append(run)
        # a run long enough to fill a block beats a five minute offcut, so
        # those go first while any are left and the offcuts fill in at the end
        chosen = random.choice([r for r in runs if len(r) >= span] or runs)
        # and the block is taken from anywhere inside the run, not from its
        # start. kil, 2026-09-19: "tu pick aleatoirement dans la video, par
        # exemple tu mets directement a 3h en plein milieu". With one unseen
        # run covering the whole file, taking its head would open every file
        # at its first minute.
        steps = max(1, (len(chosen) - span) // span + 1)
        number = chosen[0] + random.randrange(steps) * span
        count = min(span, chosen[-1] + 1 - number)
    start = float(number * unit_seconds())
    # the last grain runs to the end of the file, stub included
    length = (seconds - start if number + count >= total
              else float(count * unit_seconds()))
    return start, max(0.0, length), number


def audio_for(info):
    """The audio arguments a chunk of this file needs. Used twice, so shared:
    a primer cut with different arguments than the job that follows it would
    change codec parameters at the junction, which is the one thing the muxer
    will not take."""
    return ["-c:a", "copy"] if chan.audio_copies(info) else         ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"]


def ready_set():
    """The hour held aside, or None when nothing usable is waiting.

    Validated against the files on disk rather than trusted: a stop in the
    middle of priming leaves the note without the chunks it names.
    """
    held = chan.read_json(READY_JOB, None)
    if not held or not held.get("chunks") or not held.get("source"):
        return None
    if not all((READY / name).exists() for name in held["chunks"]):
        return None
    return held


def clear_ready():
    for stale in READY.glob("*.ts"):
        stale.unlink(missing_ok=True)
    READY_JOB.unlink(missing_ok=True)


def prime():
    """Cut the head of another hour and hold it, so a skip is instant.

    Run only when the wire is an hour ahead and the cutter has nothing else to
    do, so it never competes with the job that is feeding the channel. The
    hour is not written to the ledger here: it is not on the wire, it may
    never be, and an hour marked played that nobody saw is a repeat in the
    other direction.
    """
    if ready_set():
        return
    clear_ready()
    book = ledger()
    durations = chan.read_json(chan.STATE / "durations.json", {})
    onair = chan.read_json(chan.STATE / "onair.json", {}).get("source")
    source, _, since = draw_or_repeat(book, durations, last_recording(book),
                                      avoid=(onair,) if onair else ())
    if source is None and onair:
        # the file on air is the only one with anything unseen left. Holding
        # another of its hours is not the change a skip is asking for, but it
        # beats the standby clip, which is the real alternative.
        source, _, since = draw_or_repeat(book, durations, last_recording(book))
    if source is None:
        return
    info = chan.probe(source)
    if info is None or chan.shape_problem(info):
        return
    drawn = window(source, info["seconds"], None, since)
    if drawn is None:
        return
    start, _, number = drawn
    READY.mkdir(parents=True, exist_ok=True)
    job = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-ss", f"{start:.3f}", "-t", f"{PRIMER_SECONDS:.3f}", "-i", str(source),
         "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy"] + audio_for(info)
        + chan.DROP_SEI
        + ["-f", "segment", "-segment_time", str(chan.CHUNK_SECONDS),
           "-segment_format", "mpegts", "-segment_list", str(READY / "list"),
           "-reset_timestamps", "1", str(READY / "ready_%05d.ts")],
        capture_output=True)
    names = completed(READY / "list")
    (READY / "list").unlink(missing_ok=True)
    kept = []
    for raw in names:
        piece = READY / raw
        if not piece.exists():
            continue
        chunk = f"{next_seq():010d}.ts"
        piece.replace(READY / chunk)
        kept.append(chunk)
    for leftover in READY.glob("ready_*.ts"):
        leftover.unlink(missing_ok=True)
    if not kept:
        chan.log(f"rien a tenir pret pour {source.name[:50]}: "
                 f"{job.stderr.decode(errors='replace')[-120:].strip()}")
        return
    chan.write_json(READY_JOB, {"source": source.name, "number": number,
                                "seconds": info["seconds"], "chunks": kept,
                                "at": int(time.time())})
    chan.log(f"tenu pret: {source.name[:50]} heure "
             f"{number // per_slice() + 1} (chunk {number}), {len(kept)} chunks")


def serve_ready():
    """Put the held hour on the wire this second. True when there was one.

    The hour is not written to the ledger here either: run_job does that when
    its own first chunk lands, which is the same branch it has always used and
    the one a restart resumes correctly.
    """
    held = ready_set()
    if not held:
        return False
    source, number = held["source"], held["number"]
    if number in played(source):
        clear_ready()  # drawn and aired the normal way while it sat here
        return False
    moved = []
    for name in held["chunks"]:
        piece = READY / name
        if not piece.exists():
            break
        piece.replace(chan.CHUNKS / name)
        remember_chunk(name, source, number, held["seconds"], number + len(moved))
        moved.append(name)
    clear_ready()
    if not moved:
        return False
    PICK.write_text(json.dumps({"name": source, "at": int(time.time())}))
    chan.write_json(PRIMED, {"source": source, "number": number, "done": len(moved)})
    chan.log(f"saut servi par l'heure tenue prete: {source[:50]} heure "
             f"{number // per_slice() + 1} (chunk {number}), "
             f"{len(moved)} chunks deja sur le fil")
    return True


def do_skip(reason, name=""):
    """Take the current hour off the wire and put the held one on.

    Everything waiting is dropped, which is the second the channel has nothing
    to send, so the held hour goes on in the same breath. Called from inside a
    cutting job and from the idle loop alike: with an hour of chunks ahead the
    cutter is idle most of the time, and a skip asked then used to sit unread
    until the next job started, which on 2026-09-19 was the whole reason a
    vote looked like it had done nothing.
    """
    for stale in sorted(chan.CHUNKS.glob("*.ts"))[SKIP_KEEP_CHUNKS:]:
        stale.unlink(missing_ok=True)
    if not serve_ready():
        chan.log("rien de tenu pret, le clip d'attente couvre le saut")
    FLUSH.write_text(str(int(time.time())))
    chan.log(f"passage saute ({reason}): {name[:60]}")


def run_job(source, origin, skip, number=None, since=0):
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
    drawn = window(source, info["seconds"], number, since)
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
                unit = number + moved
                if unit in played(source.name):
                    # the feeder has already sent this minute. The cutter runs
                    # an hour ahead and a resumed job recuts from its own start
                    # mark, so it can reach minutes the wire has passed in the
                    # meantime: on 2026-09-19 that queued four chunks of an
                    # hour already gone out. Reserved covers what is waiting,
                    # the ledger covers what has left, and this is the ledger
                    # half of the same guard.
                    piece.unlink(missing_ok=True)
                    moved += 1
                    continue
                chunk = f"{next_seq():010d}.ts"
                piece.replace(chan.CHUNKS / chunk)
                # nothing is written to the ledger here: a chunk is spent when
                # the feeder sends it, and one that a skip throws away was
                # never seen, so its minutes go back in the draw
                remember_chunk(chunk, source.name, number, info["seconds"], unit)
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
            do_skip(reason, source.name)
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
        done = played(source.name) | set(reserved().get(
            chan.video_id(source.name) or source.name, ()))
        total = units_in(info["seconds"])
        if len(done) < total:
            source.replace(chan.QUEUE / source.name)
            chan.log(f"fini {source.name}: {moved} chunks, {len(done)}/{total} heures diffusees")
            return True
    settle(source, origin)
    chan.log(f"fini {source.name}: {moved} chunks")
    return True


def recover():
    """What a stop left behind: a job to resume, or files to put back in line."""
    if not ready_set():
        clear_ready()
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
            if SKIP.exists():
                reason = chan.read_json(SKIP, {}).get("reason", "demande")
                SKIP.unlink(missing_ok=True)
                do_skip(reason, chan.read_json(chan.STATE / "onair.json",
                                               {}).get("source", ""))
                continue
            # an hour in hand: the spare minutes go into holding the head of
            # another hour ready, which is what makes a skip instant
            prime()
            time.sleep(POLL)
            continue
        source, origin = next_source()
        if source is None:
            time.sleep(30)
            continue
        replay = chan.read_json(REPLAY, {})
        since = replay.get("since", 0) if replay.get("name") == source.name else 0
        primed = chan.read_json(PRIMED, {})
        PRIMED.unlink(missing_ok=True)
        if primed.get("source") == source.name:
            if not run_job(source, origin, int(primed.get("done", 0)),
                           int(primed["number"])):
                source.replace((chan.QUEUE if origin == "queue" else chan.AIRED)
                               / source.name)
                time.sleep(60)
            continue
        if not run_job(source, origin, 0, None, since):
            # could not measure: back where it came from, and give the box a minute
            source.replace((chan.QUEUE if origin == "queue" else chan.AIRED) / source.name)
            time.sleep(60)


if __name__ == "__main__":
    main()
