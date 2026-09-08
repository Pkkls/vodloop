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
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import common
import prep

LIBRARY = pathlib.Path("/home/ubuntu/videos")
INBOX = pathlib.Path("/home/ubuntu/yt2oracle/inbox.txt")
# What the board publishes about itself every five minutes. It is the only way
# this side can see how deep the far side's queue is: the board is behind NAT
# and nothing here can ask it anything.
STATUS_FILE = pathlib.Path("/home/ubuntu/yt2oracle/status.json")
YTDLP = shutil.which("yt-dlp") or "/home/ubuntu/.local/bin/yt-dlp"

SOURCES_FILE = common.STATE / "sources.json"
POOL_FILE = common.STATE / "pool.json"
HANDED_FILE = common.STATE / "handed.json"

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
# How many URLs the board may be sitting on before this stops adding. The board
# does one at a time and a real video costs it fifteen to forty minutes, so four
# is already a couple of hours of work in hand: enough that a missed run or a
# failed fetch does not starve it, shallow enough that a choice made now is
# fetched within the hour instead of behind a day of older ones.
BOARD_QUEUE_DEPTH = 4
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
        match = VIDEO_ID.search(path.name)
        if match:
            found.add(match.group(1))
    return found


def inbox_ids():
    try:
        return set(re.findall(r"[A-Za-z0-9_-]{11}", INBOX.read_text()))
    except OSError:
        return set()


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
    """Up to count ids, shortest first, from the whole pool at once.

    Round robin is abandoned here on purpose. When air time is short the
    question is no longer which source deserves a turn, it is which video is
    back on the shelf soonest, and that is a property of the pool rather than of
    any one list. An unmeasured duration is not treated as a short video: it is
    left out, because guessing wrong in this direction queues a twelve hour
    subathon in front of a channel that has thirty minutes left.
    """
    candidates = {vid for ids in lists for vid in ids} - set(known)
    rated = sorted((durations[v], v) for v in candidates if durations.get(v))
    return [vid for _, vid in rated[:count]]


def pick(count, known, shortest=False):
    """Up to count ids, one from each source in turn until the count is met.

    Round robin rather than in order. Draining the first playlist before
    touching the second would put days of one thing on air and then days of
    another, which reads as a much smaller library than it is.
    """
    cached = pool()
    lists = [list(cached.get(cache_key(s), {}).get("ids", [])) for s in sources()]
    if shortest:
        durations = {}
        for source in sources():
            durations.update(cached.get(cache_key(source), {}).get("dur", {}))
        chosen = pick_shortest(count, known, lists, durations)
        if chosen:
            return chosen
        # nothing in the pool has a measured duration yet. Falling through to the
        # round robin queues something rather than nothing, and queueing nothing
        # is the one outcome a channel that is running dry cannot afford
    cursors = [0] * len(lists)
    chosen = []
    while len(chosen) < count:
        progressed = False
        for n, ids in enumerate(lists):
            if len(chosen) >= count:
                break
            # each source keeps its place, so a later pass carries on rather
            # than rescanning the ids it already rejected
            while cursors[n] < len(ids):
                vid = ids[cursors[n]]
                cursors[n] += 1
                if vid not in known and vid not in chosen:
                    chosen.append(vid)
                    progressed = True
                    break
        if not progressed:
            break
    return chosen


def board_queue(now=None):
    """How many URLs the board is holding, or None if it has not said lately.

    None is the honest answer to a stale file and it is treated as one: the
    caller falls back to its own depth rule rather than reading a count from a
    board that may have been unreachable for a day.
    """
    status = load(STATUS_FILE, None)
    if not isinstance(status, dict) or "queue" not in status:
        return None
    age = (time.time() if now is None else now) - status.get("at", 0)
    if age > STATUS_STALE_SECONDS:
        return None
    try:
        return int(status["queue"])
    except (TypeError, ValueError):
        return None


def main(argv):
    apply = "--apply" in argv
    free = shutil.disk_usage(LIBRARY).free
    have = len(library_ids())
    handed = load(HANDED_FILE, {})
    now = time.time()
    recent = {v for v, at in handed.items() if now - at < HANDED_COOLDOWN_SECONDS}
    runway = prep.runway_seconds()
    urgent = runway < URGENT_RUNWAY_SECONDS
    board = board_queue(now)

    print(f"bibliotheque={have}/{TARGET_LIBRARY_FILES} libre={free / 1024 ** 3:.1f}G "
          f"sources={len(sources())} en_vol={len(recent)} "
          f"antenne={runway / 3600:.1f}h{' URGENT' if urgent else ''} "
          f"carte={'?' if board is None else board}/{BOARD_QUEUE_DEPTH}")

    if free < MIN_FREE_BYTES:
        print("disque trop juste, le concierge travaille: rien ajoute")
        return 0
    want = min(TARGET_LIBRARY_FILES - have, MAX_PER_RUN)
    if board is not None:
        want = min(want, BOARD_QUEUE_DEPTH - board)
    if want <= 0:
        print("bibliotheque pleine ou carte deja servie, rien a faire")
        return 0

    known = library_ids() | inbox_ids() | recent
    chosen = pick(want, known, shortest=urgent)
    if not chosen:
        print("rien de nouveau dans les sources")
        return 0

    for vid in chosen:
        print(f"  {'ajoute' if apply else 'ajouterait'} https://www.youtube.com/watch?v={vid}")
    if apply:
        INBOX.parent.mkdir(parents=True, exist_ok=True)
        with INBOX.open("a", encoding="utf-8") as fh:
            for vid in chosen:
                fh.write(f"https://www.youtube.com/watch?v={vid}\n")
        for vid in chosen:
            handed[vid] = now
        save(HANDED_FILE, {v: at for v, at in handed.items()
                           if now - at < 7 * 24 * 3600})
        print(f"{len(chosen)} URL(s) deposee(s) dans l'inbox")
    else:
        print("essai a blanc, l'inbox n'a pas ete touchee. --apply pour agir.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
