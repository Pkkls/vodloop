#!/usr/bin/env python3
"""Kick's own VODs, fetched by this server. Listing side, shared with supply.py.

The board exists because YouTube walls datacenter addresses. Kick does not:
measured from Oracle, its API answers 200 to a plain browser user agent and its
CDN serves a 9 Mo segment at 33 Mbps. So a channel's own VODs skip the board
entirely, cost it none of its paced budget, and arrive within the hour instead
of the day.

A VOD is listed as parts, and each part carries its own id, `k` + eight hex of
the recording's uuid + its number. That matters: the channel never replays, so
an id that had covered a whole ten hour stream would take the other nine hours
out of the catalogue the moment the first one aired.
"""
import hashlib
import json
import pathlib
import subprocess
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
TABLE = chan.STATE / "kick.tsv"


def get(url, timeout=60):
    """curl, not urllib: Kick answers 403 to python's default fingerprint."""
    try:
        done = subprocess.run(["curl", "-sS", "-m", str(timeout), "-A", UA, url],
                              capture_output=True, text=True, timeout=timeout + 10)
        return done.stdout if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def part_id(uuid, number):
    """Eleven characters, unique per part, in the alphabet the names use."""
    return "k" + hashlib.sha1(uuid.encode()).hexdigest()[:8] + f"{number:02d}"


def safe_title(text, limit=70):
    keep = "".join(c if c.isalnum() or c in "-_" else "_" for c in (text or ""))
    while "__" in keep:
        keep = keep.replace("__", "_")
    return keep.strip("_")[:limit] or "kick"


def parse_master(text, maxh):
    """(url, bandwidth, height) of the tallest rendition the channel accepts.

    Kick publishes the source rung plus its own transcodes. A rung above the
    channel's ceiling is not taken: 1080p Kick is 3,43 Go/h against 1,10 in
    720p, and the disk is the thing in short supply, never the pixels.
    """
    best = None
    lines = text.splitlines()
    for n, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = line.split(":", 1)[1]
        height = bandwidth = 0
        for part in attrs.split(","):
            if part.startswith("RESOLUTION="):
                height = int(part.split("x")[-1])
            elif part.startswith("BANDWIDTH="):
                bandwidth = int(part.split("=")[1])
        uri = lines[n + 1].strip() if n + 1 < len(lines) else ""
        if not uri or not height or height > maxh or height < 480:
            continue
        if best is None or height > best[2]:
            best = (uri, bandwidth, height)
    return best


def parse_media(text):
    """[(url, seconds)] of the segments, in order."""
    out = []
    length = None
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                length = float(line[8:].split(",")[0])
            except ValueError:
                length = None
        elif line and not line.startswith("#") and length is not None:
            out.append((line.strip(), length))
            length = None
    return out


def slice_parts(total_seconds, part_seconds):
    """[(start, end)] covering the whole recording, never shorter than needed."""
    if part_seconds <= 0 or total_seconds <= part_seconds:
        return [(0, int(total_seconds))]
    count = int(total_seconds // part_seconds) + (1 if total_seconds % part_seconds else 0)
    step = total_seconds / count
    return [(int(round(n * step)), int(round((n + 1) * step))) for n in range(count)]


def segments_for(segments, start, end):
    """The segments whose own start falls in [start, end), with their offset."""
    kept, clock = [], 0.0
    for url, length in segments:
        if start <= clock < end:
            kept.append(url)
        clock += length
    return kept


def catalogue(slug, maxh, max_file_bytes, max_seconds):
    """[(id, seconds, place)] for one Kick channel, newest first, plus the table.

    The table is what the fetcher needs and the catalogue cannot carry: where
    the pictures are, and what to call the file.
    """
    raw = get(f"https://kick.com/api/v2/channels/{urllib.parse.quote(slug)}/videos")
    try:
        vods = json.loads(raw)
    except ValueError:
        return [], []
    if not isinstance(vods, list):
        return [], []
    vods = [v for v in vods if isinstance(v, dict) and not v.get("is_live")
            and (v.get("duration") or 0) > 60000 and v.get("source")]
    vods.sort(key=lambda v: str(v.get("start_time") or ""), reverse=True)
    rows, table = [], []
    place = 0
    for vod in vods:
        uuid = str((vod.get("video") or {}).get("uuid") or vod.get("id") or "")
        seconds = (vod.get("duration") or 0) / 1000.0
        if not uuid:
            continue
        master = get(vod["source"])
        rung = parse_master(master, maxh)
        if rung is None:
            continue
        uri, bandwidth, _ = rung
        # the part is as long as the file ceiling allows at this rung's bitrate
        part_seconds = min(max_seconds, int(max_file_bytes * 8 / max(bandwidth, 1)))
        name = f"{str(vod.get('start_time') or '')[:10]}_{safe_title(vod.get('session_title'))}"
        for number, (start, end) in enumerate(slice_parts(seconds, part_seconds), 1):
            vid = part_id(uuid, number)
            rows.append((vid, end - start, place))
            table.append((vid, urllib.parse.urljoin(vod["source"], uri), start, end,
                          name, bandwidth))
            place += 1
    return rows, table


def write_table(table):
    chan.STATE.mkdir(parents=True, exist_ok=True)
    tmp = TABLE.with_suffix(".tmp")
    tmp.write_text("".join("\t".join(str(f) for f in row) + "\n" for row in table))
    tmp.replace(TABLE)


def read_table():
    out = {}
    try:
        for line in TABLE.read_text().splitlines():
            fields = line.split("\t")
            if len(fields) >= 5:
                # the rung's own bitrate, when the table carries it: a Kick
                # 1080p rung is 0,75 Mo/s where the channel's average is 0,37,
                # and the share is spent on the true number
                out[fields[0]] = (fields[1], int(fields[2]), int(fields[3]), fields[4],
                                  int(fields[5]) if len(fields) > 5 else 0)
    except (OSError, ValueError):
        pass
    return out
