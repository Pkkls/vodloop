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

Two things bound it. It stops when the library is big enough, because queueing
past that only fills a disk the janitor then has to empty. And it does not
requeue anything already in the library, already waiting in the inbox, or
handed over recently: a video in flight is in none of those first two, having
been claimed off the inbox, so without the third it would be fetched twice.
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

LIBRARY = pathlib.Path("/home/ubuntu/videos")
INBOX = pathlib.Path("/home/ubuntu/yt2oracle/inbox.txt")
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
MIN_FREE_BYTES = 10 * 1024 ** 3

VIDEO_ID = re.compile(r"-([A-Za-z0-9_-]{11})\.(?:mp4|mkv)$")


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
    """Video ids for one source, keeping only titles that match when asked."""
    out = subprocess.run(
        [YTDLP, "--flat-playlist", "--no-warnings", "--print",
         "%(id)s	%(title)s", source["url"]],
        capture_output=True, text=True, timeout=900)
    keep = []
    for line in out.stdout.splitlines():
        vid, _, title = line.partition("	")
        vid = vid.strip()
        if len(vid) != 11:
            continue
        if source["match"] and not any(w in title.lower() for w in source["match"]):
            continue
        keep.append(vid)
    return keep


def pool():
    """Video ids per source, cached because listing thousands is slow."""
    cached = load(POOL_FILE, {})
    now = time.time()
    for source in sources():
        key = cache_key(source)
        entry = cached.get(key)
        if entry and now - entry.get("at", 0) < POOL_TTL_SECONDS:
            continue
        try:
            ids = list_source(source)
        except (OSError, subprocess.SubprocessError):
            continue
        # an empty listing is a failed listing, not an empty source: keeping the
        # previous one is better than forgetting a source because YouTube
        # hiccuped once
        if ids:
            cached[key] = {"at": now, "ids": ids}
    save(POOL_FILE, cached)
    return cached


def pick(count, known):
    """Up to count ids, one from each source in turn until the count is met.

    Round robin rather than in order. Draining the first playlist before
    touching the second would put days of one thing on air and then days of
    another, which reads as a much smaller library than it is.
    """
    cached = pool()
    lists = [list(cached.get(cache_key(s), {}).get("ids", [])) for s in sources()]
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


def main(argv):
    apply = "--apply" in argv
    free = shutil.disk_usage(LIBRARY).free
    have = len(library_ids())
    handed = load(HANDED_FILE, {})
    now = time.time()
    recent = {v for v, at in handed.items() if now - at < HANDED_COOLDOWN_SECONDS}

    print(f"bibliotheque={have}/{TARGET_LIBRARY_FILES} libre={free / 1024 ** 3:.1f}G "
          f"sources={len(sources())} en_vol={len(recent)}")

    if free < MIN_FREE_BYTES:
        print("disque trop juste, le concierge travaille: rien ajoute")
        return 0
    want = min(TARGET_LIBRARY_FILES - have, MAX_PER_RUN)
    if want <= 0:
        print("bibliotheque pleine, rien a faire")
        return 0

    known = library_ids() | inbox_ids() | recent
    chosen = pick(want, known)
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
