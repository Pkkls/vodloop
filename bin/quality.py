#!/usr/bin/env python3
"""Write down what the channel is actually putting out, one line per run.

Nothing here decides anything. Every other part of this system reasons about
what the stream should be: this one records what it is, so that "the quality
dropped" stops being something noticed by watching and becomes something with a
date on it. The audio loss this was written alongside had been running for days
and was found by probing a chunk by hand.

The picture is measured on the newest settled chunk rather than on the library,
because the library is only the input. A chunk is what the feeder concatenates
and the pusher sends, so it is the last place the answer is still true.

Run with no argument to append one sample. --report reads the last lines back
and says whether anything has fallen.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import common
import prep

LOG = common.STATE / "quality.jsonl"

# A chunk prep is still writing measures short and light, so it is skipped in
# favour of the newest one nothing has touched for a while.
SETTLED_SECONDS = 30
# ponytail: the file is trimmed to its second half once it passes this, rather
# than rotated. 288 samples a day is about 90 ko, so this holds roughly four
# months, and the disk this runs on has 5.4 Go free and a prep that stops
# preparing below 4. Rotation is the upgrade if anyone ever wants a year of it.
MAX_LOG_BYTES = 4 * 1024 ** 2

# What a fall looks like, taken from the library as it was measured 2026-09-08.
#
# Video: the worst full chunk on disk carried 2565 kbps of picture and the
# thinnest library file 2661, against the 1425 kbps the channel was pushing
# before the 1080p rebuild. Anything under this is the old failure, not variance.
MIN_VIDEO_BPS = 2_400_000
# Audio: not the 150k the sources nominally carry. Measured across all 38 files
# the real range is 141116 to 159782, and the transcode this replaced produced
# 131025. 135000 is the only line with a witness on both sides of it: below is
# the 128k path, above is every source in the library including the quietest.
MIN_AUDIO_BPS = 135_000

UA = "Mozilla/5.0 (X11; Linux x86_64) vodloop-quality"


def kick():
    """is_live and viewer_count, or the reason there is no answer.

    The status code is checked and recorded because 404 is what this got for
    hours on 2026-09-06 while the channel was live and fine: the slug had
    changed and every probe read the refusal as silence. A refusal is not an
    outage and must never be written down as one.
    """
    url = f"https://kick.com/api/v2/channels/{common.channel_slug()}"
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=20) as answer:
            status = answer.status
            body = answer.read(1 << 20)
    except urllib.error.HTTPError as exc:
        return {"kick_http": exc.code}
    except (OSError, ValueError) as exc:
        return {"kick_http": None, "kick_error": type(exc).__name__}
    if status != 200:
        return {"kick_http": status}
    try:
        data = json.loads(body)
    except ValueError:
        return {"kick_http": status, "kick_error": "body is not json"}
    if not isinstance(data, dict):
        return {"kick_http": status, "kick_error": "unexpected body"}
    stream = data.get("livestream")
    return {"kick_http": status, "is_live": bool(stream),
            "viewer_count": (stream or {}).get("viewer_count")}


def newest_chunk():
    """The most recent chunk nothing is still writing to, or None."""
    chunks = common.ready_segments()
    if not chunks:
        return None
    now = time.time()
    try:
        settled = [p for p in chunks if now - p.stat().st_mtime > SETTLED_SECONDS]
    except OSError:
        settled = []
    return max(settled or chunks, key=lambda p: p.stat().st_mtime)


def probe(path):
    """What one chunk carries. The keys are absent when it cannot be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,r_frame_rate,"
             "bit_rate,sample_rate,channels:format=duration,bit_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60)
        info = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"chunk_error": "unreadable"}
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    fmt = info.get("format") or {}
    total = number(fmt.get("bit_rate"))
    abits = number(audio.get("bit_rate"))
    return {
        "duration": number(fmt.get("duration")),
        "total_bps": total,
        # MPEG-TS carries no per-stream rate for the video, so it is what is
        # left of the file once the sound is taken out of it
        "video_bps": (total - abits) if (total and abits) else None,
        "vcodec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": rate(video.get("r_frame_rate")),
        "acodec": audio.get("codec_name"),
        "abitrate": abits,
        "asample_rate": number(audio.get("sample_rate")),
        "achannels": audio.get("channels"),
    }


def number(raw):
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def rate(raw):
    """"50/1" as a number, because a ratio does not compare."""
    try:
        top, _, bottom = str(raw).partition("/")
        return round(float(top) / float(bottom or 1), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def sample():
    row = {"ts": int(time.time()),
           "buffer_s": prep.seconds_on_disk(),
           "free_bytes": shutil.disk_usage(common.ROOT).free}
    chunk = newest_chunk()
    row["chunk"] = chunk.name if chunk else None
    if chunk is not None:
        row.update(probe(chunk))
    row.update(kick())
    return row


def append(row):
    common.STATE.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    try:
        if LOG.stat().st_size > MAX_LOG_BYTES:
            kept = LOG.read_text(encoding="utf-8").splitlines(keepends=True)
            tmp = LOG.with_suffix(".tmp")
            tmp.write_text("".join(kept[len(kept) // 2:]), encoding="utf-8")
            tmp.replace(LOG)
    except OSError:
        pass  # a log that cannot be trimmed is large, not wrong


def faults(row):
    """Everything wrong with one sample, named. Empty means nothing is."""
    found = []
    if not row.get("chunk"):
        found.append("aucun chunk sur le disque")
    else:
        video = row.get("video_bps")
        # a partial chunk is the tail of a video and measures light through no
        # fault of the encoder, so it does not get to raise an alarm
        full = (row.get("duration") or 0) >= common.CHUNK_SECONDS * 0.9
        if video is not None and full and video < MIN_VIDEO_BPS:
            found.append(f"video a {video // 1000} kbps (< {MIN_VIDEO_BPS // 1000})")
        audio = row.get("abitrate")
        if audio is not None and audio < MIN_AUDIO_BPS:
            found.append(f"audio a {audio // 1000} kbps (< {MIN_AUDIO_BPS // 1000})")
    if row.get("kick_http") == 200 and row.get("is_live") is False:
        found.append("la chaine n'est pas en ligne")
    return found


def read_rows(count):
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[-count:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def report(count):
    rows = read_rows(count)
    if not rows:
        print(f"aucune mesure dans {LOG}")
        return 1
    for row in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(row.get("ts", 0)))
        video = row.get("video_bps")
        audio = row.get("abitrate")
        # an unmeasurable field prints as "?" rather than as a zero, because a
        # zero here reads as a measurement and this is the absence of one
        print(f"{when}  {row.get('chunk') or '-':>16}  "
              f"{row.get('width') or '?'}x{row.get('height') or '?'}@{row.get('fps') or '?'}  "
              f"v={video // 1000 if video else '?'}k  "
              f"a={audio // 1000 if audio else '?'}k  "
              f"tampon={row.get('buffer_s')}s  "
              f"libre={(row.get('free_bytes') or 0) // 1024 ** 3}Go  "
              f"kick={row.get('kick_http')}/"
              f"{'live' if row.get('is_live') else 'off'}/"
              f"{row.get('viewer_count') if row.get('viewer_count') is not None else '?'}"
              + ("  <- " + ", ".join(faults(row)) if faults(row) else ""))
    last = faults(rows[-1])
    print()
    if last:
        print("BAISSE: " + " | ".join(last))
        return 1
    print(f"rien a signaler sur {len(rows)} mesure(s)")
    return 0


def main(argv):
    if "--report" in argv:
        rest = [a for a in argv if a != "--report"]
        try:
            count = int(rest[0]) if rest else 20
        except ValueError:
            count = 20
        return report(max(1, count))
    row = sample()
    append(row)
    print(json.dumps(row), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
