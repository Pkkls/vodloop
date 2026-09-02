#!/usr/bin/env python3
"""Turn queued video URLs into uniform MPEG-TS chunks, staying ahead of playback.

The source is never stored: yt-dlp streams into ffmpeg and only the normalised
chunks touch the disk. Every chunk shares the exact encode settings in common.py,
which is what lets the feeder concatenate them without restarting the pusher.

Only publicly reachable videos are handled. There is deliberately no support for
supplying an account session, so a video behind a sign-in check is reported as an
error on the queue item rather than retried by other means.
"""
import pathlib
import shutil
import subprocess
import sys
import time

import chatlogic
import common

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def overlay_filter(title_file):
    """Burn the title into the picture while prep is already re-encoding, so the
    overlay costs nothing at push time, where the stream is only remuxed.

    The title comes from YouTube, so a hostile one must not be able to reach the
    filter graph. Two things prevent that: the text is read from a file instead
    of being spliced into the graph string, and expansion is off so a title
    containing %{...} is drawn literally rather than evaluated.
    """
    if not pathlib.Path(FONT).exists():
        return common.VFILTER
    escaped = str(title_file).replace("\\", "/").replace(":", r"\:")
    return (
        common.VFILTER + ",drawtext=fontfile=" + FONT
        + f":textfile={escaped}:expansion=none:reload=0"
        + ":fontsize=22:fontcolor=white@0.85:box=1:boxcolor=black@0.45"
        + ":boxborderw=10:x=28:y=h-th-28"
    )

YTDLP = shutil.which("yt-dlp") or str(common.ROOT.parent / ".local/bin/yt-dlp")
FORMAT = "bv*[height<=1080][vcodec^=avc1]+ba/b[height<=1080]/b"
POLL_SECONDS = 10
BASE = [YTDLP, "--no-warnings", "--no-progress"]


def probe(url):
    """Title, duration and publishing channel, without downloading."""
    out = subprocess.run(
        BASE + ["--simulate", "--print",
                "%(title)s\t%(duration)s\t%(channel_id)s\t%(channel)s", url],
        capture_output=True, text=True, timeout=180,
    )
    if out.returncode != 0:
        last = (out.stderr.strip().splitlines() or ["extraction failed"])[-1]
        return None, last
    line = (out.stdout.strip().splitlines() or [""])[-1]
    parts = (line.split("\t") + ["", "", "", ""])[:4]
    return {"title": common.clean_text(parts[0], 120), "duration": parts[1],
            "channel_id": parts[2].strip(),
            "channel": common.clean_text(parts[3], 60)}, None


def seconds_on_disk():
    """Rough backlog size. Chunks are CHUNK_SECONDS except the last of a video,
    which is close enough for a threshold and far cheaper than probing each file."""
    return len(common.ready_segments()) * common.CHUNK_SECONDS


def prepare(item):
    """Stream one URL through ffmpeg into numbered chunks. True on success."""
    common.SEGMENTS.mkdir(parents=True, exist_ok=True)
    pattern = str(common.SEGMENTS / f"{item['id']:05d}_%05d.ts")

    title_file = common.STATE / f"title_{item['id']:05d}.txt"
    caption = common.clean_text(item.get("title"), 70)
    if item.get("by_name"):
        caption = f"{caption}   -   demande par {common.clean_text(item['by_name'], 24)}"
    title_file.write_text(caption, encoding="utf-8")

    encode = list(common.ENCODE)
    encode[encode.index("-vf") + 1] = overlay_filter(title_file)

    puller = subprocess.Popen(
        BASE + ["-f", FORMAT, "-o", "-", "--", item["url"]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    encoder = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
        + encode
        + ["-f", "segment", "-segment_time", str(common.CHUNK_SECONDS),
           "-segment_format", "mpegts", "-reset_timestamps", "1", pattern],
        stdin=puller.stdout, stderr=subprocess.PIPE,
    )
    puller.stdout.close()  # so yt-dlp sees EPIPE if ffmpeg dies first
    enc_err = encoder.communicate()[1].decode(errors="replace")
    pull_err = puller.communicate()[1].decode(errors="replace")

    produced = list(common.SEGMENTS.glob(f"{item['id']:05d}_*.ts"))
    if encoder.returncode != 0 or not produced:
        message = (pull_err or enc_err or "no output").strip()
        item["error"] = (message.splitlines() or ["failed"])[-1][:200]
        return False
    item["chunks"] = len(produced)
    return True


def reap(queue):
    """A ready item whose chunks have all been consumed has finished playing."""
    for item in queue["items"]:
        if item["status"] != "ready":
            continue
        if not list(common.SEGMENTS.glob(f"{item['id']:05d}_*.ts")):
            item["status"] = "played"
            (common.STATE / f"title_{item['id']:05d}.txt").unlink(missing_ok=True)

    # Chunks whose item no longer exists would otherwise sit there forever and,
    # worse, be mistaken for a later item that reuses the number.
    live = {f"{i['id']:05d}" for i in queue["items"]
            if i["status"] in ("preparing", "ready")}
    for chunk in common.ready_segments():
        if chunk.name.split("_")[0] not in live:
            chunk.unlink(missing_ok=True)


def disk_is_tight():
    return shutil.disk_usage(common.ROOT).free < common.MIN_FREE_BYTES


def main():
    while True:
        queue = common.load_queue()
        reap(queue)
        # chat votes decide the order; ties fall back to who asked first
        pending = chatlogic.playback_order(queue)

        if not pending or seconds_on_disk() >= common.AHEAD_LIMIT_SECONDS \
                or disk_is_tight():
            common.save_queue(queue)
            time.sleep(POLL_SECONDS)
            continue

        item = pending[0]
        meta, err = probe(item["url"])
        if err:
            item["status"] = "error"
            item["error"] = err[:200]
            common.save_queue(queue)
            continue

        try:
            length = float(meta["duration"])
        except (TypeError, ValueError):
            length = 0.0
        if length > common.MAX_DURATION_SECONDS:
            item["status"] = "error"
            item["error"] = f"trop long ({int(length / 3600)}h)"
            common.save_queue(queue)
            continue

        # Who published it is the only control that holds. A chat member can
        # queue anything YouTube hosts, and a video that gets the Kick channel
        # banned is indistinguishable from any other by its title or its words.
        if not common.channel_allowed(meta["channel_id"]):
            item["status"] = "error"
            item["error"] = "channel not on the allowlist"
            item["channel"] = meta["channel"]
            item["channel_id"] = meta["channel_id"]
            common.save_queue(queue)
            continue

        item.update(meta)
        item["status"] = "preparing"
        common.save_queue(queue)

        ok = prepare(item)

        queue = common.load_queue()  # reload: the dashboard may have edited it
        for entry in queue["items"]:
            if entry["id"] == item["id"]:
                entry.update(item)
                entry["status"] = "ready" if ok else "error"
        common.save_queue(queue)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
