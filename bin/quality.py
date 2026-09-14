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

Every chunk is then compared against the file it was made from, and that
comparison is the whole point now that nothing is supposed to be re-encoded. If
the codec, the size and the frame rate on the wire are the same as the source's,
the packets were copied and there is no generation to lose. If they ever differ,
something re-encoded, and this says so in as many words.

That comparison also replaced the fixed thresholds this started with. Twice in
one day a floor written from "what the library looks like" turned out to be
measuring an assumption: 150k of audio, when YouTube serves 128k and the 160k in
the library only existed because a PC had re-encoded it. A source can be thin
without anything here being broken. What matters is whether the wire matches it.

    python3 bin/quality.py                       sample once, append a line
    python3 bin/quality.py --report 20           read the last 20 back
    python3 bin/quality.py --report --telegram   and send it to the bot
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import common
import prep
import tgbot

LOG = common.STATE / "quality.jsonl"
ALERTS = common.STATE / "quality_alert.json"

# ponytail: the file is trimmed to its second half once it passes this, rather
# than rotated. 288 samples a day is about 250 ko, so this holds roughly six
# weeks, and the disk this runs on has a prep that stops preparing below 4 Go.
MAX_LOG_BYTES = 4 * 1024 ** 2
# The same fault every five minutes is not monitoring, it is noise someone
# learns to ignore. One message per distinct fault per this long.
ALERT_COOLDOWN_SECONDS = 30 * 60
# Some faults describe the video rather than a breakage, and nothing anyone does
# in the next half hour changes them: a source too thin to lead Kick's ladder
# stays too thin for as long as it is on air, which for this pool can be eleven
# hours. Saying it once a shift is telling; saying it twenty times is noise.
STANDING_COOLDOWN_SECONDS = 6 * 3600
# What marks one. Kept as a prefix on the fault text so there is one place to
# read it from and nothing to keep in step.
STANDING_PREFIX = "RUNG:"

# The height the channel asked the downloader for. An arrival below it is a
# silent fault by construction: the format selector falls through to a lower
# branch when the wanted one is too large for the fetching board, the fallback
# succeeds, and nothing anywhere calls a working fallback an error. Four of ten
# library files were 720p on 2026-09-14 and nothing had reported it.
#
# Compared against what is actually on the wire rather than against the files,
# because that is the thing being promised, and it costs nothing: the sample
# already carries it. Unset means the channel has no opinion and this is quiet.
WANTED_HEIGHT = int(os.environ.get("VODLOOP_MAX_HEIGHT") or 0)

# A catastrophe net, and deliberately nothing finer than that. The real question
# is asked against the source a few functions down; these two only catch the
# stream collapsing to something no source could explain. Measured 2026-09-08:
# the library runs 2661 to 2819 kbps of picture, a fresh YouTube 1080p60 runs
# higher, and the failure this remembers is the 1425 kbps the channel pushed
# before the 1080p rebuild. Anything under a megabit is broken, not thin.
MIN_VIDEO_BPS = 1_000_000
# 128k is what YouTube's m4a carries and what an arrival now brings with it, so
# a floor at 135k, which is where this started, would have flagged every fresh
# download as a loss. AAC below 48k is not a quiet passage, it is a fault.
MIN_AUDIO_BPS = 48_000
# How far a chunk's bitrate may sit from its source's before it is worth saying
# out loud. A chunk is a five minute slice of a longer file, so its rate moves
# with what is happening on screen even when every byte was copied verbatim.
BITRATE_TOLERANCE = 0.35

UA = "Mozilla/5.0 (X11; Linux x86_64) vodloop-quality"


# --- what Kick says -------------------------------------------------------

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
            "viewer_count": (stream or {}).get("viewer_count"),
            "playback_url": data.get("playback_url")}


STREAM_INF = re.compile(r"^#EXT-X-STREAM-INF:(.*)$")


def rungs(playback_url):
    """Every rung Kick advertises, from the master playlist.

    The source rung is the one carrying VIDEO="chunked": that is the name Kick
    gives the passthrough, the copy of what was pushed to it. The others are its
    own encodes, made from that one.

    The order of the rungs in the playlist is not stable, so the source is found
    by its VIDEO group and never by position. Reading the first entry instead is
    a mistake already made once here, and it measured the 360p rung.
    """
    lines = []
    request = urllib.request.Request(playback_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=20) as answer:
            if answer.status != 200:
                return None
            text = answer.read(1 << 20).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    for line in text.splitlines():
        match = STREAM_INF.match(line.strip())
        if not match:
            continue
        attrs = dict(re.findall(r'([A-Z-]+)=("[^"]*"|[^,]*)', match.group(1)))
        size = attrs.get("RESOLUTION", "").split("x")
        lines.append({
            "video": attrs.get("VIDEO", "").strip('"'),
            "bps": whole(attrs.get("BANDWIDTH")),
            "fps": number(attrs.get("FRAME-RATE")),
            "width": whole(size[0]) if len(size) == 2 else None,
            "height": whole(size[1]) if len(size) == 2 else None,
        })
    return lines or None


def whole(raw):
    try:
        return int(str(raw).strip('"'))
    except (TypeError, ValueError):
        return None


def ladder(playback_url, pushed=None):
    """What Kick advertises for the source rung, and what can outbid it.

    `pushed` is the (width, height) of the chunk on air, and when it is known it
    is what "smaller" is measured against. Kick labels the source rung once per
    RTMP session and never again: measured 2026-09-12, a session opened at
    1080p60 still advertised 1920x1080 hours later while its segments decoded
    to 640x360, so against the label every 720p file read as an upscale.

    A player choosing by bandwidth takes the fattest rung it can afford, so the
    thing that costs a viewer picture is a rung with FEWER pixels than the
    source advertising MORE bandwidth than it: the player takes that one and
    blows it back up. On 2026-09-08, fed 50 fps, the 1080p source advertised
    3 070 272 while Kick's own 720p60 advertised 3 422 999, and that is what
    "fausse 1080p, plein de tearing" was.

    Only smaller rungs count. Kick also publishes an encode at the source's own
    resolution, and when a thin source is outbid by that one the viewer loses a
    generation but not a single pixel, which is not worth waking anyone for. A
    720p30 stream at 690 kbps, which is what YouTube serves for the long IRL
    VODs in this pool, is outbid by Kick's 720p60 rung on every sample of every
    one of its eleven hours.
    """
    if not playback_url:
        return {}
    found = rungs(playback_url)
    if not found:
        return {"ladder_error": "playlist illisible"}
    source = next((r for r in found if r["video"] == "chunked"), None)
    if source is None:
        # A session opened below 1080p gets no passthrough at all: measured
        # 2026-09-12 on a 720p60 session, the ladder topped out at Kick's own
        # 720p60 encode. That is a generation lost, not a fault, unless what is
        # on air now is bigger than that top, which is every viewer downscaled.
        top = max(found, key=lambda r: (r["width"] or 0) * (r["height"] or 0))
        out = {"rung_width": top["width"], "rung_height": top["height"],
               "rung_fps": top["fps"], "rung_bps": None, "rung_count": len(found),
               "rung_best_smaller_bps": None}
        pushed_pixels = pushed[0] * pushed[1] if pushed and all(pushed) else 0
        if pushed_pixels > (top["width"] or 0) * (top["height"] or 0):
            out["ladder_error"] = ("session ouverte plus bas que l'antenne, "
                                   "Kick ne sert pas sa resolution")
        return out
    width, height = pushed if pushed and all(pushed) else (source["width"], source["height"])
    pixels = (width or 0) * (height or 0)
    smaller = [r["bps"] for r in found
               if r is not source and r["bps"]
               and 0 < (r["width"] or 0) * (r["height"] or 0) < pixels]
    return {"rung_width": source["width"], "rung_height": source["height"],
            "rung_fps": source["fps"], "rung_bps": source["bps"],
            "rung_count": len(found),
            "rung_best_smaller_bps": max(smaller) if smaller else None}


# --- what the machine is doing --------------------------------------------

# Matched against a process command line, all words present. The pusher has no
# script name to match on: it is a bare ffmpeg, and what makes it the pusher is
# where it is reading from and where it is sending.
WATCHED = {"push": ("ffmpeg", "rtmps", str(common.FIFO)),
           "feed": ("feeder.py",),
           "prep": ("prep.py",)}


def _cpu_jiffies():
    """Total and idle jiffies, aggregate and per core."""
    out = {}
    try:
        for line in pathlib.Path("/proc/stat").read_text().splitlines():
            if not line.startswith("cpu"):
                break
            fields = line.split()
            values = [int(v) for v in fields[1:]]
            # user nice system idle iowait irq softirq steal ...
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            out[fields[0]] = (sum(values), idle)
    except (OSError, ValueError, IndexError):
        return {}
    return out


def _owned(entry, role):
    """Whether a process is this channel's unit for that role.

    Two channels run the same scripts out of the same tree, so "feeder.py" on
    a command line says what a process does and nothing about whose it is. The
    cgroup says: it carries the unit that started it. The .service suffix
    matters, or vodloop-push also matches vodloop-push@othername.service and
    the first channel reports the second one's numbers as its own.
    """
    try:
        return f"{common.unit(role)}.service" in (entry / "cgroup").read_text()
    except OSError:
        return False


def _pids():
    """The pid of each service worth watching, found by its command line."""
    found = {}
    try:
        entries = list(pathlib.Path("/proc").iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            line = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace")
        except OSError:
            continue  # it exited between the listing and the read
        for label, needles in WATCHED.items():
            if (label not in found and all(w in line for w in needles)
                    and _owned(entry, label)):
                found[label] = int(entry.name)
    return found


def _proc_jiffies(pid):
    try:
        fields = (pathlib.Path(f"/proc/{pid}/stat").read_text()
                  .rpartition(")")[2].split())
        return int(fields[11]) + int(fields[12])  # utime + stime
    except (OSError, ValueError, IndexError):
        return None


def cpu(window=1.0):
    """Busy percentage over one second: the box, each vCPU, and the three
    processes that matter.

    Sampled as a difference on purpose. /proc/stat counts since boot, and a
    total since boot says nothing at all about what the machine is doing now,
    which is the only thing worth watching on two vCPU that also carry a live
    stream.
    """
    before, pids = _cpu_jiffies(), _pids()
    before_proc = {k: _proc_jiffies(v) for k, v in pids.items()}
    if not before:
        return {"cpu_error": "pas de /proc/stat"}
    time.sleep(window)
    after = _cpu_jiffies()
    after_proc = {k: _proc_jiffies(v) for k, v in pids.items()}

    def busy(name):
        if name not in before or name not in after:
            return None
        total = after[name][0] - before[name][0]
        idle = after[name][1] - before[name][1]
        return round(100.0 * (total - idle) / total, 1) if total > 0 else None

    ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    cores = sorted(k for k in after if k != "cpu")
    row = {"cpu_count": len(cores) or os.cpu_count(),
           "cpu_busy_pct": busy("cpu"),
           "cpu_per_core": [busy(name) for name in cores]}
    try:
        one, five, fifteen = os.getloadavg()
        row.update({"load1": round(one, 2), "load5": round(five, 2),
                    "load15": round(fifteen, 2)})
    except (OSError, AttributeError):
        pass
    for label in WATCHED:
        start, end = before_proc.get(label), after_proc.get(label)
        row[f"cpu_{label}_pct"] = (
            round(100.0 * (end - start) / ticks / window, 1)
            if start is not None and end is not None else None)
    return row


# --- what is on the wire, and what it was made from -----------------------

def on_air_chunk():
    """The chunk the feeder is sending right now, or None.

    The feeder plays the oldest ready chunk and deletes it once sent, so while
    it plays it is still the first one in the list. This used to read the
    NEWEST chunk, the one prep had just cut and that would air hours later.
    Measured 2026-09-12 21:15: the monitor reported 1920x1080 at 60 and 4781
    kbps while the channel was pushing 640x360 at 534 kbps, so the one alarm
    written for a stream collapsing under a megabit could never fire on it.
    """
    chunks = common.ready_segments()
    return chunks[0] if chunks else None


def streams(path):
    """Codec, geometry and rates of one file, however it is wrapped."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,profile,width,height,r_frame_rate,"
             "bit_rate,sample_rate,channels:format=duration,bit_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120)
        info = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    found = info.get("streams") or []
    video = next((s for s in found if s.get("codec_type") == "video"), {})
    audio = next((s for s in found if s.get("codec_type") == "audio"), {})
    fmt = info.get("format") or {}
    total = number(fmt.get("bit_rate"))
    abits = number(audio.get("bit_rate"))
    return {
        "duration": number(fmt.get("duration")),
        "total_bps": total,
        # MPEG-TS carries no per-stream rate for the video, so there it is what
        # is left of the file once the sound is taken out of it. An mp4 source
        # states it outright, which is why that is preferred when present.
        "video_bps": number(video.get("bit_rate")) or (
            (total - abits) if (total and abits) else None),
        "vcodec": video.get("codec_name"),
        "vprofile": video.get("profile"),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": rate(video.get("r_frame_rate")),
        "acodec": audio.get("codec_name"),
        "abitrate": abits,
        "asample_rate": number(audio.get("sample_rate")),
        "achannels": audio.get("channels"),
    }


def source_of(chunk):
    """The library file a chunk was made from, via the id in its name."""
    match = re.match(r"^(\d+)_", chunk.name)
    if not match:
        return None
    wanted = int(match.group(1))
    try:
        queue = common.load_queue()
    except (OSError, ValueError):
        return None
    for item in queue.get("items", []):
        if item.get("id") == wanted and item.get("path"):
            path = pathlib.Path(item["path"])
            return path if path.is_file() else None
    return None


def compare(chunk_info, source_info):
    """Whether the wire carries the source untouched, and by how much it moved.

    Equal codec, size and frame rate means the packets were copied: there is no
    generation to lose, and no amount of bitrate arithmetic adds to that. The
    ratio is reported beside it because a chunk is a five minute slice of a
    longer file and moves with what is on screen, so it confirms rather than
    proves.
    """
    if not source_info:
        return {}
    out = {"source_" + k: v for k, v in source_info.items()
           if k in ("vcodec", "width", "height", "fps", "video_bps",
                    "acodec", "abitrate", "asample_rate", "achannels")}
    # The frame rate is compared within a tolerance: a copied variable rate file
    # reads 29263/1000 in its mkv and 117/4 once in mpegts, which is the same
    # packets described twice. Exact equality called that a re-encode on
    # 2026-09-12 20:20 with no encoder running.
    mine_fps, theirs_fps = chunk_info.get("fps"), source_info.get("fps")
    out["copied_video"] = bool(
        chunk_info.get("vcodec") and chunk_info["vcodec"] == source_info.get("vcodec")
        and chunk_info.get("width") == source_info.get("width")
        and chunk_info.get("height") == source_info.get("height")
        and (mine_fps == theirs_fps
             or (mine_fps is not None and theirs_fps is not None
                 and abs(mine_fps - theirs_fps) < 0.1)))
    out["copied_audio"] = bool(
        chunk_info.get("acodec") and chunk_info["acodec"] == source_info.get("acodec")
        and chunk_info.get("asample_rate") == source_info.get("asample_rate")
        and chunk_info.get("achannels") == source_info.get("achannels"))
    for kind, mine_key, theirs_key in (("video", "video_bps", "video_bps"),
                                       ("audio", "abitrate", "abitrate")):
        mine, theirs = chunk_info.get(mine_key), source_info.get(theirs_key)
        out[f"{kind}_ratio"] = (round(mine / theirs, 3)
                                if mine and theirs else None)
    return out


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
    chunk = on_air_chunk()
    row["chunk"] = chunk.name if chunk else None
    if chunk is not None:
        info = streams(chunk)
        if info is None:
            row["chunk_error"] = "illisible"
        else:
            row.update(info)
            source = source_of(chunk)
            row["source"] = source.name if source else None
            if source is not None:
                row.update(compare(info, streams(source) or {}))
    row.update(cpu())
    row.update(kick())
    if row.get("is_live"):
        row.update(ladder(row.get("playback_url"), (row.get("width"), row.get("height"))))
    # the url is a signed handle that changes every sample and is worth nothing
    # once read, so it is not kept in a log that lives for six weeks
    row.pop("playback_url", None)
    return row


# --- reading it back ------------------------------------------------------

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
    aired = row.get("rung_height")
    if WANTED_HEIGHT and aired and aired < WANTED_HEIGHT:
        found.append(f"diffusion en {aired}p alors que {WANTED_HEIGHT}p est demande")
    if not row.get("chunk"):
        found.append("aucun chunk sur le disque")
    elif row.get("chunk_error"):
        # a chunk ffprobe cannot read is the shape of the fault that took the
        # channel down on 2026-09-05, so it counts as one rather than as a gap
        found.append(f"chunk illisible ({row['chunk_error']})")
    else:
        video = row.get("video_bps")
        # a partial chunk is the tail of a video and measures light through no
        # fault of anything, so it does not get to raise an alarm
        full = (row.get("duration") or 0) >= common.CHUNK_SECONDS * 0.9
        # The floors only speak where the comparison to the source cannot. A
        # chunk that matched its source codec for codec carries exactly what
        # came off YouTube, so a thin one is thin content and not a loss, and
        # the pipeline has nothing to answer for. This is the third time an
        # absolute floor here has been wrong about a library that changed under
        # it: 150k audio against YouTube's 128k, then 2.4 Mbps video, and a
        # 40 528 s stream that YouTube serves at 690 kbps in 720p30 would have
        # been the next. Where a copy did not happen, or there is no source to
        # compare against, the net stays up.
        unexplained_video = not row.get("copied_video")
        unexplained_audio = not row.get("copied_audio")
        if video is not None and full and unexplained_video and video < MIN_VIDEO_BPS:
            found.append(f"video a {video // 1000} kbps (< {MIN_VIDEO_BPS // 1000})")
        audio = row.get("abitrate")
        if audio is not None and unexplained_audio and audio < MIN_AUDIO_BPS:
            found.append(f"audio a {audio // 1000} kbps (< {MIN_AUDIO_BPS // 1000})")
        # The one this file exists for now. Nothing is supposed to be encoded
        # any more, so a picture that does not match its source did not get here
        # by copying, and that is a regression someone has to hear about.
        if row.get("source"):
            if row.get("copied_video") is False:
                found.append(
                    f"REENCODAGE video: {row.get('vcodec')} "
                    f"{row.get('width')}x{row.get('height')}@{row.get('fps')} "
                    f"a l'antenne contre {row.get('source_vcodec')} "
                    f"{row.get('source_width')}x{row.get('source_height')}"
                    f"@{row.get('source_fps')} a la source")
            if row.get("copied_audio") is False:
                found.append(
                    f"REENCODAGE audio: {row.get('acodec')} "
                    f"{row.get('asample_rate')}/{row.get('achannels')}ch a "
                    f"l'antenne contre {row.get('source_acodec')} "
                    f"{row.get('source_asample_rate')}/"
                    f"{row.get('source_achannels')}ch a la source")
            if full:
                for kind in ("video", "audio"):
                    ratio = row.get(f"{kind}_ratio")
                    if ratio is not None and abs(ratio - 1.0) > BITRATE_TOLERANCE:
                        found.append(
                            f"debit {kind} a {int(ratio * 100)}% de la source")
    if row.get("kick_http") == 200 and row.get("is_live") is False:
        found.append("la chaine n'est pas en ligne")
    # What Kick hands a player, as opposed to what was handed to Kick. The
    # source rung is the only one nothing re-encoded, so it has to be the
    # fattest one on the ladder; when it is not, every player choosing by
    # bandwidth takes one of Kick's own encodes and upscales it, which looks
    # like a bad 1080p rather than like an outage and so is never reported.
    source_bps = row.get("rung_bps")
    other_bps = row.get("rung_best_smaller_bps")
    if source_bps and other_bps and other_bps > source_bps:
        # deliberately without the two numbers in it. The alert de-dupes on the
        # text of the fault, and this one stands for hours at a time while both
        # numbers move every sample, so spelling them out here would send a
        # message every five minutes instead of one every thirty. They are in
        # the rung line of the detail block that goes out with the alert.
        found.append("RUNG: un encodage de Kick est annonce plus gros que la "
                     "source, les lecteurs le prendront et l'upscaleront")
    if row.get("ladder_error"):
        found.append(f"ladder: {row['ladder_error']}")
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


def kbps(value):
    return f"{value // 1000}k" if value else "?"


def verdict(row):
    if not row.get("source"):
        return "?"
    if row.get("copied_video") and row.get("copied_audio"):
        return "copie"
    return "REENCODE"


def one_line(row):
    """One sample, compact, for reading a run of them at a glance."""
    when = time.strftime("%m-%d %H:%M", time.localtime(row.get("ts", 0)))
    return (f"{when} {row.get('width') or '?'}x{row.get('height') or '?'}"
            f"@{row.get('fps') or '?'} v={kbps(row.get('video_bps'))} "
            f"a={kbps(row.get('abitrate'))} {verdict(row)} "
            f"cpu={row.get('cpu_busy_pct')}% tampon={row.get('buffer_s')}s "
            f"kick={'live' if row.get('is_live') else 'off'}/"
            f"{row.get('viewer_count') if row.get('viewer_count') is not None else '?'}")


def detail(row):
    """One sample in full: the source on the left, the wire on the right."""
    lines = [
        f"chunk    {row.get('chunk') or '-'}  ({row.get('duration')}s)",
        f"source   {row.get('source') or 'inconnue'}",
        "",
        f"  video  source {row.get('source_vcodec')} "
        f"{row.get('source_width')}x{row.get('source_height')}"
        f"@{row.get('source_fps')} {kbps(row.get('source_video_bps'))}",
        f"         antenne {row.get('vcodec')} "
        f"{row.get('width')}x{row.get('height')}@{row.get('fps')} "
        f"{kbps(row.get('video_bps'))}   ratio {row.get('video_ratio')}",
        f"  audio  source {row.get('source_acodec')} "
        f"{row.get('source_asample_rate')}/{row.get('source_achannels')}ch "
        f"{kbps(row.get('source_abitrate'))}",
        f"         antenne {row.get('acodec')} "
        f"{row.get('asample_rate')}/{row.get('achannels')}ch "
        f"{kbps(row.get('abitrate'))}   ratio {row.get('audio_ratio')}",
        f"  verdict {verdict(row)}  (video copiee={row.get('copied_video')}, "
        f"audio copiee={row.get('copied_audio')})",
        "",
        f"  cpu      {row.get('cpu_busy_pct')}% sur {row.get('cpu_count')} vcpu "
        f"{row.get('cpu_per_core')}",
        f"  charge   {row.get('load1')} / {row.get('load5')} / {row.get('load15')}",
        f"  process  push={row.get('cpu_push_pct')}% "
        f"feed={row.get('cpu_feed_pct')}% prep={row.get('cpu_prep_pct')}%",
        f"  disque   {(row.get('free_bytes') or 0) // 1024 ** 3} Go libres, "
        f"tampon {row.get('buffer_s')}s",
        f"  kick     http={row.get('kick_http')} live={row.get('is_live')} "
        f"spectateurs={row.get('viewer_count')}",
        # Recorded rather than compared. The buffer runs up to half an hour, so
        # the chunk measured above is not the one on air yet, and asserting the
        # two agree would fire on every change of source rather than on a fault.
        # What this is for is the day a 720p file follows a 1080p one: if Kick
        # keeps advertising the old shape, it is in this line that it shows.
        f"  rung     source {row.get('rung_width')}x{row.get('rung_height')}"
        f"@{row.get('rung_fps')} {kbps(row.get('rung_bps'))} sur "
        f"{row.get('rung_count')} rungs, meilleur plus petit "
        f"{kbps(row.get('rung_best_smaller_bps'))}"
        f"{'  ' + row['ladder_error'] if row.get('ladder_error') else ''}",
    ]
    return "\n".join(lines)


def report(count, to_telegram=False):
    rows = read_rows(count)
    if not rows:
        message = f"aucune mesure dans {LOG}"
        print(message)
        if to_telegram:
            tgbot.say(message)
        return 1
    body = [one_line(row) for row in rows]
    body += ["", detail(rows[-1]), ""]
    last = faults(rows[-1])
    body.append("BAISSE: " + " | ".join(last) if last
                else f"rien a signaler sur {len(rows)} mesure(s)")
    message = "\n".join(body)
    print(message)
    if to_telegram:
        tgbot.say(message)
    return 1 if last else 0


def alert(row):
    """Tell Telegram about a fault, once per distinct fault per cooldown."""
    found = faults(row)
    if not found:
        return
    key = " | ".join(found)
    # a fault nothing can repair in the next half hour does not get to say so
    # every half hour. Mixed with anything else it is a new situation and pages
    # at the normal rate.
    cooldown = (STANDING_COOLDOWN_SECONDS
                if all(f.startswith(STANDING_PREFIX) for f in found)
                else ALERT_COOLDOWN_SECONDS)
    now = time.time()
    try:
        seen = json.loads(ALERTS.read_text())
        seen = seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        seen = {}
    if now - float(seen.get(key, 0) or 0) < cooldown:
        return
    if not tgbot.say("qualite: " + key + "\n\n" + detail(row)):
        return  # unsent, so unrecorded: the next pass has to try again
    seen = {k: v for k, v in seen.items() if now - float(v or 0) < 24 * 3600}
    seen[key] = now
    try:
        common.STATE.mkdir(parents=True, exist_ok=True)
        tmp = ALERTS.with_suffix(".tmp")
        tmp.write_text(json.dumps(seen))
        tmp.replace(ALERTS)
    except OSError:
        pass  # an alert that repeats is better than one that never fires


def main(argv):
    to_telegram = "--telegram" in argv
    rest = [a for a in argv if not a.startswith("--")]
    if "--report" in argv:
        try:
            count = int(rest[0]) if rest else 20
        except ValueError:
            count = 20
        return report(max(1, count), to_telegram)
    row = sample()
    append(row)
    print(json.dumps(row), flush=True)
    if to_telegram:
        alert(row)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
