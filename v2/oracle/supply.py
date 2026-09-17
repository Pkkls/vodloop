#!/usr/bin/env python3
"""Tell the board what this channel needs, and make the room for it. From cron.

    python3 supply.py            say what it would do
    python3 supply.py --apply    evict, write state/want.json and state/candidates.tsv

Three outputs, all read by the board, which is behind NAT and can only ask:
  - want.json: how many seconds the queue is short of WINDOW_HOURS, and how many
    bytes a delivery may weigh.
  - candidates.tsv: the catalogue minus everything on this box, aired within
    REFETCH_DAYS, or refused. The board draws from it at random.
  - the room itself. The only files ever deleted are aired ones, oldest first,
    and only as many as the offer needs. A file waiting to air is never touched,
    so no pair of thresholds can deadlock: arrivals need room, room comes from
    aired files, aired files come from arrivals.

The catalogue is relisted once a day from sources.txt, one URL per line, with
an optional "| word, word" to keep only titles containing one of the words.
Listing works from a datacenter address; only the player API is walled.
"""
import pathlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402

CATALOG = chan.STATE / "catalog.tsv"
CANDIDATES = chan.STATE / "candidates.tsv"
WANT = chan.STATE / "want.json"
SOURCES = chan.ROOT / "sources.txt"
MARGIN_BYTES = chan.GIB
CATALOG_TTL = 24 * 3600
YTDLP = shutil.which("yt-dlp") or str(pathlib.Path.home() / ".local/bin/yt-dlp")


def plan(queue_bytes, queued_seconds, other_bytes, aired, free, budget=chan.BUDGET_BYTES,
         window=chan.WINDOW_SECONDS, max_file=chan.MAX_FILE_BYTES,
         floor=chan.FLOOR_BYTES, margin=MARGIN_BYTES):
    """(need_seconds, offer_bytes, evict) from sizes alone. Pure.

    aired is [(path, bytes, mtime)]; other_bytes is current/, chunks/ and upload/.
    """
    need = max(0, int(window - queued_seconds))
    if need == 0:
        return 0, 0, []
    hard = queue_bytes + other_bytes
    offer = min(max_file, budget - hard - margin)
    if offer <= 0:
        return need, 0, []
    aired_bytes = sum(size for _, size, _ in aired)
    evict = []
    for path, size, _ in sorted(aired, key=lambda a: a[2]):
        if hard + aired_bytes + offer <= budget and free - offer >= floor:
            break
        evict.append(path)
        aired_bytes -= size
        free += size
    offer = min(offer, budget - hard - aired_bytes, free - floor)
    return need, max(0, offer), evict


def parse_sources(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, words = line.partition("|")
        out.append((url.strip(), [w.strip().lower() for w in words.split(",") if w.strip()]))
    return out


def parse_listing(text, words, low=chan.MIN_SECONDS, high=chan.MAX_SECONDS):
    """id and seconds of the entries in the band whose title matches."""
    kept = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or len(parts[0]) != 11:
            continue
        try:
            secs = int(float(parts[1]))
        except ValueError:
            continue  # live or hidden: NA
        if not low <= secs <= high:
            continue
        if words and not any(w in parts[2].lower() for w in words):
            continue
        kept.append((parts[0], secs))
    return kept


def refresh_catalog(apply):
    try:
        fresh = time.time() - CATALOG.stat().st_mtime < CATALOG_TTL
        if fresh and SOURCES.stat().st_mtime < CATALOG.stat().st_mtime:
            return
    except OSError:
        pass
    try:
        sources = parse_sources(SOURCES.read_text())
    except OSError:
        chan.log("sources.txt absent")
        return
    rows, failed = {}, 0
    for url, words in sources:
        try:
            out = subprocess.run([YTDLP, "--flat-playlist", "--no-warnings", "--print",
                                  "%(id)s\t%(duration)s\t%(title)s", url],
                                 capture_output=True, text=True, timeout=900).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        kept = parse_listing(out, words)
        failed += not out.strip()
        rows.update(kept)
        chan.log(f"catalogue {url}: {len(kept)} dans la bande")
    if failed == len(sources) or not rows:
        chan.log("aucune liste lue, catalogue precedent conserve")
        return
    if apply:
        tmp = CATALOG.with_suffix(".tmp")
        tmp.write_text("".join(f"{vid}\t{secs}\n" for vid, secs in rows.items()))
        tmp.replace(CATALOG)


def excluded(now):
    ids = {chan.video_id(p) for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED, chan.UPLOAD)
           for p in chan.media(folder)}
    for name in ("aired.tsv", "rejected.tsv"):
        try:
            for line in (chan.STATE / name).read_text().splitlines():
                fields = line.split("\t")
                if len(fields) >= 2 and (name == "rejected.tsv"
                                         or now - int(fields[0]) < chan.REFETCH_SECONDS):
                    ids.add(fields[1])
        except (OSError, ValueError):
            continue
    ids.discard(None)
    return ids


def main(argv):
    apply = "--apply" in argv
    now = time.time()
    for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED, chan.CHUNKS, chan.UPLOAD, chan.STATE):
        folder.mkdir(parents=True, exist_ok=True)
    refresh_catalog(apply)

    durations = chan.read_json(chan.STATE / "durations.json", {})
    queue = chan.media(chan.QUEUE)
    queued = sum(chan.duration(p, durations) for p in queue)
    chan.write_json(chan.STATE / "durations.json",
                    {k: v for k, v in durations.items()
                     if any(k.startswith(p.name + ":") for p in queue)})
    aired = []
    for p in chan.media(chan.AIRED):
        try:
            st = p.stat()
            aired.append((p, st.st_size, st.st_mtime))
        except OSError:
            continue
    other = sum(chan.tree_bytes(f) for f in (chan.CURRENT, chan.CHUNKS, chan.UPLOAD))
    free = shutil.disk_usage(chan.ROOT).free
    need, offer, evict = plan(sum(chan.size_of(p) for p in queue), queued, other, aired, free)

    for path in evict:
        chan.log(f"{'retire' if apply else 'retirerait'} {chan.size_of(path) / chan.GIB:.2f} Go deja diffuse: {path.name[:70]}")
        if apply:
            path.unlink(missing_ok=True)

    skip = excluded(now)
    try:
        catalog = [line.split("\t") for line in CATALOG.read_text().splitlines()]
    except OSError:
        catalog = []
    candidates = [(vid, secs) for vid, secs in catalog if vid not in skip]
    chan.log(f"file {len(queue)} ({queued / 3600:.1f} h), diffuses {len(aired) - len(evict)}, "
             f"besoin {need / 3600:.1f} h, offre {offer / chan.GIB:.1f} Go, "
             f"libre {free / chan.GIB:.1f} Go, candidats {len(candidates)}/{len(catalog)}")
    if apply:
        tmp = CANDIDATES.with_suffix(".tmp")
        tmp.write_text("".join(f"{vid}\t{secs}\n" for vid, secs in candidates))
        tmp.replace(CANDIDATES)
        chan.write_json(WANT, {"need_seconds": need, "offer_bytes": offer, "maxh": chan.MAXH,
                               "queue_files": len(queue), "queue_hours": round(queued / 3600, 1),
                               "candidates": len(candidates), "at": int(now)})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
