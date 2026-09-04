#!/usr/bin/env python3
"""Turn queued video URLs into uniform MPEG-TS chunks, staying ahead of playback.

The source is never stored: yt-dlp streams into ffmpeg and only the normalised
chunks touch the disk. Every chunk shares the exact encode settings in common.py,
which is what lets the feeder concatenate them without restarting the pusher.

Only publicly reachable videos are handled. There is deliberately no support for
supplying an account session, so a video behind a sign-in check is reported as an
error on the queue item rather than retried by other means.
"""
import os
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


def encode_budget(item):
    """How long this item's encode may run, in seconds.

    Tied to the video's own duration rather than fixed, because a four hour
    video legitimately takes hours on this machine while a three minute clip
    that has not finished in half an hour is stuck.
    """
    try:
        length = float(item.get("duration") or 0)
    except (TypeError, ValueError):
        length = 0.0
    if length <= 0:
        return common.ENCODE_TIMEOUT_CEILING
    budget = length * common.ENCODE_TIMEOUT_FACTOR
    return max(common.ENCODE_TIMEOUT_FLOOR,
               min(budget, common.ENCODE_TIMEOUT_CEILING))


def matches_target(path):
    """Whether a file already holds exactly the picture a chunk must carry.

    Only the video is judged. The audio is transcoded either way, so letting it
    differ costs nothing, while a single re-encoded picture costs more wall time
    than the video it produces buys back.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,width,height,r_frame_rate", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    want = f"h264,{common.WIDTH},{common.HEIGHT},{common.FPS}/1"
    return out.strip().splitlines()[:1] == [want]


def normalise_in_place(path):
    """Re-encode a library file into the exact shape a chunk must have, once.

    A library file is replayed forever. Encoding it on every pass costs more
    wall time than the video buys back on this box, so the queue can never get
    ahead and the channel falls back to the standby clip. Doing it once here
    means no one has to remember to run a tool after adding videos.

    The replacement is a rename, so a reader already holding the old file keeps
    reading it, and a failure leaves the original untouched.
    """
    target = path.with_suffix(".norm.mp4")
    print(f"normalisation ({path.name})", flush=True)
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(path)] + list(common.ENCODE)
            + ["-f", "mp4", str(target)],
            capture_output=True, text=True, timeout=common.ENCODE_TIMEOUT_CEILING)
    except (OSError, subprocess.SubprocessError) as exc:
        target.unlink(missing_ok=True)
        print(f"  echec: {exc}", flush=True)
        return None
    if out.returncode != 0 or not target.exists():
        target.unlink(missing_ok=True)
        print(f"  echec: {(out.stderr or '').strip().splitlines()[-1:]}", flush=True)
        return None
    final = path.with_suffix(".mp4")
    target.replace(final)
    if final != path:
        path.unlink(missing_ok=True)
    print(f"  fait: {final.name}", flush=True)
    return final


def prepare(item):
    """Stream one URL through ffmpeg into numbered chunks. True on success."""
    common.SEGMENTS.mkdir(parents=True, exist_ok=True)
    pattern = str(common.SEGMENTS / f"{item['id']:05d}_%05d.ts")

    title_file = common.STATE / f"title_{item['id']:05d}.txt"
    caption = common.clean_text(item.get("title"), 70)
    if item.get("by_name"):
        caption = f"{caption}   -   requested by {common.clean_text(item['by_name'], 24)}"
    title_file.write_text(caption, encoding="utf-8")

    encode = list(common.ENCODE)
    encode[encode.index("-vf") + 1] = overlay_filter(title_file)
    if item.get("path"):
        source = pathlib.Path(item["path"])
        # a library file in the wrong shape is normalised once instead of being
        # re-encoded on every pass through the rotation
        if not matches_target(source) and not consumable(source):
            fixed = normalise_in_place(source)
            if fixed is not None:
                item["path"] = str(fixed)
                source = fixed
        if matches_target(source):
            # nothing to redraw, so the title overlay goes with it: a caption is
            # not worth a channel that cannot keep a picture on the wire
            encode = list(common.REMUX)

    # the muxer's own record of what it wrote. Counting the files instead would
    # be wrong: the feeder deletes each chunk as it plays it, and on a dry queue
    # it can eat the whole video before ffmpeg returns.
    listing = common.STATE / f"list_{item['id']:05d}.txt"
    tail = ["-f", "segment", "-segment_time", str(common.CHUNK_SECONDS),
            "-segment_format", "mpegts", "-segment_list", str(listing),
            "-reset_timestamps", "1", pattern]

    if item.get("path"):
        # a file handed over directly: ffmpeg reads it, no downloader involved
        puller = None
        encoder = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", item["path"]]
            + encode + tail,
            stderr=subprocess.PIPE,
        )
    else:
        puller = subprocess.Popen(
            BASE + ["-f", FORMAT, "-o", "-", "--", item["url"]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        encoder = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
            + encode + tail,
            stdin=puller.stdout, stderr=subprocess.PIPE,
        )
        puller.stdout.close()  # so yt-dlp sees EPIPE if ffmpeg dies first

    # A wall clock on the encode. Without it one hostile or pathological input
    # pins the encoder for as long as it likes on a box with two vCPUs, and the
    # queue behind it never moves again. The budget follows the video's own
    # length so a legitimately long one is not cut off: several times realtime,
    # with a floor for short clips and a ceiling for anything that reports no
    # duration at all.
    budget = encode_budget(item)
    try:
        enc_err = encoder.communicate(timeout=budget)[1].decode(errors="replace")
        pull_err = puller.communicate(timeout=60)[1].decode(errors="replace") if puller else ""
    except subprocess.TimeoutExpired:
        for process in (encoder, puller):
            if process is None:
                continue
            process.kill()
            try:
                process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        for chunk in common.SEGMENTS.glob(f"{item['id']:05d}_*.ts"):
            chunk.unlink(missing_ok=True)  # half a video is worse than none
        listing.unlink(missing_ok=True)
        item["error"] = f"gave up after {int(budget / 60)} min"
        return False

    produced = listing.read_text().split() if listing.exists() else []
    listing.unlink(missing_ok=True)
    if encoder.returncode != 0 or not produced:
        message = (pull_err or enc_err or "no output").strip()
        item["error"] = (message.splitlines() or ["failed"])[-1][:200]
        return False
    item["chunks"] = len(produced)
    return True


MEDIA_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".ts", ".m4v", ".avi"}
SETTLE_SECONDS = 30


def take_dropped_files(queue):
    """Queue any video file dropped in incoming/.

    This path exists because a channel's videos are not always fetchable, while
    the people who made them can simply hand over the files. Nothing here goes
    through the downloader, and chat cannot reach it: the command parser only
    ever produces YouTube ids, never a path.
    """
    common.INCOMING.mkdir(parents=True, exist_ok=True)
    known = {i.get("path") for i in queue["items"] if i.get("path")}
    for candidate in sorted(common.INCOMING.iterdir()):
        if not candidate.is_file() or candidate.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if str(candidate) in known:
            continue
        # a file still being copied must not be handed to ffmpeg half-written
        if time.time() - candidate.stat().st_mtime < SETTLE_SECONDS:
            continue
        queue["seq"] += 1
        queue["items"].append({
            "id": queue["seq"],
            "url": candidate.name,
            "path": str(candidate),
            "status": "pending",
            "by": "file",
            "by_name": "",
            "title": common.clean_text(candidate.stem, 120),
            "votes": [],
            "added_at": time.time(),
        })
        print(f"queued from incoming/: {candidate.name}", flush=True)


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


# A folder of files to fall back on when nothing is queued, so the channel keeps
# playing instead of sitting on the standby clip. Unset means the behaviour is
# exactly what it was: run dry and show the filler.
LIBRARY = pathlib.Path(os.environ["VODLOOP_LIBRARY"]) if os.environ.get("VODLOOP_LIBRARY") else None
REFILL_BELOW_SECONDS = 10 * 60


def consumable(path):
    """Whether playing this file is allowed to consume it.

    A file dropped in incoming/ is handed over for a single play and is removed
    once it has been encoded. A library file is queued where it lives and has to
    survive being played, or the first pass through the rotation deletes the
    library it is supposed to replay.
    """
    return pathlib.Path(path).parent == common.INCOMING


def refill_from_library(queue):
    """Put the library back in the queue once it has been played through.

    The files are queued where they are. Copying them into incoming/ first, as
    this did, doubles the disk for nothing: a hard link cannot cross the bind
    mounts the unit sandboxes incoming/ with, so every pass fell back to a real
    copy of the whole library.

    It only fires when there is nothing left to encode and the backlog is nearly
    gone, so playback is what paces it, not this function.
    """
    if LIBRARY is None or not LIBRARY.is_dir():
        return 0
    waiting = [i for i in queue["items"] if i["status"] in ("pending", "preparing")]
    if waiting or seconds_on_disk() >= REFILL_BELOW_SECONDS:
        return 0

    sources = sorted(p for p in LIBRARY.iterdir()
                     if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES)
    if not sources:
        return 0

    # the played entries naming these same files go first, or the queue keeps a
    # dead copy of the whole library on every pass
    targets = {str(p) for p in sources}
    queue["items"] = [i for i in queue["items"] if i.get("path") not in targets]
    for src in sources:
        queue["seq"] += 1
        queue["items"].append({
            "id": queue["seq"],
            "url": src.name,
            "path": str(src),
            "status": "pending",
            "by": "file",
            "by_name": "",
            "title": common.clean_text(src.stem, 120),
            "votes": [],
            "added_at": time.time(),
        })
    print(f"bibliotheque remise en file: {len(sources)} fichier(s)", flush=True)
    return len(sources)


def recover_orphans(queue):
    """Put back anything left mid-encode by a previous run.

    This process is the only thing that ever writes "preparing", so at startup
    nothing can legitimately be in that state: whatever is there was interrupted,
    and the loop only ever looks at "pending", so it would sit untouched forever.
    Twice on 2026-09-03 a restart of this unit left an item stranded that way and
    the queue quietly stopped moving behind it.

    The chunks it half wrote go too. Half a video reaching the stream is worse
    than the item being encoded again.
    """
    recovered = 0
    for item in queue["items"]:
        if item["status"] != "preparing":
            continue
        item["status"] = "pending"
        item.pop("error", None)
        recovered += 1
        for chunk in common.SEGMENTS.glob(f"{item['id']:05d}_*.ts"):
            chunk.unlink(missing_ok=True)
    if recovered:
        print(f"reprise: {recovered} item(s) laisses en cours par un arret", flush=True)
    return recovered


def disk_is_tight():
    return shutil.disk_usage(common.ROOT).free < common.MIN_FREE_BYTES


def main():
    first = True
    while True:
        queue = common.load_queue()
        if first:
            # once, before anything else: whatever the previous run left mid
            # encode has to go back in the pile or it blocks the queue forever
            if recover_orphans(queue):
                common.save_queue(queue)
            first = False
        reap(queue)
        # before take_dropped_files, which is what actually queues them
        refill_from_library(queue)
        take_dropped_files(queue)
        # chat votes decide the order; ties fall back to who asked first
        pending = chatlogic.playback_order(queue)

        if not pending or seconds_on_disk() >= common.AHEAD_LIMIT_SECONDS \
                or disk_is_tight():
            common.save_queue(queue)
            time.sleep(POLL_SECONDS)
            continue

        item = pending[0]

        if item.get("path"):
            # handed over by the operator, not requested by a stranger: there is
            # no publisher to check and no downloader to ask
            item["status"] = "preparing"
            common.save_queue(queue)
            ok = prepare(item)
            if not ok:
                if consumable(item["path"]):
                    failed = common.INCOMING / "failed"
                    failed.mkdir(exist_ok=True)
                    try:
                        pathlib.Path(item["path"]).rename(failed / pathlib.Path(item["path"]).name)
                    except OSError:
                        pass
            elif consumable(item["path"]):
                pathlib.Path(item["path"]).unlink(missing_ok=True)
            queue = common.load_queue()
            for entry in queue["items"]:
                if entry["id"] == item["id"]:
                    entry.update(item)
                    entry["status"] = "ready" if ok else "error"
            common.save_queue(queue)
            continue

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
            item["error"] = f"too long ({int(length / 3600)}h)"
            common.save_verdict(item.get("video_id"), False, item["error"])
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
            # remembered, so the next person who pastes it is refused for free
            common.save_verdict(item.get("video_id"), False, item["error"])
            common.save_queue(queue)
            continue

        common.save_verdict(item.get("video_id"), True)
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
