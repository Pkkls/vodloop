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
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

import chatlogic
import common

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _drawtext(text_file, size, alpha, y):
    """One drawtext box reading its text from a file.

    The title comes from YouTube, so a hostile one must not be able to reach the
    filter graph. Two things prevent that: the text is read from a file instead
    of being spliced into the graph string, and expansion is off so a title
    containing %{...} is drawn literally rather than evaluated.
    """
    escaped = str(text_file).replace("\\", "/").replace(":", r"\:")
    return (
        "drawtext=fontfile=" + FONT
        + f":textfile={escaped}:expansion=none:reload=0"
        + f":fontsize={size}:fontcolor=white@{alpha}"
        + ":box=1:boxcolor=black@0.45:boxborderw=10"
        + f":x=28:y={y}"
    )


def overlay_filter(title_file, next_file=None):
    """Burn what is playing, and what follows, while prep is already re-encoding
    so the overlay costs nothing at push time, where the stream is only remuxed.

    Two stacked boxes rather than one two-line box: the second line is the
    weaker information and reads as such only if it is smaller and dimmer.

    The "next" line is what the queue says at encode time. A vote landing later
    can change the real order, and this text cannot follow it: drawtext bakes
    pixels. It is a strong hint, not a promise, which is why it is worded as
    one. An always-accurate answer is what !next and the dashboard are for.
    """
    if not pathlib.Path(FONT).exists():
        return common.VFILTER
    parts = [common.VFILTER]
    if next_file is not None and pathlib.Path(next_file).exists():
        # the upper box sits one line higher, so the pair reads top-down
        parts.append(_drawtext(title_file, 23, "0.92", "h-th-58"))
        parts.append(_drawtext(next_file, 17, "0.60", "h-th-26"))
    else:
        parts.append(_drawtext(title_file, 23, "0.92", "h-th-28"))
    return ",".join(parts)

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


# Ten seconds is enough because the defect is spread evenly through a file, one
# packet roughly every six seconds, rather than clustered anywhere. Measured
# 2026-09-05 over four library files: the ten second verdict agreed with the full
# segmentation on all four, and the cost is about a second per item.
REMUX_PROBE_SECONDS = 10


def remux_is_safe(path):
    """Whether copying this file's video yields chunks the pusher can send.

    matches_target says the picture is already the right shape, which is what
    makes the cheap path legal. It does not say the copy will come out playable,
    and on 2026-09-05 that gap took the channel down for hours.

    Copying the video out of some library files leaves a few dozen video packets
    per chunk carrying no PTS at all. The transport stream holds them without
    complaint, so nothing upstream notices, and prep reports a clean job. The flv
    muxer at the far end refuses the first one with "Invalid argument" and the
    pusher exits. systemd restarts it, the feeder is still holding that same
    chunk, and it exits again: 1298 consecutive restarts, the whole time black.

    Nothing on the reading side helps. Measured against a reproduced bad chunk,
    +genpts, +igndts and +discardcorrupt all still exit 1, and dropping
    -reset_timestamps or adding -copyts changes nothing on the writing side
    either. Re-encoding is the only thing that produced a clean chunk, so the
    only useful question is which files need it, and that is what this answers.

    The source itself probes clean, so this has to remux to find out rather than
    inspect the original.
    """
    probe = pathlib.Path(tempfile.gettempdir()) / f"remuxprobe_{os.getpid()}.ts"
    try:
        done = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-t", str(REMUX_PROBE_SECONDS), "-i", str(path),
             # audio is transcoded on both paths, so it cannot be what differs;
             # dropping it keeps the probe to about a second
             "-c:v", "copy", "-an", "-f", "mpegts", "-y", str(probe)],
            capture_output=True, timeout=120)
        if done.returncode != 0:
            # None, not False: this is "could not measure", and the caller that
            # remembers verdicts must not write it down. Caching it made 22 good
            # files invisible on 2026-09-06, because the probes were losing to a
            # busy box and every loss was recorded as a permanent refusal.
            return None
        packets = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(probe)],
            capture_output=True, text=True, timeout=120).stdout
        # a probe that read nothing proves nothing, so it does not get to vote
        # yes, and it does not get to vote no either
        if not packets.strip():
            return None
        return "N/A" not in packets
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        probe.unlink(missing_ok=True)


# Items this run has already abandoned once. Abandoning is only ever worth it
# if something else can run instead, and if the same item comes straight back
# then nothing else could: abandoning it again just spins. Cleared whenever a
# job actually produces chunks, because that proves the queue moved on.
ABANDONED = set()

# The backlog an encode is not allowed to eat into. Two chunks: the feeder is
# playing one while it claims the next, so below this there is nothing left
# between the channel and the standby clip.
ABANDON_BELOW_SECONDS = 2 * common.CHUNK_SECONDS
# How often the backlog is looked at while an encode runs. Short enough to
# notice before the last chunk is gone, long enough to cost nothing.
ENCODE_POLL_SECONDS = 20


def wait_for_encode(encoder, budget, armed=True):
    """Wait for the encoder, watching what it is doing to the channel.

    Returns (stderr, starved). starved means it was still running while the
    backlog fell to ABANDON_BELOW_SECONDS, and the caller should abandon it.

    armed is what stops this eating itself. Abandoning is only useful if there
    is something cheaper to run instead; with nothing else to pick,
    cheapest_when_starving hands the same item straight back, this abandons it
    again, and the pair spin forever producing nothing. Measured 2026-09-06:
    item 144 abandoned every ninety seconds for eight minutes, backlog stuck at
    zero, the channel on the standby clip the whole time. A slow encode that
    finishes beats a fast decision that never does, so with no alternative the
    watchdog stands down and lets the job run.

    This exists because every estimate of encode cost has been wrong, four
    times, each one ending in the same grey screen: a flat threshold, then a
    factor of 1.5, then 4, then a cost model that did not know remux_is_safe had
    moved ten files onto the expensive path. Each fix made the guess better and
    the next wrong guess cost the channel another two hours.

    So this does not guess. The backlog is the one number that cannot be wrong
    about whether the encoder is losing the race: it is measured, not predicted,
    and it falls if and only if the channel is being consumed faster than it is
    being fed. An encode that is winning never trips this no matter what any
    estimate said about it, and one that is losing trips it whatever the
    estimate said too. That is the whole point.

    The item is not marked failed. It is fine, this was the wrong moment, and it
    goes back to pending for a time when the queue can pay for it.
    """
    deadline = time.time() + budget
    while True:
        try:
            return encoder.communicate(timeout=ENCODE_POLL_SECONDS)[1].decode(
                errors="replace"), False
        except subprocess.TimeoutExpired:
            if time.time() >= deadline:
                raise
            if armed and seconds_on_disk() <= ABANDON_BELOW_SECONDS:
                return "", True


REMUX_VERDICTS = common.STATE / "remux.json"


def remux_verdict(path, unmeasured=False):
    """Whether prepare() will copy this file rather than re-encode it.

    `unmeasured` is the answer when the probe could not be taken at all, and the
    two callers want opposite ones. Choosing what to prepare next can afford to
    say no and look again a pass later. Deciding whether to throw an item out of
    the queue cannot: a no there removes the video, and on 2026-09-08 a probe
    that could not run on a busy box took the only fresh video in the library out
    of the queue and left the channel looping the one it had already played.

    Exactly the pair prepare() tests, remembered: the right picture AND a copy
    that comes out playable. Both, or the answer is wrong in the direction that
    costs the channel hours. Asking only remux_is_safe let 18 files at 1280x718
    through, two pixels short: their copies are perfectly valid, matches_target
    still refuses them, and prep re-encodes them anyway. The filter that exists
    to keep encodes out of the queue was letting them in.

    The probe costs about a second, which is nothing when prep is choosing one
    item and far too much when the refill asks about every file in the library
    on a loop. Keyed on size and mtime, so a file replaced by the normaliser is
    asked again rather than answering for the file it used to be.
    """
    path = pathlib.Path(path)
    try:
        stat = path.stat()
        key = f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        return False
    try:
        cache = json.loads(REMUX_VERDICTS.read_text())
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return bool(cache[key])
    if not matches_target(path):
        # The picture is a property of the file and of nothing else, so this one
        # answer is safe to keep: it will say the same thing on an idle box and
        # on a thrashing one.
        verdict, remember = False, True
    else:
        answer = remux_is_safe(path)
        if answer is None:
            # no measurement, and nothing is written down: recording it is what
            # hid 22 conformant files behind a busy afternoon. What is returned
            # is the caller's to choose, because "I could not look" is not "no".
            return unmeasured
        # A trial remux that succeeded proves the file; one that failed proves
        # nothing, because it competes for the same two vCPU as the push and the
        # cut. Measured 2026-09-08: two files whose three probes all pass on an
        # idle box were each carrying a False taken during a busy minute, and
        # the channel looped one video with three usable ones in the library.
        # So a yes is remembered and a no is asked again, which costs a second.
        verdict, remember = bool(answer), bool(answer)
    if not remember:
        return verdict
    # only this file's entry survives, so the cache cannot grow with every
    # version of every file the normaliser ever wrote
    cache = {k: v for k, v in cache.items() if not k.startswith(path.name + ":")}
    cache[key] = verdict
    try:
        common.STATE.mkdir(parents=True, exist_ok=True)
        tmp = REMUX_VERDICTS.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        tmp.replace(REMUX_VERDICTS)
    except OSError:
        pass  # a cache that cannot be written is slow, not wrong
    return verdict


def matches_target(path):
    """Whether a file already holds a picture a chunk can carry as it is.

    Codec and size, and deliberately not the frame rate. That pin is what made
    every arriving video expensive: YouTube serves h264 1080p at 30 or at 60 and
    never at 50, so a file downloaded in exactly the right codec and exactly the
    right size still failed this test on its frame rate alone and was re-encoded
    in full, at 0.06x realtime, to change nothing a viewer can see.

    The frame rate is asked about again, but as a set rather than a value:
    common.fps_supported says whether Kick will pass a source through at that
    rate at all. Pinning it to one number was what made every arrival expensive,
    and dropping it entirely was wrong in the other direction, because 50 is a
    rate Kick re-encodes rather than relays. Both mistakes were made on
    2026-09-08 and the second one is why the channel looked like a bitmap for an
    hour. A junction between two supported rates was measured going through the
    real feeder and pusher commands with exit 0 and nothing on stderr, and
    random bytes in the same chain exit 1, so the check can fail.

    The size is a ceiling rather than an equality, and that is kil's call taken
    on 2026-09-08 with the risk stated. A video YouTube only has at 720p is now
    played at 720p instead of being re-encoded up to 1080, which is upscaling
    and buys nothing. The cost is a resolution change mid-stream, and unlike the
    frame rate that one does live in the sequence header the ingest reads. The
    flv muxer accepted it in the same measurement, but the ingest is the part no
    test here can reach without putting the channel on the line, so this is the
    one change in this file resting on a decision rather than on a witness.
    quality.py samples is_live and the viewer count every five minutes, which is
    where it will show if the ingest disagrees.

    The ceiling itself is not decoration: both downloaders ask for height<=1080,
    and pushing a 4K file over this link would spend the whole bitrate budget on
    a picture Kick re-encodes anyway.

    Only the video is judged here. audio_matches_target answers for the sound.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,width,height,r_frame_rate", "-of", "csv=p=0",
             str(path)],
            capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    fields = (out.strip().splitlines() or [""])[0].split(",")
    if len(fields) != 4 or fields[0] != "h264":
        return False
    try:
        width, height = int(fields[1]), int(fields[2])
        top, _, bottom = fields[3].partition("/")
        fps = float(top) / float(bottom or 1)
    except (ValueError, ZeroDivisionError):
        return False
    return (0 < width <= common.WIDTH and 0 < height <= common.HEIGHT
            and common.fps_supported(fps))


def audio_matches_target(path):
    """Whether this file's sound is already exactly what a chunk must carry.

    Codec, rate and channel count, all three. The chunks are concatenated into
    one stream and every junction where any of those changes is a junction the
    pusher has to survive, which is the same reason common.py pins the picture.
    The bitrate is deliberately not compared: it is the one thing that varies
    across the library, and carrying it through untouched is the point.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,sample_rate,channels", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip().splitlines()[:1] == ["aac,44100,2"]


def captioned():
    """Library files whose picture already carries their title.

    A remux cannot draw, so a file in the target shape keeps whatever was burned
    into it when it was last encoded and nothing else. Without this record the
    47 files normalised before the caption existed would stay conformant, stay
    remuxed, and never show a title again. Recording it by name rather than
    probing the picture is the only cheap way to ask the question.
    """
    try:
        return set(json.loads((common.STATE / "captioned.json").read_text()))
    except (OSError, ValueError):
        return set()


def mark_captioned(name):
    common.STATE.mkdir(parents=True, exist_ok=True)
    done = captioned()
    done.add(name)
    tmp = common.STATE / "captioned.tmp"
    tmp.write_text(json.dumps(sorted(done)))
    tmp.replace(common.STATE / "captioned.json")


def normalise_in_place(path, title_file=None):
    """Re-encode a library file into the exact shape a chunk must have, once.

    A library file is replayed forever. Encoding it on every pass costs more
    wall time than the video buys back on this box, so the queue can never get
    ahead and the channel falls back to the standby clip. Doing it once here
    means no one has to remember to run a tool after adding videos.

    The title is burned here rather than at chunk time, and that is the only
    place it can be free: once a file is in the target shape every later pass
    remuxes it, and a remux cannot draw. Burning it at chunk time instead would
    force a re-encode of the one class of file that currently keeps a picture on
    the wire.

    Only the name of the video is burned. Who asked for it, and what follows it,
    both change on every rotation, so baking either would make this file lie on
    every play after the first.

    Every failure returns None and the caller encodes the file the slow way.
    This is an optimisation, and an optimisation that can stop the pipeline is
    worse than no optimisation: the first version let a read-only library raise
    out of here and crash the service on a loop.

    The replacement is a rename, so a reader already holding the old file keeps
    reading it, and a failure leaves the original untouched.
    """
    target = path.with_suffix(".norm.mp4")
    print(f"normalisation ({path.name})", flush=True)
    encode = list(common.ENCODE)
    if title_file is not None and pathlib.Path(title_file).exists():
        # ponytail: no marker is written to say a file has been captioned. The
        # guard is that a normalised file already matches the target, so this
        # never runs twice on it. Change the target shape and the whole library
        # is re-normalised, which would stack a second caption on the first.
        encode[encode.index("-vf") + 1] = overlay_filter(title_file)
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(path)] + encode
            + ["-f", "mp4", str(target)],
            capture_output=True, text=True, timeout=common.ENCODE_TIMEOUT_CEILING)
        if out.returncode != 0 or not target.exists():
            target.unlink(missing_ok=True)
            print(f"  echec: {(out.stderr or '').strip().splitlines()[-1:]}", flush=True)
            return None
        final = path.with_suffix(".mp4")
        target.replace(final)
        if final != path:
            path.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass  # a read-only library cannot even be tidied up
        print(f"  echec: {exc}", flush=True)
        return None
    if title_file is not None:
        # recorded only on the path that actually drew, so a file normalised
        # without a caption is not mistaken for one that has it
        mark_captioned(final.name)
    print(f"  fait: {final.name}", flush=True)
    return final


def prepare(item, upcoming=None, watchdog=True):
    """Stream one URL through ffmpeg into numbered chunks. True on success.

    watchdog=False when nothing cheaper is waiting: abandoning then only
    hands the same item back and the pair spin producing nothing.
    """
    common.SEGMENTS.mkdir(parents=True, exist_ok=True)
    pattern = str(common.SEGMENTS / f"{item['id']:05d}_%05d.ts")

    title_file = common.STATE / f"title_{item['id']:05d}.txt"
    name = common.clean_text(item.get("title"), 70)
    caption = name
    if item.get("by_name"):
        caption = f"{caption}   -   requested by {common.clean_text(item['by_name'], 24)}"
    title_file.write_text(caption, encoding="utf-8")

    # the burned-in copy carries the name alone. The requester belongs to one
    # request, and this file is replayed forever: baked in, it would credit the
    # wrong person on every play after the first.
    name_file = common.STATE / f"name_{item['id']:05d}.txt"
    name_file.write_text(name, encoding="utf-8")

    # written even when nothing follows, so a stale file from the previous item
    # can never be picked up and drawn as this one's "next"
    next_file = common.STATE / f"next_{item['id']:05d}.txt"
    following = common.clean_text(
        (upcoming or {}).get("title") or (upcoming or {}).get("url"), 60)
    next_file.write_text(f"a suivre  {following}" if following else "",
                         encoding="utf-8")
    if not following:
        next_file.unlink(missing_ok=True)

    encode = list(common.ENCODE)
    encode[encode.index("-vf") + 1] = overlay_filter(title_file, next_file)
    if item.get("path"):
        source = pathlib.Path(item["path"])
        # A library file in the wrong shape is normalised once instead of being
        # re-encoded on every pass through the rotation, and one already in the
        # target shape still gets a single pass if its picture carries no title,
        # because a remux cannot add one later and this is the only moment the
        # caption is free.
        #
        # Both wait until the queue can outlast the encode rather than until it
        # passes a fixed 45 minutes. Once this function commits to normalising,
        # it stays there for the whole job and cheapest_when_starving cannot
        # help: the choice has already been made. Measured 2026-09-05, the flat
        # gate let a two hour encode start on 7200s of queue that drained at
        # 1.1x, which runs dry before the encode ends. A gate that does not know
        # what it is admitting is the same bug this file already fixed one level
        # up, left in place one level down.
        # The caption used to be reason enough on its own: a file already in the
        # right shape was re-encoded in full, once, purely to burn its title into
        # the picture. That is a whole video's worth of encoding for a line of
        # text, on every video that ever arrives, and it is the single largest
        # thing this pipeline spent its time on. Dropped 2026-09-08. The title is
        # still answered by !next, by the dashboard and by the chat, which is
        # where an accurate answer lived anyway: a burned caption cannot follow a
        # vote that lands later, and this file says so twenty lines further down.
        #
        # What is left below can only fire for a file prep cannot copy at all,
        # and drop_unremuxable takes those out of the queue before prepare() ever
        # sees them, so in practice nothing reaches it.
        affordable = seconds_on_disk() >= normalise_cost(item) + COST_MARGIN_SECONDS
        if (not matches_target(source)
                and not consumable(source)
                and affordable):
            fixed = normalise_in_place(source, name_file)
            if fixed is not None:
                item["path"] = str(fixed)
                source = fixed
        if matches_target(source) and remux_is_safe(source):
            # nothing to redraw, so the title overlay goes with it: a caption is
            # not worth a channel that cannot keep a picture on the wire
            encode = list(common.REMUX)
            if audio_matches_target(source):
                # A library file is replayed forever, and until 2026-09-08 every
                # pass transcoded its 160k aac down to 128k: 00360_00002.ts
                # probed at 131025 against 159507 in the file it was copied from,
                # and the next pass would have taken another cut off that. MPEG-TS
                # carries aac as it is, so this costs nothing and stops the loss
                # dead. Measured on the whole library the same day: 38 of 38 files
                # are aac LC 44100 stereo, so this is the path they all take.
                encode = ["-c:v", "copy", "-c:a", "copy"]

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
        enc_err, starved = wait_for_encode(
            encoder, budget,
            armed=watchdog and item["id"] not in ABANDONED)
        if starved:
            # Once per item, ever. The armed flag is computed from the queue and
            # was wrong for one of the two call sites on 2026-09-06: item 144
            # was abandoned every twenty-five seconds for six minutes, backlog
            # pinned at zero, the channel black throughout. This does not depend
            # on getting that calculation right anywhere.
            ABANDONED.add(item["id"])
            for process in (encoder, puller):
                if process is None:
                    continue
                process.kill()
                try:
                    process.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
            for chunk in common.SEGMENTS.glob(f"{item['id']:05d}_*.ts"):
                chunk.unlink(missing_ok=True)
            listing.unlink(missing_ok=True)
            # not an error: the file is fine, this was the wrong moment for it.
            # The caller puts it back to pending on this flag.
            item["defer"] = "abandonne, la file allait tomber a vide"
            print(f"abandon {item['id']}: tampon a {seconds_on_disk()}s, "
                  f"un item moins cher passe devant", flush=True)
            return False
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
    # something got made, so the standoff is over
    ABANDONED.clear()
    return True


MEDIA_SUFFIXES = common.MEDIA_SUFFIXES
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
            "title": common.pretty_title(candidate.stem),
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
            if item.get("path"):
                record_play(item["path"])
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
# Normalising produces nothing for the channel until it finishes, where an
# ordinary encode feeds chunks out as it goes. So it is only worth starting with
# enough backlog to cover it: a half hour video takes about forty minutes here.
# Below this the file is encoded the slow way again and normalised on a later
# pass, when the remuxes have built the reserve back up.
NORMALISE_ABOVE_SECONDS = 45 * 60


def consumable(path):
    """Whether playing this file is allowed to consume it.

    A file dropped in incoming/ is handed over for a single play and is removed
    once it has been encoded. A library file is queued where it lives and has to
    survive being played, or the first pass through the rotation deletes the
    library it is supposed to replay.
    """
    return pathlib.Path(path).parent == common.INCOMING


HISTORY = common.STATE / "history.json"
# How long a file is kept out of the draw after it has last been queued. The
# library is 21.3h of video, so one pass puts every file inside this window and
# the draw falls through to the oldest-first branch below. That is intended: the
# week is a preference, never a lock, because a library locked out of its own
# rotation is a grey screen.
REPLAY_GAP_SECONDS = 7 * 86400


def load_history():
    """What each library file has done, as {path: {"at": epoch, "plays": n}}.

    A bare number is read as the "at" of an entry with no plays yet, because
    that is the shape this file had for the few hours between the rotation
    memory landing and the play count landing on top of it.
    """
    try:
        data = json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for path, entry in data.items():
        if isinstance(entry, dict):
            at, plays = entry.get("at"), entry.get("plays")
        else:
            at, plays = entry, 0
        try:
            out[str(path)] = {"at": float(at), "plays": max(0, int(plays or 0))}
        except (TypeError, ValueError):
            continue  # an unreadable entry means "never played", not a crash
    return out


def played_count(history, path):
    return history.get(str(path), {}).get("plays", 0)


def last_queued(history, path):
    return history.get(str(path), {}).get("at", 0.0)


def save_history(history):
    """Write it atomically, the same way the remux verdicts are written."""
    try:
        common.STATE.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY.with_suffix(".tmp")
        tmp.write_text(json.dumps(history, indent=1))
        tmp.replace(HISTORY)
    except OSError:
        pass  # a history that cannot be written repeats itself, it does not stop


def library_size():
    """How many videos the rotation still has to draw from."""
    if LIBRARY is None or not LIBRARY.is_dir():
        return 0
    return sum(1 for p in LIBRARY.iterdir()
               if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES)


def runway_seconds(history=None, ignoring=None):
    """Video the channel can still play, in seconds: chunks already cut plus
    every library file that has plays left in it.

    This is the number that decides whether anything may be deleted. A file
    count cannot: three files might be twenty minutes or six hours, and the
    channel does not consume files, it consumes time.

    Durations are remembered in the history entry the first time they are
    probed, because this is asked on every finished video and ffprobe on a
    multi-gigabyte file is not free.

    `ignoring` is the file being considered for deletion, so the caller can ask
    what the runway would be once it is gone rather than what it is now.
    """
    total = seconds_on_disk()
    if LIBRARY is None or not LIBRARY.is_dir():
        return total
    history = load_history() if history is None else history
    for path in LIBRARY.iterdir():
        if not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if ignoring is not None and path == pathlib.Path(ignoring):
            continue
        entry = history.setdefault(str(path), {"at": 0.0, "plays": 0})
        if entry.get("plays", 0) >= common.MAX_PLAYS:
            continue  # spent: it is not runway, it is what is about to go
        if not entry.get("secs"):
            entry["secs"] = duration_of(path)
        total += (entry["secs"] or 0) * (common.MAX_PLAYS - entry.get("plays", 0))
    return total


def record_play(path):
    """Count one play of a library file, and retire it once it has had its two.

    Counted here, where an item has just been observed to finish, rather than
    where it was queued: a file that failed to encode was never on air and has
    no business being retired for it.

    Retiring means deleting, for the same reason the janitor deletes: moving a
    file frees nothing when it is all one filesystem, and the collector can
    fetch it again from the source. The floor is the whole safety of it. With
    nothing arriving to replace what goes, this would otherwise cut the rotation
    down one video at a time, and a channel with nothing left to play is a worse
    answer to "I keep seeing the same video" than the repeat was.
    """
    if LIBRARY is None:
        return
    path = pathlib.Path(path)
    try:
        if path.parent != LIBRARY or not path.is_file():
            return
    except OSError:
        return
    history = load_history()
    entry = history.setdefault(str(path), {"at": 0.0, "plays": 0})
    entry["plays"] += 1
    if entry["plays"] < common.MAX_PLAYS:
        save_history(history)
        return
    left = runway_seconds(history, ignoring=path)
    if left < common.MIN_RUNWAY_SECONDS:
        # Said on every pass rather than once. A rotation sitting on the floor is
        # the state where nothing is arriving to replace what plays, and that is
        # worth repeating until someone or something fixes the supply.
        print(f"garde {path.name}: sans lui il resterait {int(left / 60)} min "
              f"a diffuser, plancher {common.MIN_RUNWAY_SECONDS // 60} min",
              flush=True)
        save_history(history)
        return
    try:
        path.unlink()
    except OSError as exc:
        print(f"  retrait impossible ({path.name}): {exc}", flush=True)
        save_history(history)
        return
    history.pop(str(path), None)
    save_history(history)
    print(f"retire apres {common.MAX_PLAYS} passages: {path.name} "
          f"({int(left / 60)} min encore en reserve)", flush=True)


# A stream longer than the board's card can hold whole arrives in pieces, named
# by the video they came from and their place in it: <title>-<id>.p02of04.mp4.
# Everything downstream treats them as ordinary library files, which is the
# point; the only thing that has to know is the draw.
PART = re.compile(r"-([A-Za-z0-9_-]{11})\.p(\d+)of\d+\.[A-Za-z0-9]+$")


def video_of(path):
    """The video a file belongs to, and its place in it.

    A file that is not a part is its own video at position zero, so a library
    with no parts in it behaves exactly as it did before there were any.
    """
    found = PART.search(path.name)
    if found:
        return found.group(1), int(found.group(2))
    return str(path), 0


def shuffled_by_video(paths):
    """Shuffle whole videos, never the pieces inside one.

    random.shuffle over the files would scatter the parts of an eleven hour
    stream through the rotation and play them out of order. Grouping first
    means the draw picks videos and each one is emitted whole, in order.
    """
    groups = {}
    for path in paths:
        video, index = video_of(path)
        groups.setdefault(video, []).append((index, path))
    order = list(groups)
    random.shuffle(order)
    picked = []
    for video in order:
        picked.extend(path for _, path in sorted(groups[video], key=lambda x: x[0]))
    return picked


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

    # Only what can be remuxed. This is the invariant the channel lives by: the
    # feeder consumes at 1x forever, a remux produces at about 10x and a full
    # re-encode at 0.06x measured on this box, so one encoded file is a loss no
    # amount of scheduling recovers from. Queueing a file prep cannot copy is
    # queueing a grey screen, whatever the cost model then does about it.
    #
    # 2026-09-06: 38 of 62 library files could not be copied, so six items in
    # ten were an encode, and refill could not even fire because it waits for an
    # empty pending list and those items never cleared. Files that need work go
    # to normalise.py, and rejoin here once it has done it.
    media = [p for p in LIBRARY.iterdir()
             if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES]
    sources = sorted(p for p in media if remux_verdict(p))
    if not sources:
        print("aucun fichier remuxable en bibliotheque: normalise.py a du retard",
              flush=True)
        return 0

    now = time.time()
    # the janitor deletes library files, and a deleted file keeps no place in
    # the rotation: the same name arriving again is a new file, not a replay
    known = {str(p) for p in media}
    history = {path: entry for path, entry in load_history().items() if path in known}
    # The video on air right now is not a candidate for the pass that follows
    # it. Its entry is still "ready" and its chunks are still on the disk, and
    # without this it went back in the draw it is currently the answer to and
    # could be picked first, which is the same file twice in a row.
    on_air = {i["path"] for i in queue["items"]
              if i.get("path") and list(common.SEGMENTS.glob(f"{i['id']:05d}_*.ts"))}
    pool = [p for p in sources if str(p) not in on_air] or sources

    # A file that has had its plays should already be gone, retired by
    # record_play as it finished. One still here is one the floor refused to
    # delete, and it goes back in the draw only when there is nothing else.
    fresh_enough = [p for p in pool if played_count(history, p) < common.MAX_PLAYS]
    pool = fresh_enough or pool

    # Nothing recorded what had already played, so the rotation was a fresh
    # shuffle with no memory: the last video of one pass could be the first of
    # the next. A file that has been on air within the week stands aside while
    # anything else is available.
    picks = [p for p in pool
             if now - last_queued(history, p) > REPLAY_GAP_SECONDS] or list(pool)
    # Random every pass, never an order. The order is not what stops a repeat
    # anyway: refill queues the whole pool at once and only fires again once
    # that is spent, so a file cannot come back before every other one has
    # played, whichever order they play in.
    #
    # The draw is over videos, not over files. A stream too long to fit on the
    # board's card whole arrives as several files that are one video, and a
    # plain shuffle would interleave them with another's and air hour nine
    # before hour one.
    picks = shuffled_by_video(picks)

    # The played entries naming these same files go first, or the queue keeps a
    # dead copy of the whole library on every pass. An item still holding chunks
    # is exempt: dropping it left those chunks with no item, reap() deleted them
    # on its next pass, and the video was cut off mid-play and put back in the
    # draw. That was the repeat.
    targets = {str(p) for p in sources}
    queue["items"] = [
        i for i in queue["items"]
        if i.get("path") not in targets
        or list(common.SEGMENTS.glob(f"{i['id']:05d}_*.ts"))
    ]
    # Every item below is stamped with the same added_at, and playback_order
    # breaks that tie with a stable sort, so the order of this list is the order
    # the channel plays. Votes are untouched: they sort ahead of added_at and
    # still decide what jumps the line.
    for src in picks:
        history.setdefault(str(src), {"at": 0.0, "plays": 0})["at"] = now
        queue["seq"] += 1
        queue["items"].append({
            "id": queue["seq"],
            "url": src.name,
            "path": str(src),
            "status": "pending",
            "by": "file",
            "by_name": "",
            "title": common.pretty_title(src.stem),
            "votes": [],
            "added_at": time.time(),
        })
    save_history(history)
    print(f"bibliotheque remise en file: {len(picks)} fichier(s)", flush=True)
    return len(picks)


def drop_unremuxable(queue):
    """Take library files prep cannot copy back out of the queue.

    This is what unsticks the deadlock, and it is worth naming precisely.
    refill_from_library only fires when nothing is pending, so a queue holding
    31 library items prep could not copy never refilled: it worked through them
    one multi-hour encode at a time, on the standby clip for most of it, and no
    amount of choosing better within that list helped because every entry in it
    was expensive. Measured 2026-09-06.

    Dropping them is safe because a library file is queued where it lives and is
    never consumed by playing. normalise.py converts them offline and refill
    picks them up on the next pass, so this removes an entry, never a video.

    Items a person asked for are left alone. Someone waiting on a request they
    made is owed the wait, and there is at most a handful of those.
    """
    # Only a measured no gets an item thrown out. A probe that could not run
    # says nothing about the file, and treating that silence as a refusal is how
    # the only fresh video in the library was removed from the queue while the
    # channel looped the one it had already played.
    victims = [i for i in queue["items"]
               if i["status"] == "pending" and i.get("by") == "file"
               and i.get("path")
               and not remux_verdict(i["path"], unmeasured=True)]
    if not victims:
        return 0
    ids = {i["id"] for i in victims}
    queue["items"] = [i for i in queue["items"] if i["id"] not in ids]
    common.save_queue(queue)
    print(f"retires de la file: {len(victims)} fichier(s) non remuxables, "
          f"normalise.py s'en charge", flush=True)
    return len(victims)


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


# Wall time per second of video, for a re-encode. Not the 0.77x the encoder
# manages alone: prep shares two vCPUs with the pusher and the feeder, and the
# jobs actually observed on 2026-09-05 ran 5586s and 8324s for VODs well under
# an hour, which is nearer four. 1.5 was the nominal figure and it let a job
# through on a queue that could not outlast it, twice, each time ending in a
# grey screen. Erring high only delays an encode; erring low takes the channel
# off the air for the length of one.
#
# Raised to 11 on 2026-09-05 after four was measured wrong the same way 1.5 was.
# Item 140: 1674s of source, two chunks written, so 600s of output, in 6889s of
# wall time. Four predicted 6696s for the whole job and that much had already
# gone on a third of it. 6889/600 is 11.5, and this is one measurement, taken
# with the pusher and the feeder on the same two vCPUs, which is the only
# condition that matters because it is the one prep runs in.
ENCODE_COST_FACTOR = 11.0
# What is left over after an item is prepared, so the queue is never spent to
# the last second on a single bet.
COST_MARGIN_SECONDS = 5 * 60


def duration_of(path):
    """Length of a file in seconds, or 0 when it cannot be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60).stdout
        return float(out.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def normalise_cost(item):
    """Wall time a normalisation of this item will take.

    Always a full re-encode, even for a file already in the target shape, since
    the reason to normalise one of those is to burn a caption into it and that
    means redrawing every frame.

    Items queued from the library carry no duration, so the file is probed
    rather than written off. Treating unknown as infinite here would be quiet
    and total: every such file would fail the affordability test forever and
    silently never get its caption, which is the failure this whole path exists
    to produce rather than prevent.
    """
    duration = float(item.get("duration") or 0)
    if duration <= 0 and item.get("path"):
        duration = duration_of(item["path"])
    if duration <= 0:
        # nothing readable to go on: assume expensive rather than spend a queue
        # that cannot afford it
        return float("inf")
    return duration * ENCODE_COST_FACTOR


def prepare_cost(item):
    """Roughly how many seconds of wall time preparing this item will take.

    Near zero for a file already in the target shape AND safe to copy, since
    only the container and the audio are touched. Longer than the video itself
    for anything else.

    Both halves are load-bearing. remux_is_safe took ten files out of the cheap
    path without this being told, so a file the pusher cannot be sent still
    priced itself at zero, cheapest_when_starving read that as free and started
    it on an empty queue. Measured 2026-09-05: item 140, 1674s of source, 600s
    of output in 6889s of wall time, the channel on the standby clip throughout.
    A cost model that does not know what the encoder decided is worse than none,
    because it is trusted.
    """
    # the cached verdict, not the two probes: this is asked about every pending
    # item on every pass, and two ffprobes each turns choosing an item into
    # minutes of work on a box that has none to spare
    path = item.get("path")
    if path and remux_verdict(path):
        return 0.0
    return normalise_cost(item)


def cheapest_when_starving(pending):
    """The item to prepare now: playback order, unless the queue cannot pay.

    A file already in the target shape is remuxed in about a minute. One that is
    not is re-encoded at 0.77x realtime, so a 33 minute VOD takes over two hours
    during which the queue drains and the channel sits on the standby clip.
    Measured on 2026-09-05: 26 of 75 library files are 1280x718, two pixels
    short, and each costs that full re-encode.

    A fixed threshold cannot express this. Ten minutes of queue is plenty before
    a remux and nothing at all before a two hour encode, and the first version
    of this used ten minutes for both: at 1200s of queue it took the expensive
    head, drained, and the channel went dark for the rest of the encode. So the
    question asked here is not "is the queue low" but "can the queue outlast
    what this item costs", which is the only form that scales with the job.

    Order is bent only to keep a picture on the wire, and nothing is dropped:
    what is skipped stays queued, ahead of whatever it was already ahead of.
    """
    if not pending:
        return None
    buffered = seconds_on_disk()
    if buffered >= prepare_cost(pending[0]) + COST_MARGIN_SECONDS:
        return pending[0]
    for candidate in pending:
        # each probe is an ffprobe, so stop at the first that costs nothing
        if prepare_cost(candidate) == 0.0:
            return candidate
    return pending[0]


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
        drop_unremuxable(queue)
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

        item = cheapest_when_starving(pending)
        # Arm the abandon watchdog only if there is something cheaper to switch
        # to. Without this it abandons, gets handed the same item back, and
        # loops forever with the backlog at zero.
        alternative = any(prepare_cost(other) == 0.0
                          for other in pending if other is not item)
        # what the queue says follows, at encode time. A vote landing later can
        # still change it, so the overlay words it as a hint rather than a fact.
        upcoming = next((i for i in pending if i is not item), None)

        if item.get("path"):
            # handed over by the operator, not requested by a stranger: there is
            # no publisher to check and no downloader to ask
            item["status"] = "preparing"
            common.save_queue(queue)
            ok = prepare(item, upcoming, watchdog=alternative)
            if not ok and not item.get("defer"):
                # a deferred file is not a failed one, so it stays where it is
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
            deferred = bool(item.pop("defer", None))
            for entry in queue["items"]:
                if entry["id"] == item["id"]:
                    entry.update(item)
                    entry.pop("defer", None)
                    # abandoned to keep a picture on the wire, not failed: it
                    # goes back in line rather than out of the rotation
                    entry["status"] = ("ready" if ok
                                       else "pending" if deferred else "error")
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

        ok = prepare(item, upcoming, watchdog=alternative)

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
