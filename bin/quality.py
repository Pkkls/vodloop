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

# A chunk prep is still writing measures short and light, so it is skipped in
# favour of the newest one nothing has touched for a while.
SETTLED_SECONDS = 30
# ponytail: the file is trimmed to its second half once it passes this, rather
# than rotated. 288 samples a day is about 250 ko, so this holds roughly six
# weeks, and the disk this runs on has a prep that stops preparing below 4 Go.
MAX_LOG_BYTES = 4 * 1024 ** 2
# The same fault every five minutes is not monitoring, it is noise someone
# learns to ignore. One message per distinct fault per this long.
ALERT_COOLDOWN_SECONDS = 30 * 60

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
            "viewer_count": (stream or {}).get("viewer_count")}


# --- what the machine is doing --------------------------------------------

# Matched against a process command line, all words present. The pusher has no
# script name to match on: it is a bare ffmpeg, and what makes it the pusher is
# where it is sending.
WATCHED = {"push": ("ffmpeg", "flv", "rtmps"),
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
            if label not in found and all(w in line for w in needles):
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
    out["copied_video"] = bool(
        chunk_info.get("vcodec") and chunk_info["vcodec"] == source_info.get("vcodec")
        and chunk_info.get("width") == source_info.get("width")
        and chunk_info.get("height") == source_info.get("height")
        and chunk_info.get("fps") == source_info.get("fps"))
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
    chunk = newest_chunk()
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
        if video is not None and full and video < MIN_VIDEO_BPS:
            found.append(f"video a {video // 1000} kbps (< {MIN_VIDEO_BPS // 1000})")
        audio = row.get("abitrate")
        if audio is not None and audio < MIN_AUDIO_BPS:
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
    now = time.time()
    try:
        seen = json.loads(ALERTS.read_text())
        seen = seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        seen = {}
    if now - float(seen.get(key, 0) or 0) < ALERT_COOLDOWN_SECONDS:
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
