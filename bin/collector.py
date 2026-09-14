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
import random
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
# Combien de fois une cle a ete remise a la carte sans jamais arriver.
LOST_FILE = common.STATE / "lost.json"

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
# How many fetches the board may hold at once, which is the only throttle on
# how fast the fetching machine pulls from the source.
#
# Filled to 8 on 2026-09-14, it pulled nine videos back to back, about 28 Go
# in ten hours, and the source started refusing every request with a bot
# check at 13:23, minutes after a 4.27 Go download completed. Nothing was
# wrong with the downloader: its version was current and every player client
# was refused alike, so it was the address that had been flagged, by a burst
# this depth allowed.
#
# A channel eats about 24 hours of video a day and that has to be fetched
# whatever the depth, so the throttle is not about volume, it is about shape:
# a shallow board spaces the same fetches out instead of bursting them.
BOARD_QUEUE_DEPTH = int(os.environ.get("VODLOOP_BOARD_DEPTH") or 3)
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

# The shortest thing this channel will fetch at all, and a hard floor rather
# than a preference. Unset, the two numbers above decide and the channel gets
# many short videos, which is the widest rotation a fixed disk can hold.
#
# A channel built on whole days wants the opposite and says so here. Measured
# 2026-09-13 on the second channel, which had been left with the defaults: the
# two videos the collector chose for it were 38 and 21 minutes, because the
# urgent path asks for the SHORTEST candidate above MIN_USEFUL_SECONDS and the
# band above caps at sixty minutes. Both rules were working exactly as written
# and both were wrong for that channel.
#
# An unmeasured duration cannot clear this. Fetching a video that might be
# forty minutes is not honouring a floor, it is guessing.
MIN_SECONDS = int(os.environ.get("VODLOOP_MIN_SECONDS") or 0)
# Draw inside a source at random instead of reading its listing top down. A
# listing is newest first, so in order means the same recent evenings every
# pass, whatever else is in the archive: 369 videos of which the channel only
# ever sees the newest handful.
RANDOM_PICK = bool(os.environ.get("VODLOOP_RANDOM_PICK"))
# The longest thing the board can bring back WHOLE, which is the only way it
# brings anything back at all.
#
# Measured 2026-09-14 over the board's whole history: 55 successful deliveries,
# none of them a part, against 5 attempts at one and 10 HTTP 403 from
# googlevideo. --download-sections hands the ranged request to ffmpeg, which
# makes it without yt-dlp's headers, and YouTube refuses it. Whole videos were
# landing the same hour, so this is not the anti-bot wall, it is the ranged
# path specifically.
#
# So a channel asking for long videos has to ask for ones that fit whole. The
# board aborts past MAX_FILESIZE=4G and this material measures 1.16 Go/h at
# 1080p, which puts the ceiling near three and a half hours. Unset, nothing is
# refused for being long, which is what a channel taking parts wants.
MAX_SECONDS = int(os.environ.get("VODLOOP_MAX_SECONDS") or 0)
# A key handed to the board and still absent from both the library and what
# prep has played did not arrive. It is not "offered recently", it is a
# failure, and the refetch window was never meant for failures: it exists so a
# video that PLAYED is not fetched again straight away.
#
# Left alone the two meanings collapse and the pool shrinks on every bad day
# the board has. Measured 2026-09-14 on the second channel: seven of the
# fifteen entries in the ledger were keys that never landed, held out of the
# draw for twenty one days each. Nobody would ever have noticed: the channel
# simply had fewer and fewer things it was allowed to ask for.
LOST_AFTER_SECONDS = 6 * 3600
# How many times a key may be lost before it is left alone for the full
# window. Without it a key the board cannot fetch at all comes back every run,
# and the board spends its day on six doomed attempts per offer. Three is
# enough to ride out a wall day and short enough that a dead video stops
# costing anything.
MAX_LOSSES = 3

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


def played_keys():
    """Keys that reached the library at some point, from what prep has played.

    A file still in the library is held; one that played and was retired is
    gone from both, and only prep's history remembers it existed. Without this
    a retired video would look exactly like one that never arrived.
    """
    seen = set()
    for path in prep.load_history():
        key = key_of(pathlib.Path(path))
        if key:
            seen.add(key)
    return seen


def free_the_lost(handed, held, now):
    """Give back to the draw every key the board was asked for and never
    delivered, and stop asking for the ones it keeps failing to deliver.

    Returns the keys freed, having already written both ledgers: the next run
    has to see this even if this one dies on the line after.
    """
    landed = held | played_keys()
    losses = load(LOST_FILE, {})
    freed = []
    for key, at in list(handed.items()):
        if key in landed or now - at <= LOST_AFTER_SECONDS:
            continue
        count = losses.get(key, 0) + 1
        losses[key] = count
        if count < MAX_LOSSES:
            handed.pop(key, None)
            freed.append(key)
    if freed or losses:
        # On garde les comptes AU plafond: ce sont eux qui bloquent. Les purger
        # remettait le compteur a zero et la cle repartait pour trois pertes,
        # donc le plafond ne plafonnait rien. Seules les cles finalement
        # arrivees oublient leur ardoise.
        save(LOST_FILE, {k: n for k, n in losses.items() if k not in landed})
        save(HANDED_FILE, handed)
    return freed


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
    floor = max(MIN_USEFUL_SECONDS, MIN_SECONDS)
    worth = [(secs, key) for secs, key in rated
             if secs >= floor and (not MAX_SECONDS or secs <= MAX_SECONDS)]
    # Nothing clears the floor: better a short video than none, EXCEPT when the
    # floor was set deliberately. An operator asking for an hour minimum is
    # stating a contract, and quietly serving forty minutes because the pool
    # looked thin would break it in the one place nobody would look.
    return [key for _, key in (worth if MIN_SECONDS else (worth or rated))[:count]]


def pick(count, known, shortest=False, cat=None):
    """Up to count keys, one from each source in turn until the count is met.

    A key is a video id, or an id and its part when the video is too long to
    be fetched whole. Either way it is one thing the board can be asked for.

    Round robin rather than in order. Draining the first playlist before
    touching the second would put days of one thing on air and then days of
    another, which reads as a much smaller library than it is.
    """
    lists, durations, _ = cat if cat else catalogue()

    # The floor comes first, so everything after it chooses among things that
    # already clear it. Applied later it would be a filter on a decision
    # already made, which is how a rule ends up looking enforced and not being.
    if MIN_SECONDS or MAX_SECONDS:
        lists = [[k for k in keys
                  if (durations.get(k) or 0) >= MIN_SECONDS
                  and (not MAX_SECONDS or (durations.get(k) or 0) <= MAX_SECONDS)]
                 for keys in lists]

    if RANDOM_PICK:
        # Shuffled once per pass, then read by the same round robin below, so
        # each source still takes its turn and what it offers on that turn is a
        # draw. Neither the shortest-first rule nor the band applies here: both
        # exist to bias the choice, and this channel asked for no bias.
        lists = [random.sample(keys, len(keys)) for keys in lists]
    elif shortest:
        chosen = pick_shortest(count, known, lists, durations)
        if chosen:
            return chosen
        # nothing in the pool has a measured duration yet. Falling through to the
        # round robin queues something rather than nothing, and queueing nothing
        # is the one outcome a channel that is running dry cannot afford
    elif not (MIN_SECONDS or MAX_SECONDS):
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
    # Autonomous repair, before anything reads the ledger: a channel that is
    # not being resupplied has to widen what it may ask for by itself. There is
    # nobody to tell, the machine that would be told can be switched off, and
    # the only two things running are this box and the board.
    freed = free_the_lost(handed, library_ids(), now)
    if freed:
        print(f"{len(freed)} cle(s) remise(s) et jamais arrivee(s), rendue(s) au tirage")

    cooldown = REFETCH_SECONDS or HANDED_COOLDOWN_SECONDS
    recent = {k for k, at in handed.items() if now - at < cooldown}
    # Still on its way, which is a different question from may it be offered
    # again. A key handed over has either landed or failed within the six
    # hours below; after that it is not in flight whatever the refetch rule
    # says. Measured 2026-09-14 with REFETCH_DAYS=21: eight keys that had been
    # dropped from the board's queue and would never arrive were still being
    # charged to the share three weeks later, and the channel could not use
    # what it had been given.
    in_flight = {k for k, at in handed.items()
                 if now - at < HANDED_COOLDOWN_SECONDS}
    runway = prep.runway_seconds()
    urgent = runway < URGENT_RUNWAY_SECONDS
    board = board_queue(now)
    budget = common.BUDGET_BYTES
    used = common.bytes_used(LIBRARY) if budget else 0

    # Ce qui est facture a la part, pas ce qui a ete remis: la carte plafonne le
    # compte plus bas, et afficher le brut annoncait onze quand trois etaient
    # payees, ce qui envoie lire le mauvais chiffre pendant une panne.
    charged = len(in_flight - held)
    if board is not None:
        charged = min(charged, board)
    share = (f" part={used / 1024 ** 3:.1f}/{budget / 1024 ** 3:.1f}G"
             if budget else "")
    print(f"bibliotheque={have}/{TARGET_LIBRARY_FILES} libre={free / 1024 ** 3:.1f}G"
          f"{share} sources={len(sources())} en_vol={charged} "
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
        # The board says how many it is holding, and that is the only honest
        # count of what is still coming. A key handed over and absent from that
        # count has landed, or failed, or been taken out of its queue by hand,
        # and charging it reserves bytes for a file that will never arrive.
        #
        # Measured 2026-09-14: eleven keys inside the six hour window against a
        # board reporting three, so nineteen gigabytes were reserved out of an
        # eighteen gigabyte share and the channel refused every run while its
        # library sat at three files. The newest are kept, being the ones the
        # board has not had time to finish.
        flying = [k for k, _ in sorted(((k, handed[k]) for k in in_flight - held),
                                       key=lambda kv: -kv[1])]
        if board is not None:
            flying = flying[:board]
        for key in flying:
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
