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
import cut  # noqa: E402
import kick  # noqa: E402

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


def measured_rate(paths, durations, fallback=250_000):
    """Bytes per second of what this channel actually receives.

    Measured, because the two channels differ by a factor of three: 720p VODs
    land at about 0.22 MB/s while long IRL streams are served at 690 kbps. It
    decides how long a video the board may be asked for at all.
    """
    size = secs = 0
    for path in paths:
        length = chan.duration(path, durations)
        if length > 60:
            size += chan.size_of(path)
            secs += length
    return size / secs if secs >= 3600 else fallback


def read_catalog():
    """(id, seconds, source, place) of the catalogue on disk.

    Numbers come back as numbers: comparing the file's text seconds to a
    ceiling is a TypeError that only fires in production. A file written before
    the places existed still reads, everything in it landing in source 0.
    """
    rows = []
    try:
        for line in CATALOG.read_text().splitlines():
            fields = line.split("\t")
            try:
                rows.append((fields[0], int(fields[1]),
                             int(fields[2]) if len(fields) > 2 else 0,
                             int(fields[3]) if len(fields) > 3 else len(rows)))
            except (IndexError, ValueError):
                continue
    except OSError:
        pass
    return rows


def newest_first(rows):
    """The candidates, sources taken in turn, each newest first.

    kil, 2026-09-17: "tu mets trop de videos trop anciennes d'un coup". The
    order of a listing is what carries recency, so the head of this list is the
    most recent of every source, and the board draws near the head.
    """
    by_source = {}
    for vid, secs, rank, place in rows:
        by_source.setdefault(rank, []).append((place, vid, secs))
    lists = [[(vid, secs) for _, vid, secs in sorted(items)]
             for _, items in sorted(by_source.items())]
    out, cursor = [], 0
    while any(cursor < len(items) for items in lists):
        for items in lists:
            if cursor < len(items):
                out.append(items[cursor])
        cursor += 1
    return out


def fetch_ceiling(rate):
    """The longest video the board can bring back whole, at that rate."""
    return min(chan.MAX_SECONDS, int(chan.MAX_FILE_BYTES / rate))


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
    rows, failed, table = {}, 0, []
    for rank, (url, words) in enumerate(sources):
        if url.startswith("kick:"):
            # the channel's own VODs, fetched by this server rather than by the
            # board: Kick lets a datacenter address through, YouTube does not
            kept, part = kick.catalogue(url[5:], chan.MAXH, chan.MAX_FILE_BYTES,
                                        chan.MAX_SECONDS)
            for vid, secs, place in kept:
                rows.setdefault(vid, (secs, rank, place))
            table += part
            failed += not kept
            chan.log(f"catalogue {url}: {len(kept)} morceaux")
            continue
        try:
            out = subprocess.run([YTDLP, "--flat-playlist", "--no-warnings", "--print",
                                  "%(id)s\t%(duration)s\t%(title)s", url],
                                 capture_output=True, text=True, timeout=900).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        kept = parse_listing(out, words)
        failed += not out.strip()
        # The listing comes back newest first, and that order is the only thing
        # that says how recent a video is: the flat listing carries no date.
        # Kept per source, with its place in it, so the draw can stay near the
        # top instead of pulling something from three years ago.
        for place, (vid, secs) in enumerate(kept):
            rows.setdefault(vid, (secs, rank, place))
        chan.log(f"catalogue {url}: {len(kept)} dans la bande")
    if failed == len(sources) or not rows:
        chan.log("aucune liste lue, catalogue precedent conserve")
        return
    if apply:
        tmp = CATALOG.with_suffix(".tmp")
        tmp.write_text("".join(f"{vid}\t{secs}\t{rank}\t{place}\n"
                               for vid, (secs, rank, place) in rows.items()))
        tmp.replace(CATALOG)
        if table:
            kick.write_table(table)


FORGIVEN_FAILURES = 1


def ledger_ids(name):
    rows = []
    try:
        for line in (chan.STATE / name).read_text().splitlines():
            fields = line.split("\t")
            if len(fields) >= 2:
                rows.append((int(fields[0]), fields[1]))
    except (OSError, ValueError):
        pass
    return rows


def excluded(now):
    """Everything the board must not be offered: held here, aired lately,
    refused for what it is, or failed to cut more than once."""
    ids = {chan.video_id(p) for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED, chan.UPLOAD)
           for p in chan.media(folder)}
    ids |= {vid for at, vid in ledger_ids("aired.tsv") if now - at < chan.REFETCH_SECONDS}
    ids |= {vid for _, vid in ledger_ids("rejected.tsv")}
    failures = {}
    for _, vid in ledger_ids("failed.tsv"):
        failures[vid] = failures.get(vid, 0) + 1
    ids |= {vid for vid, count in failures.items() if count > FORGIVEN_FAILURES}
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
    queued = sum(cut.remaining(p, chan.duration(p, durations)) for p in queue)
    held = queue + chan.media(chan.AIRED) + chan.media(chan.CURRENT)
    rate = measured_rate(held, durations)
    chan.write_json(chan.STATE / "durations.json",
                    {k: v for k, v in durations.items()
                     if any(k.startswith(p.name + ":") for p in held)})
    # What is evicted first is what the channel can no longer use: a file whose
    # every hour has been on the wire. A file still holding an unseen hour is
    # reserve against a dry spell, so it is the last thing to go, whatever its
    # age. Before this the oldest went first and took unseen hours with it.
    book = cut.ledger()
    aired = []
    for p in chan.media(chan.AIRED):
        try:
            st = p.stat()
            spent = not cut.unaired(p, book, durations)
            aired.append((p, st.st_size, (0 if spent else 1, st.st_mtime)))
        except OSError:
            continue
    # what the wire can still show without a repeat: the queue, what is already
    # cut, and the unseen hours of the reserve. It is the one number that says
    # how close the channel is to a loading card, so it is the one the board
    # paces itself on.
    runway = queued + len(list(chan.CHUNKS.glob("*.ts"))) * chan.CHUNK_SECONDS
    runway += sum(len(cut.unaired(p, book, durations)) * chan.PART_SECONDS
                  for p in chan.media(chan.AIRED)) if chan.PART_SECONDS else 0
    other = sum(chan.tree_bytes(f) for f in (chan.CURRENT, chan.CHUNKS, chan.UPLOAD))
    free = shutil.disk_usage(chan.ROOT).free
    need, offer, evict = plan(sum(chan.size_of(p) for p in queue), queued, other, aired, free)

    for path in evict:
        chan.log(f"{'retire' if apply else 'retirerait'} {chan.size_of(path) / chan.GIB:.2f} Go deja diffuse: {path.name[:70]}")
        if apply:
            path.unlink(missing_ok=True)

    skip = excluded(now)
    catalog = read_catalog()
    # A video the board cannot bring back whole is not a candidate. Its merge is
    # what breaks first: 211 Mo of RAM on that card, and a 9 h 30 stream of
    # 6.7 Go died in ffmpeg at the end of it on 2026-09-17, after three hours of
    # downloading. The ceiling is the file size the card survives, read back as
    # a duration through the rate this channel actually receives.
    fetch_seconds = fetch_ceiling(rate)
    # A Kick part is fetched by this server, and its id is eleven characters
    # like a YouTube one, so the board cannot tell them apart: it drew them from
    # this list and asked YouTube for kbeaa817f02. Thirty-two of the hundred and
    # eighty-seven offered to it were unfetchable, at the head of the list where
    # it draws, and each one burned a slot the pacing only gives every ninety
    # minutes. The board is offered what the board can fetch.
    mine = set(kick.read_table())
    candidates = [(vid, secs) for vid, secs in newest_first(catalog)
                  if vid not in skip and vid not in mine and secs <= fetch_seconds]
    chan.log(f"file {len(queue)} ({queued / 3600:.1f} h), reserve {runway / 3600:.1f} h, "
             f"diffuses {len(aired) - len(evict)}, "
             f"besoin {need / 3600:.1f} h, offre {offer / chan.GIB:.1f} Go, "
             f"libre {free / chan.GIB:.1f} Go, candidats {len(candidates)}/{len(catalog)} "
             f"(<= {fetch_seconds / 3600:.1f} h a {rate / 1e6:.2f} Mo/s)")
    if apply:
        tmp = CANDIDATES.with_suffix(".tmp")
        tmp.write_text("".join(f"{vid}\t{secs}\n" for vid, secs in candidates))
        tmp.replace(CANDIDATES)
        chan.write_json(WANT, {"need_seconds": need, "offer_bytes": offer, "maxh": chan.MAXH,
                               "minh": chan.MINH, "runway_seconds": int(runway),
                               "queue_files": len(queue), "queue_hours": round(queued / 3600, 1),
                               "candidates": len(candidates), "max_seconds": fetch_seconds,
                               "rate_bps": int(rate), "at": int(now)})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
