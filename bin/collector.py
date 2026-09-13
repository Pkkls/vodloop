#!/usr/bin/env python3
"""Keep the download inbox fed, so the library never has to run dry. From cron.

Everything downstream of this already worked: the board claims inbox.txt from
Oracle, fetches, and uploads to the library. What never existed was anything
putting URLs in that inbox. It was filled by hand through the dashboard, so when
nobody filled it the pipeline sat at queue=0, which it had done for two days by
2026-09-05 while the channel kept consuming 24 hours of video a day.

Sources are taken in turn rather than in order. Draining the first playlist
before touching the second would give days of one thing and then days of
another, which on a rerun channel reads as a much smaller library than it is.

    python3 bin/collector.py            report what it would queue
    python3 bin/collector.py --apply    append to the inbox

What it queues depends on how much air time is left. Above the urgent line it
takes the pool as it comes, which keeps the rotation varied. Below it, the only
thing worth fetching is whatever puts the channel back on its feet soonest, so
it takes the shortest candidates it can measure: acquisition runs several times
faster than playback, so a 30 min video is on the shelf in a few minutes while a
12 h one is not there for half an hour.

How deep it queues depends on the board. The board drains one URL per five
minute tick and always takes the oldest line, so a deep backlog is a stack of
decisions made hours ago, before the runway was what it is now. Topping up to a
shallow depth is what keeps the choice made here the choice that gets fetched.

Three things bound it. It stops when the library is big enough or the board is
stocked, because queueing past either only fills a disk the janitor then has to
empty. And it does not requeue anything already in the library, already waiting
in the inbox, or handed over recently: a video in flight is in none of those
first two, having been claimed off the inbox, so without the third it would be
fetched twice.
"""
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import common
import prep

LIBRARY = common.LIBRARY_DIR
# The board serves every channel from one inbox per library: inbox.txt for the
# first, inbox-<name>.txt for a library named videos-<name>.
INBOX = pathlib.Path(os.environ.get("VODLOOP_INBOX") or "/home/ubuntu/yt2oracle/inbox.txt")
# What the board publishes about itself every five minutes. It is the only way
# this side can see how deep the far side's queue is: the board is behind NAT
# and nothing here can ask it anything.
STATUS_FILE = pathlib.Path("/home/ubuntu/yt2oracle/status.json")
YTDLP = shutil.which("yt-dlp") or "/home/ubuntu/.local/bin/yt-dlp"

SOURCES_FILE = common.STATE / "sources.json"
POOL_FILE = common.STATE / "pool.json"
HANDED_FILE = common.STATE / "handed.json"
# Ids dont la source elle-meme est defectueuse. Ecrit par prep, lu ici.
UNUSABLE_FILE = common.STATE / "unusable.json"

# A ceiling, not the real limit: disk is. Set above what the disk can hold so
# the equilibrium is decided by free space, which is the thing that actually
# stops prep, rather than by a file count guessed here.
TARGET_LIBRARY_FILES = 90
# One run should top up, not flood: the board fetches these one at a time.
MAX_PER_RUN = 8
# Listing a 4000 video channel takes minutes, and it does not change by the
# hour. Re-listed once a day.
POOL_TTL_SECONDS = 24 * 3600
# A video claimed off the inbox is in neither the inbox nor the library while it
# downloads. Long enough to cover that, short enough that a failed one is
# retried the same day.
HANDED_COOLDOWN_SECONDS = 6 * 3600
# Deliberately above the janitor's 8G target. Overlapping the two would have
# them fight: the collector filling down to the line the janitor then clears,
# every quarter of an hour, forever. Leaving a band between them means the
# collector only adds when there is room the janitor is not about to reclaim.
# Below the janitor's target, or the two of them deadlock and the library can
# never grow again. Measured 2026-09-08: this was 10 Go while the janitor
# stopped retiring at 5 Go free, so free space settled just above 5 and this
# refused every single run. The library had not gained a file since 05
# September and the channel was replaying the same 21 hours, which is the
# complaint that started all of this. The janitor now frees to 10 and this
# fires below 8, so there is always a gap the collector can work in.
MIN_FREE_BYTES = 8 * 1024 ** 3
# How many URLs the board may be sitting on before this stops adding. Two rates
# set it: the board finishes roughly one fetch every ten minutes, and this runs
# every thirty. Four left the board idle between passes; eight is about eighty
# minutes of work in hand, so a missed run or a string of refusals cannot empty
# it, and it is still shallow enough that a choice made now is fetched within
# the hour rather than behind a day of older ones.
#
# Deep is not free. A 49 entry queue built before the variety rule existed held
# 35 subathons of two hours and more and blocked every later decision for days,
# which is the state that had the channel looping one video on 2026-09-09.
BOARD_QUEUE_DEPTH = 8
# The board publishes its state on a five minute tick. Past this the file is not
# reporting the board, it is reporting the last time the board could be reached,
# and a count read off it would be a guess. Then this falls back to the depth
# rule it used before there was a board to ask.
STATUS_STALE_SECONDS = 45 * 60
# Under this much air time left, variety stops being the point and speed of
# recovery is. Four hours is roughly a quarter of a day: far enough above the
# one hour floor that prep never has to refuse a deletion, far enough below a
# full library that this is not permanently in a hurry.
URGENT_RUNWAY_SECONDS = 4 * 3600
# Below this a video is not worth fetching at all. Measured on the board: a run
# costs about ten minutes end to end whatever it returns, and the download
# itself moves at several times real time, so the time a fetch adds to the
# runway is roughly its duration minus that fixed cost. Twenty minutes returns
# about double what it costs; seventeen seconds returns nothing and spends the
# same ten minutes, which is how the library gained one minute of air across
# three complete cycles on 2026-09-09.
MIN_USEFUL_SECONDS = 20 * 60
# What to prefer once the channel is not starving, and it is a different
# question. The disk holds about the same number of HOURS whatever is on it, so
# what a band changes is how many distinct videos those hours are: measured on
# this pool of 4570, four hours and over gives five videos on the disk, twenty
# to sixty minutes gives fifty five, five to twenty minutes gives two hundred.
# Nearly half the pool is four hours or more, so taking it as it comes builds
# the poorest rotation available.
#
# Below the band a fetch is confetti, above it one fetch buys one video and
# fills the disk with it. In between there are 574 candidates, which is more
# than the disk can hold, so the band never runs the collector dry.
VARIETY_MIN_SECONDS = 5 * 60
VARIETY_MAX_SECONDS = 60 * 60

# A channel whose sources are mostly streams of four to eleven hours cannot take
# them whole: the board's card caps a file at 4 Go, and at 720p that is under
# five hours, so 252 of the 369 videos of such a source would fall back to 480p
# or 360p, and prep refuses 360p. Set, a video longer than this is queued as
# parts of at most this long, each fetched on its own through
# --download-sections. Unset, every video is queued whole, as before.
PART_SECONDS = int(os.environ.get("VODLOOP_PART_SECONDS") or 0)
# the tallest picture the board is asked for; unset keeps its own ladder
MAX_HEIGHT = int(os.environ.get("VODLOOP_MAX_HEIGHT") or 0)
# How long a video handed to the board stays out of the draw. The six hour
# cooldown is right for a pool of thousands of short videos, where a retired
# file coming back is a rerun. For a pool of long streams cut in parts it is a
# loop: part one plays, is retired, and is fetched again before part two ever
# comes. Unset keeps the cooldown.
REFETCH_SECONDS = float(os.environ.get("VODLOOP_REFETCH_DAYS") or 0) * 86400
# With a share of the disk, stop this far under it: the files already handed to
# the board are counted at their estimated size, and the estimate can be short.
BUDGET_HEADROOM_BYTES = int(1.5 * 1024 ** 3)
# bytes per second assumed for a video when the library has nothing measured
DEFAULT_RATE = 250_000
PART_KEY = re.compile(r"-([A-Za-z0-9_-]{11})(?:\.(p\d+of\d+))?\.(?:mp4|mkv)$")

# The optional part suffix is not decoration. A stream too long for the board's
# card arrives as <title>-<id>.p02of04.mp4, and without this the id is not read
# back off those names at all: library_ids() would come back empty, every video
# already downloaded would look absent, and this would queue the whole pool
# again on every run while the disk filled.
VIDEO_ID = re.compile(r"-([A-Za-z0-9_-]{11})(?:\.p\d+of\d+)?\.(?:mp4|mkv)$")

# prep reads its library out of VODLOOP_LIBRARY, which the unit file sets and
# this module's crontab line does not. Left alone, runway_seconds() then counts
# only the chunks already cut and misses every unplayed file: measured
# 2026-09-08, 1500 s against the true 3097 s, so every run read as urgent and the
# unhurried branch below could never be reached. Pointing prep at the directory
# this module already knows about is one line, and unlike a third copy of the
# path in a crontab it cannot drift out of step with the two above it.
prep.LIBRARY = LIBRARY


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(path, data):
    common.STATE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def sources():
    """Declared sources, each a url and an optional list of title keywords.

    A plain string stays a whole source. An entry with "match" takes only the
    videos whose title contains one of the words, which is how a general
    archive channel contributes the part of itself that belongs here without
    dragging in everything else it holds.
    """
    out = []
    for entry in load(SOURCES_FILE, []):
        if isinstance(entry, str):
            out.append({"url": entry, "match": []})
        elif isinstance(entry, dict) and entry.get("url"):
            out.append({"url": entry["url"],
                        "match": [w.lower() for w in entry.get("match", [])]})
    return out


def cache_key(source):
    # the filter is part of the identity: changing the words has to invalidate
    # the list, or a narrowed source keeps serving what it used to match
    return source["url"] + ("|" + ",".join(source["match"]) if source["match"] else "")


def library_ids():
    """Video ids already downloaded. yt-dlp names files <title>-<id>.<ext>, so
    the id is read back off the name rather than kept in a second ledger that
    could disagree with the directory."""
    if not LIBRARY.is_dir():
        return set()
    found = set()
    for path in LIBRARY.iterdir():
        match = PART_KEY.search(path.name)
        if match:
            found.add(match.group(1))
            if match.group(2):
                found.add(f"{match.group(1)}.{match.group(2)}")
    return found


def key_of(path):
    """The key a library file was fetched under: its id, or id.pNNofMM."""
    match = PART_KEY.search(pathlib.Path(path).name)
    if not match:
        return None
    return f"{match.group(1)}.{match.group(2)}" if match.group(2) else match.group(1)


def inbox_ids():
    try:
        text = INBOX.read_text()
    except OSError:
        return set()
    found = set(re.findall(r"[A-Za-z0-9_-]{11}", text))
    # a part line names its video and its place in it, and the place is the key
    for vid, part in re.findall(r"v=([A-Za-z0-9_-]{11})\S*\s+part=(p\d+of\d+)", text):
        found.add(f"{vid}.{part}")
    return found


def parts_of(vid, secs):
    """The keys a video is fetched under, each with its range in seconds.

    A video that fits in one part, or whose length is unknown, is one key, the
    id itself, with no range: fetched whole, exactly as before parts existed.
    """
    if not PART_SECONDS or not secs or secs <= PART_SECONDS:
        return [(vid, None)]
    count = math.ceil(secs / PART_SECONDS)
    step = secs / count
    return [(f"{vid}.p{k:02d}of{count:02d}", (round((k - 1) * step), round(k * step)))
            for k in range(1, count + 1)]


def catalogue():
    """Each source's keys in order, with every key's duration and part range."""
    cached = pool()
    lists, durations, spans = [], {}, {}
    for source in sources():
        entry = cached.get(cache_key(source), {})
        measured = entry.get("dur", {})
        keys = []
        for vid in entry.get("ids", []):
            for key, span in parts_of(vid, measured.get(vid)):
                keys.append(key)
                if span:
                    spans[key] = span
                    durations[key] = span[1] - span[0]
                elif vid in measured:
                    durations[key] = measured[vid]
        lists.append(keys)
    return lists, durations, spans


def line_for(key, spans):
    """The inbox line for a key. A whole video without a height is the bare URL
    the board has always been given."""
    vid, _, part = key.partition(".")
    line = f"https://www.youtube.com/watch?v={vid}"
    if part:
        start, end = spans[key]
        line += f" part={part} range={start}-{end}"
    if MAX_HEIGHT:
        line += f" h={MAX_HEIGHT}"
    return line


def library_rate(durations):
    """Bytes per second of what the library actually holds, measured on the
    files whose duration the listing already gave. Used to size what is still
    on its way, so a share of the disk is not overrun by a batch of arrivals."""
    size = secs = 0
    if LIBRARY.is_dir():
        for path in LIBRARY.iterdir():
            key = key_of(path)
            if key and durations.get(key):
                try:
                    size += path.stat().st_size
                except OSError:
                    continue
                secs += durations[key]
    return size / secs if secs >= 3600 else DEFAULT_RATE


def estimate(key, durations, rate):
    """What a candidate is expected to weigh once it lands.

    An unmeasured video is not free. Charged nothing it would walk past the
    share unnoticed and a pool with no durations at all would queue until the
    disk said stop, which is the one thing a share exists to prevent. A part's
    worth, or an hour, is wrong in the safe direction.
    """
    return (durations.get(key) or PART_SECONDS or 3600) * rate


def unusable_ids():
    """Videos whose source itself is defective, and that must never be fetched
    again.

    Some uploads carry video packets with no PTS at all. Copied into MPEG-TS
    they look fine, and the flv muxer at the far end refuses the first one and
    takes the pusher down with it. prep refuses them on purpose, which is right,
    but nothing stopped the collector offering the same id once the file left
    the library: on 2026-09-10 one was downloaded a second time, 2,8 Go and a
    full cycle of a board that manages a handful of fetches a day, and the fresh
    copy carried exactly the same five bad packets in its first thirty seconds.
    The defect is upstream, so no amount of re-fetching will change it.

    A missing or unreadable file means no exclusion, which is the safe
    direction: at worst a video is fetched again, never one silently lost.
    """
    return set(load(UNUSABLE_FILE, []))


def list_source(source):
    """Ids and durations for one source, keeping matching titles when asked.

    The duration comes out of the same flat listing as the id, so knowing how
    long every candidate is costs nothing beyond one more field. Asking a board
    with one RISC-V core for it, one video at a time, would have cost minutes it
    owes the downloads instead.
    """
    out = subprocess.run(
        [YTDLP, "--flat-playlist", "--no-warnings", "--print",
         "%(id)s	%(duration)s	%(title)s", source["url"]],
        capture_output=True, text=True, timeout=900)
    keep, durations = [], {}
    for line in out.stdout.splitlines():
        parts = line.split("	", 2)
        if len(parts) != 3:
            continue
        vid, secs, title = parts
        vid = vid.strip()
        if len(vid) != 11:
            continue
        if source["match"] and not any(w in title.lower() for w in source["match"]):
            continue
        keep.append(vid)
        # a live or hidden entry prints NA. Left out rather than stored as zero,
        # which would read as the shortest video in the pool and be picked first
        try:
            durations[vid] = int(float(secs))
        except ValueError:
            pass
    return keep, durations


def pool():
    """Video ids and durations per source, cached because listing is slow."""
    cached = load(POOL_FILE, {})
    now = time.time()
    for source in sources():
        key = cache_key(source)
        entry = cached.get(key)
        # a listing from before durations were recorded is fresh by its own
        # clock and useless to the picker, so it is re-listed like a stale one
        if entry and "dur" in entry and now - entry.get("at", 0) < POOL_TTL_SECONDS:
            continue
        try:
            ids, durations = list_source(source)
        except (OSError, subprocess.SubprocessError):
            continue
        # an empty listing is a failed listing, not an empty source: keeping the
        # previous one is better than forgetting a source because YouTube
        # hiccuped once
        if ids:
            cached[key] = {"at": now, "ids": ids, "dur": durations}
    save(POOL_FILE, cached)
    return cached


def pick_shortest(count, known, lists, durations):
    """Up to count keys: the shortest candidates that still pay for themselves.

    Round robin is abandoned here on purpose. When air time is short the
    question is no longer which source deserves a turn, it is which video is
    back on the shelf soonest, and that is a property of the pool rather than of
    any one list. An unmeasured duration is not treated as a short video: it is
    left out, because guessing wrong in this direction queues a twelve hour
    subathon in front of a channel that has thirty minutes left.

    The floor is the part that was missing, and leaving it out was a trap that
    ran for two days. A fetch costs the board about ten minutes whatever it
    brings back: metadata, download, merge, upload, verify. Sorting on duration
    alone therefore asks for the videos with the worst possible return, and it
    got exactly that: three clips of 17, 26 and 17 seconds, one minute of air
    for three full cycles. The channel could not climb out, because every pass
    left the runway low, which kept it urgent, which asked for more clips.

    So the shortest candidate is chosen from those long enough to be worth the
    trip. Below the floor a fetch is a net loss of runway, not a small gain.
    """
    candidates = {key for keys in lists for key in keys} - set(known)
    rated = sorted((durations[k], k) for k in candidates if durations.get(k))
    worth = [(secs, key) for secs, key in rated if secs >= MIN_USEFUL_SECONDS]
    # nothing in the pool clears the floor: better a short video than none
    return [key for _, key in (worth or rated)[:count]]


def pick(count, known, shortest=False, cat=None):
    """Up to count keys, one from each source in turn until the count is met.

    A key is a video id, or an id and its part when the video is too long to
    be fetched whole. Either way it is one thing the board can be asked for.

    Round robin rather than in order. Draining the first playlist before
    touching the second would put days of one thing on air and then days of
    another, which reads as a much smaller library than it is.
    """
    lists, durations, _ = cat if cat else catalogue()
    if shortest:
        chosen = pick_shortest(count, known, lists, durations)
        if chosen:
            return chosen
        # nothing in the pool has a measured duration yet. Falling through to the
        # round robin queues something rather than nothing, and queueing nothing
        # is the one outcome a channel that is running dry cannot afford
    else:
        # Not starving, so the question is variety rather than air time. Each
        # source keeps its turn; what is filtered is what that turn may offer.
        # A source with nothing in the band keeps its whole list, so narrowing
        # can never silence a source altogether.
        banded = [[k for k in keys
                   if VARIETY_MIN_SECONDS <= (durations.get(k) or 0) < VARIETY_MAX_SECONDS]
                  or keys
                  for keys in lists]
        lists = banded
    cursors = [0] * len(lists)
    chosen = []
    while len(chosen) < count:
        progressed = False
        for n, keys in enumerate(lists):
            if len(chosen) >= count:
                break
            # each source keeps its place, so a later pass carries on rather
            # than rescanning the keys it already rejected
            while cursors[n] < len(keys):
                key = keys[cursors[n]]
                cursors[n] += 1
                if key not in known and key not in chosen:
                    chosen.append(key)
                    progressed = True
                    break
        if not progressed:
            break
    return chosen


def board_queue(now=None):
    """How many URLs the board holds for THIS library, or None if it has not
    said lately.

    One board serves every channel, from one inbox each. A single total would
    have each channel read its neighbour's backlog as its own and stop queueing
    while its own inbox sat empty, so the per library counts are preferred. The
    board only learned to publish them later than the total, so the total is
    still read when it is all there is.

    None is the honest answer to a stale file and it is treated as one: the
    caller falls back to its own depth rule rather than reading a count from a
    board that may have been unreachable for a day.
    """
    status = load(STATUS_FILE, None)
    if not isinstance(status, dict):
        return None
    age = (time.time() if now is None else now) - status.get("at", 0)
    if age > STATUS_STALE_SECONDS:
        return None
    queues = status.get("queues")
    if isinstance(queues, dict):
        # a board that reports its queues and does not mention this library is
        # holding nothing of ours, which is a measurement and not an unknown
        value = queues.get(LIBRARY.name, 0)
    elif "queue" in status:
        value = status["queue"]
    else:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def main(argv):
    apply = "--apply" in argv
    free = shutil.disk_usage(LIBRARY).free
    held = library_ids()
    # Files, not keys: a part file answers to both its id and its part key, so
    # counting keys would read a library of parts as twice the size it is and
    # stop fetching at half the ceiling.
    have = sum(1 for p in LIBRARY.iterdir() if key_of(p)) if LIBRARY.is_dir() else 0
    handed = load(HANDED_FILE, {})
    now = time.time()
    # A part is fetched, played and retired while its neighbours are still to
    # come, so on a source cut into parts the cooldown has to outlive the whole
    # stream: at six hours the channel loops on part one and never reaches two.
    cooldown = REFETCH_SECONDS or HANDED_COOLDOWN_SECONDS
    recent = {k for k, at in handed.items() if now - at < cooldown}
    runway = prep.runway_seconds()
    urgent = runway < URGENT_RUNWAY_SECONDS
    board = board_queue(now)
    budget = common.BUDGET_BYTES
    used = common.bytes_used(LIBRARY) if budget else 0

    share = (f" part={used / 1024 ** 3:.1f}/{budget / 1024 ** 3:.1f}G"
             if budget else "")
    print(f"bibliotheque={have}/{TARGET_LIBRARY_FILES} libre={free / 1024 ** 3:.1f}G"
          f"{share} sources={len(sources())} en_vol={len(recent)} "
          f"antenne={runway / 3600:.1f}h{' URGENT' if urgent else ''} "
          f"carte={'?' if board is None else board}/{BOARD_QUEUE_DEPTH}")

    # With a share, free space is no longer this channel's own measure: the
    # neighbour's arrivals move it, and refusing on the 8 Go line would have
    # whichever channel is second to fill simply stop for good. What bounds
    # this one is its share, and free space only as the floor the janitor
    # frees to, which both channels leave alone.
    if budget:
        room = min(budget - BUDGET_HEADROOM_BYTES - used,
                   free - common.SHARED_FREE_FLOOR_BYTES)
        if room <= 0:
            print("part servie ou disque trop juste: rien ajoute")
            return 0
    elif free < MIN_FREE_BYTES:
        print("disque trop juste, le concierge travaille: rien ajoute")
        return 0

    want = min(TARGET_LIBRARY_FILES - have, MAX_PER_RUN)
    if board is not None:
        want = min(want, BOARD_QUEUE_DEPTH - board)
    if want <= 0:
        print("bibliotheque pleine ou carte deja servie, rien a faire")
        return 0

    cat = catalogue()
    durations, spans = cat[1], cat[2]
    known = held | inbox_ids() | recent | unusable_ids()
    chosen = pick(want, known, shortest=urgent, cat=cat)
    if not chosen:
        print("rien de nouveau dans les sources")
        return 0

    if budget:
        # What is already on its way counts against the share too. It is in
        # neither the library nor the inbox, having been claimed off it, so
        # without this a run queues into space the previous run already spent.
        rate = library_rate(durations)
        for key in recent - held:
            room -= estimate(key, durations, rate)
        keep = []
        for key in chosen:
            cost = estimate(key, durations, rate)
            if cost > room:
                break
            keep.append(key)
            room -= cost
        if not keep:
            print("la part ne laisse pas la place d'un fichier de plus: "
                  "rien ajoute")
            return 0
        chosen = keep

    for key in chosen:
        print(f"  {'ajoute' if apply else 'ajouterait'} {line_for(key, spans)}")
    if apply:
        INBOX.parent.mkdir(parents=True, exist_ok=True)
        with INBOX.open("a", encoding="utf-8") as fh:
            for key in chosen:
                fh.write(line_for(key, spans) + "\n")
        for key in chosen:
            handed[key] = now
        save(HANDED_FILE, {k: at for k, at in handed.items()
                           if now - at < max(7 * 24 * 3600, REFETCH_SECONDS)})
        print(f"{len(chosen)} URL(s) deposee(s) dans l'inbox")
    else:
        print("essai a blanc, l'inbox n'a pas ete touchee. --apply pour agir.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
