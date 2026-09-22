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
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402
import cut  # noqa: E402
import kick  # noqa: E402
import shorts  # noqa: E402

CATALOG = chan.STATE / "catalog.tsv"
CANDIDATES = chan.STATE / "candidates.tsv"
# ids the chat has paid to see, written by bot.py. They go to the head of the
# candidate list marked with a "!", which is what tells the board to take that
# one rather than draw, and they leave the list once they have been fetched.
REQUESTS = chan.STATE / "requests.tsv"
WANT = chan.STATE / "want.json"
# what was in the queue last time this ran, so an arrival can be told apart
# from the queue simply being non-empty
SEEN = chan.STATE / "delivered.json"
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


def announce(queue, durations, apply):
    """One line on Telegram per delivery, and nothing when nothing landed.

    kil, 2026-09-21: "dis moi lorsqu'une video est republish sur oracle". Said
    here because this is the process that sees the queue change, and because a
    watcher running somewhere else stops the day that somewhere else does. It
    reads the names it knew last time from the disk, so a restart does not
    announce the whole library.
    """
    # every folder a delivery can be in by the time this runs, not just the
    # queue: the cutter takes a file out of it within the minute, so a delivery
    # that lands between two passes was never in the queue when either looked
    now = {p.name for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED)
           for p in chan.media(folder)}
    first = not SEEN.exists()
    before = set(chan.read_json(SEEN, {}).get("queue") or [])
    fresh = sorted(now - before)
    if apply:
        chan.write_json(SEEN, {"queue": sorted(now)})
    if first or not fresh:
        # nothing to compare against on the very first run, and announcing the
        # whole library once would be the wrong kind of first impression. An
        # empty set is not the same as no set: the queue empties every time the
        # cutter takes the last file, and that used to silence the next arrival
        return []
    for name in fresh:
        where = next((f / name for f in (chan.QUEUE, chan.CURRENT, chan.AIRED)
                      if (f / name).exists()), chan.QUEUE / name)
        hours = chan.duration(where, durations) / 3600
        # the same reading bot.py gives a viewer: no epoch prefix, no video id,
        # no extension, because this line is read by a person on a phone
        title = re.sub(r"^\d{9,}-", "", pathlib.Path(name).stem)
        title = re.sub(r"-[A-Za-z0-9_-]{11}$", "", title).replace("_", " ")[:52]
        chan.log(f"arrivee: {title} ({hours:.1f} h)")
        if apply:
            chan.telegram(f"arrivee: {title} ({hours:.1f} h)")
    return fresh


def candidates():
    """(id, seconds, title) the board may still be fetched for, recent first.

    The same file the board draws from, read back, so a number the chat picks
    cannot point at something that is already here, was refused, or aired
    lately. The leading "!" of an id already requested is dropped: it is a mark
    for the board, not part of the id.
    """
    rows = []
    try:
        for line in CANDIDATES.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            try:
                rows.append((fields[0].lstrip("!"), int(fields[1]),
                             fields[2].strip() if len(fields) > 2 else ""))
            except (IndexError, ValueError):
                continue
    except OSError:
        pass
    return rows


def catalog_titles():
    """{id: title} for the whole catalogue.

    It used to hold ids and nothing else, so nothing could answer "have you got
    anything from Turkey": the only titles on the box were the handful of files
    already downloaded. Six hundred entries know the answer, and now they say it.
    """
    out = {}
    try:
        for line in CATALOG.read_text().splitlines():
            fields = line.split("	")
            if len(fields) >= 5 and fields[4].strip():
                out[fields[0]] = fields[4].strip()
    except OSError:
        pass
    return out


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


# The board filters YouTube's rungs on the video track alone and leaves this
# much of the channel's ceiling for the sound it will merge in. Offering a
# video sized against the whole ceiling means offering one the board has to
# refuse: on 2026-09-19 that was 19 per cent of what cx247 was being shown,
# each one a fetch slot spent on a format probe that could only say no.
AUDIO_ALLOWANCE = 400 * 2 ** 20


def aired_countries(titles, recent=12):
    """The countries of the last few videos put on the wire, newest first.

    Read from the hour ledger rather than from aired.tsv: a file sits between
    two of its hours for a day, and what the channel has just shown is the
    hours, not the files.
    """
    seen, out = set(), []
    rows = []
    # Both ledgers, because the granularity moved and this reader did not. An
    # hour is spent by the chunk since 737aaa3, so units.tsv is the one that
    # still grows and hours.tsv stopped on 2026-09-20: reading it alone was
    # rotating countries against a two day old memory, for ever. The two have
    # the same shape, epoch then id, and the id is all this needs.
    for name in ("units.tsv", "hours.tsv"):
        try:
            for line in (chan.STATE / name).read_text().splitlines():
                fields = line.split("	")
                if len(fields) >= 3:
                    rows.append((int(fields[0]), fields[1]))
        except (OSError, ValueError):
            continue
    for _, vid in sorted(rows, reverse=True):
        if vid in seen:
            continue
        seen.add(vid)
        out.append(chan.country_of(titles.get(vid, "")))
        if len(out) >= recent:
            break
    return out


def mix_countries(rows, titles, just_aired=()):
    """Reorder so the head of the list is not forty videos of one country.

    kil, 2026-09-19: "essaie de mixer un peu les pays". The board only ever
    looks at the first hundred and twenty lines, and on that day they held
    four countries out of the eleven the catalogue covers: Peru, Chile,
    Argentina, Korea, Thailand, Mexico and Taiwan were all past the cut and
    could never be drawn at all.

    One from each country in turn, keeping each country's own order, so the
    head holds every country the catalogue has. Countries whose hours are
    still fresh on the wire go to the back of the first round, which spaces
    them out without ever putting them out of reach.
    """
    groups = {}
    for row in rows:
        groups.setdefault(chan.country_of(titles.get(row[0], "")), []).append(row)
    fresh = list(just_aired)

    def staleness(name):
        # how many videos ago this country was last on, most recent lowest
        return fresh.index(name) if name in fresh else len(fresh)

    order = sorted(groups, key=lambda name: (-staleness(name), -len(groups[name]), name))
    out, cursor = [], 0
    while any(cursor < len(groups[name]) for name in order):
        for name in order:
            if cursor < len(groups[name]):
                out.append(groups[name][cursor])
        cursor += 1
    return out


def fetch_ceiling(rate):
    """The longest video the board can bring back whole, at that rate."""
    room = max(0, chan.MAX_FILE_BYTES - AUDIO_ALLOWANCE)
    return min(chan.MAX_SECONDS, int(room / rate))


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
    """id, seconds and title of the entries in the band whose title matches."""
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
        kept.append((parts[0], secs, re.sub(r"\s+", " ", parts[2]).strip()[:120]))
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
    # kil, 2026-09-21: the pool went from 554 to 338 in one run. One listing
    # came back empty, the catalogue was rewritten without it, and 177 videos
    # left the draw until the next relisting a day later. A source that
    # answers nothing now keeps what it answered before: a listing that fails
    # says nothing about the channel it failed to read. The cost is that a
    # source really gone stays in the catalogue, where the refusal ledger and
    # the failure count take care of it one video at a time.
    previous = {}
    try:
        for line in CATALOG.read_text().splitlines():
            fields = line.split("\t")
            if len(fields) >= 4:
                previous[fields[0]] = (int(fields[1]), int(fields[2]), int(fields[3]),
                                       fields[4] if len(fields) > 4 else "")
    except (OSError, ValueError):
        previous = {}
    rows, failed, table, empty = {}, 0, [], []
    for rank, (url, words) in enumerate(sources):
        if url.startswith("kick:") and chan.NO_KICK:
            continue
        if url.startswith("kick:"):
            # the channel's own VODs, fetched by this server rather than by the
            # board: Kick lets a datacenter address through, YouTube does not
            kept, part = kick.catalogue(url[5:], chan.MAXH, chan.MAX_FILE_BYTES,
                                        chan.MAX_SECONDS)
            for vid, secs, place in kept:
                rows.setdefault(vid, (secs, rank, place, ""))
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
        for place, (vid, secs, name) in enumerate(kept):
            rows.setdefault(vid, (secs, rank, place, name))
        if not kept:
            empty.append(rank)
        chan.log(f"catalogue {url}: {len(kept)} dans la bande")
    for rank in empty:
        kept_back = {vid: row for vid, row in previous.items() if row[1] == rank}
        if kept_back:
            chan.log(f"source {rank} muette, ses {len(kept_back)} entrees sont gardees")
        for vid, row in kept_back.items():
            rows.setdefault(vid, row)
    if failed == len(sources) or not rows:
        chan.log("aucune liste lue, catalogue precedent conserve")
        return
    if apply:
        shorts.wanted()
        tmp = CATALOG.with_suffix(".tmp")
        tmp.write_text("".join(f"{vid}\t{secs}\t{rank}\t{place}\t{name}\n"
                               for vid, (secs, rank, place, name) in rows.items()))
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
            spent = not cut.unaired(p, book, durations, cut.reserved())
            aired.append((p, st.st_size, (0 if spent else 1, st.st_mtime)))
        except OSError:
            continue
    # what the wire can still show without a repeat: the queue, what is already
    # cut, and the unseen hours of the reserve. It is the one number that says
    # how close the channel is to a loading card, so it is the one the board
    # paces itself on.
    runway = queued + len(list(chan.CHUNKS.glob("*.ts"))) * chan.CHUNK_SECONDS
    runway += sum(len(cut.unaired(p, book, durations)) * cut.unit_seconds()
                  for p in chan.media(chan.AIRED)) if chan.PART_SECONDS else 0
    # kil, 2026-09-21: "les viewers peuvent avoir le droit de skip sans que ca
    # retrieve que 2 videos". A skip needs somewhere to land, and somewhere is
    # another recording, not another hour of the one on air. Depth and breadth
    # are not the same reserve: one nine hour file is nine hours of runway and
    # exactly one destination, so the board is told how many recordings still
    # hold unseen time, not only how many hours they add up to.
    held = cut.reserved()
    streams, on_disk, spent = 0, 0, 0.0
    for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED):
        for p in chan.media(folder):
            free = len(cut.unaired(p, book, durations, held))
            streams += bool(free)
            # what a file weighs against the part of it nobody will be shown
            # again. A file is kept whole until its last hour has gone out, so
            # this share climbs from nothing to everything over its life and
            # sits near a half in the middle. Written down every ten minutes
            # rather than measured the day it matters, because on the day it
            # matters the disk is already full.
            total = cut.units_in(chan.duration(p, durations))
            on_disk += chan.size_of(p)
            spent += chan.size_of(p) * max(0, total - free) / max(1, total)
    seen_share = 100 * spent / on_disk if on_disk else 0
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
    titles = catalog_titles()
    candidates = mix_countries(candidates, titles, aired_countries(titles))
    # REFETCH_DAYS is short now, so a video that aired comes back into the
    # pool while hundreds have never been seen at all. Those go first: the
    # board gets one video every ninety minutes and spending a slot on a
    # rerun while new material is waiting is the one thing that pool ordering
    # can get wrong. Within each group the country rotation is kept.
    seen_before = {vid for _, vid in ledger_ids("aired.tsv")}
    candidates.sort(key=lambda row: row[0] in seen_before)
    chan.log(f"file {len(queue)} ({queued / 3600:.1f} h), {streams} inedites, "
             f"{seen_share:.0f}% deja vu, reserve {runway / 3600:.1f} h, "
             f"diffuses {len(aired) - len(evict)}, "
             f"besoin {need / 3600:.1f} h, offre {offer / chan.GIB:.1f} Go, "
             f"libre {free / chan.GIB:.1f} Go, candidats {len(candidates)}/{len(catalog)} "
             f"(<= {fetch_seconds / 3600:.1f} h a {rate / 1e6:.2f} Mo/s)")
    asked = []
    try:
        asked = [line.split("\t")[0].strip()
                 for line in REQUESTS.read_text().splitlines() if line.strip()]
    except OSError:
        pass
    wanted = {v for v in asked if v not in skip}
    if apply and len(wanted) != len(asked):
        # one that has landed, aired or been refused is no longer a request
        REQUESTS.write_text("".join(f"{v}\n" for v in asked if v in wanted))
    announce(queue, durations, apply)
    # anything the board dropped is built now: it costs about a second per
    # second of short, niced, and this run already holds the channel's lock
    if apply:
        shorts.collect()
        shorts.prune()
    names = catalog_titles()
    if apply:
        tmp = CANDIDATES.with_suffix(".tmp")
        rows = [(("!" if vid in wanted else "") + vid, secs, names.get(vid, ""))
                for vid, secs in candidates]
        rows.sort(key=lambda r: not r[0].startswith("!"))
        tmp.write_text("".join(f"{vid}\t{secs}\t{name}\n" for vid, secs, name in rows))
        tmp.replace(CANDIDATES)
        chan.write_json(WANT, {"need_seconds": need, "offer_bytes": offer, "maxh": chan.MAXH,
                               "minh": chan.MINH, "runway_seconds": int(runway),
                               "max_file_mb": int(chan.MAX_FILE_BYTES / (1024 ** 2)),
                               "queue_files": len(queue), "queue_hours": round(queued / 3600, 1),
                               "candidates": len(candidates), "max_seconds": fetch_seconds,
                               "requested": len(wanted), "streams": streams,
                               "rate_bps": int(rate), "at": int(now)})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
