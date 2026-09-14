#!/usr/bin/env python3
"""Feed prepared chunks into the FIFO the pusher reads, in playback order.

Two things here are load-bearing and were established by measurement:

  - chunks are remuxed with a cumulative -output_ts_offset rather than cat'd.
    A raw cat makes the next chunk restart its timestamps at zero, which the
    muxer reports as "DTS out of order" and which does not survive a long run.
  - that cumulative offset is persisted, so restarting this service resumes
    where it left off instead of sending timestamps backwards.

This process may be restarted freely. The pusher and its placeholder writer must
not be, which is why they live in a separate unit.
"""
import json
import os
import signal
import subprocess
import sys
import time

import common

IDLE_POLL_SECONDS = 2

PUSH_UNIT = common.unit("push")
SESSION = common.STATE / "session.json"
# A reopening costs the viewers about six seconds. A fault that asked for one on
# every chunk would take the channel down in a loop, so there is one per window
# whatever the profiles say.
REOPEN_GAP_SECONDS = 10 * 60


def duration_of(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


# What the standby clip says. A flat dark field was indistinguishable from a dead
# stream, for viewers and for whoever was diagnosing it, and the only way to tell
# them apart was to measure the luma. Words remove that whole class of question.
FILLER_TEXT = os.environ.get("VODLOOP_FILLER_TEXT", "vod loading...")
FILLER_FONT = os.environ.get(
    "VODLOOP_FILLER_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def filler_matches(path):
    """Whether an existing standby clip still has the shape the channel sends.

    The one found on 2026-09-14 was 1280x720 at 30 fps against a target of
    1920x1080: left over from an older profile, and nothing regenerated it
    because the old check only asked whether the file existed. A clip of the
    wrong shape is a resolution change on the wire at the worst moment.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        w, h = (int(x) for x in out.split(",")[:2])
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return w == common.WIDTH and h == common.HEIGHT


def ensure_filler():
    """A short standby clip, used when the queue runs dry so the feeder always
    has something to send. Identical encode settings to every other chunk."""
    filler = common.ROOT / "filler.ts"
    if filler.exists() and filler_matches(filler):
        return filler
    # drawn on the lavfi source rather than through -vf, because ENCODE already
    # carries its own filter chain and two of them cannot both be passed
    fond = (f"color=c=0x101014:s={common.WIDTH}x{common.HEIGHT}"
            f",drawtext=fontfile={FILLER_FONT}:text='{FILLER_TEXT}'"
            f":fontcolor=white@0.82:fontsize=h/16"
            f":x=(w-text_w)/2:y=(h-text_h)/2")
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-t", "20", "-i", fond,
         "-f", "lavfi", "-t", "20", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        + common.ENCODE + ["-f", "mpegts", "-y", str(filler)],
        capture_output=True, text=True)
    if done.returncode != 0 or not filler.exists():
        # a missing font must never cost the channel its standby clip: fall back
        # to the plain field rather than leaving the feeder with nothing to send
        print(f"filler: texte impossible ({(done.stderr or '').strip()[-120:]}), "
              "repli sur un fond uni", flush=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-t", "20", "-i",
             f"color=c=0x101014:s={common.WIDTH}x{common.HEIGHT}",
             "-f", "lavfi", "-t", "20", "-i",
             "anullsrc=channel_layout=stereo:sample_rate=44100"]
            + common.ENCODE + ["-f", "mpegts", "-y", str(filler)],
            check=True,
        )
    return filler


def skip_stamp():
    """When the chat last granted a skip. Written by the chat, read here.

    Nothing consumed this before, so a granted skip answered "skipping" and the
    video kept playing to the end.
    """
    try:
        state = json.loads((common.STATE / "chat.json").read_text())
        return float(state.get("last_skip", 0.0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def drop_item_of(chunk):
    """Remove every remaining chunk of the video this one belongs to.

    A skip that only ends the current chunk lands on the next chunk of the same
    video, which is not what anyone asked for.
    """
    item = chunk.name.split("_")[0]
    dropped = 0
    for other in common.ready_segments():
        if other.name.split("_")[0] == item:
            other.unlink(missing_ok=True)
            dropped += 1
    return dropped


def profile_of(path):
    """(width, height, fps) of a chunk's picture, or None when it cannot be read."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        width, height, rate = (out.stdout.strip().splitlines() or [""])[0].split(",")[:3]
        top, _, bottom = rate.partition("/")
        return int(width), int(height), round(float(top) / float(bottom or 1), 2)
    except (ValueError, ZeroDivisionError):
        return None


def exceeds(profile, opened):
    """Whether a session opened at `opened` would serve `profile` below itself.

    Kick fixes its ladder from the first picture of an RTMP session and never
    revises it. Measured 2026-09-12: a session opened at 1080p60 served later
    720p and 360p files under a 1920x1080 label, which costs a label, not a
    picture; a session opened at 720p60 has no passthrough rung at all and tops
    out at its own 720p60 encode, so a 1080p file played in it reaches every
    viewer at 720p. Going down never needs a new session. Going up does.
    """
    return (profile[0] * profile[1] > opened[0] * opened[1]
            or profile[2] > opened[2] + 1)


def pusher_pid():
    """The pusher's ffmpeg: pusher.sh execs it, so it is the unit's main process."""
    out = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", PUSH_UNIT],
        capture_output=True, text=True,
    )
    try:
        return int(out.stdout.strip())
    except ValueError:
        return 0


def read_session():
    try:
        data = json.loads(SESSION.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_session(data):
    tmp = SESSION.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(SESSION)


def reopen_if_below(chunk, may_reopen=True, now=time.time):
    """End the RTMP session when `chunk` would be served below itself. True if so.

    The first picture a pusher carries is what its session opened at, recorded
    against the pusher's pid so that a pusher systemd restarted, which Kick also
    does on its own every 48 hours, starts a fresh record. Ending the session is
    a SIGTERM to that ffmpeg: systemd restarts the pusher five seconds later and
    brings this unit back with it, as it did on 2026-09-10 and 2026-09-12 at
    03:21, and the chunk that asked is still first in line because nothing sent
    or deleted it.
    """
    pid = pusher_pid()
    profile = profile_of(chunk)
    if pid <= 0 or profile is None:
        return False
    session = read_session()
    last = session.get("reopened_at", 0)
    if session.get("pid") != pid or not session.get("profile"):
        write_session({"pid": pid, "profile": list(profile), "reopened_at": last})
        return False
    if not may_reopen or not exceeds(profile, tuple(session["profile"])):
        return False
    if now() - last < REOPEN_GAP_SECONDS:
        return False
    # Written before the signal: this process dies with the pusher, and the one
    # systemd starts must not ask again for the same chunk.
    write_session({"pid": 0, "profile": list(profile), "reopened_at": now()})
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        # nothing was ended, so the session is still the one recorded
        write_session(session)
        return False
    return True


POLL_SECONDS = 0.5


def feed(path, offset, stop_when=None):
    """Remux one chunk into the FIFO at the given timeline offset.

    stop_when is polled while it plays. A skip has to cut the chunk in flight or
    it waits up to CHUNK_SECONDS to be noticed, which no viewer would call a
    skip. Cutting short leaves a gap in the claimed timeline, which the muxer
    tolerates; what it does not tolerate is the same range sent twice.
    """
    with open(common.FIFO, "wb") as pipe:
        process = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", str(path), "-c", "copy",
             "-output_ts_offset", f"{offset:.3f}",
             # Each chunk is a self-contained mpegts stream, and concatenating
             # them in the FIFO makes the TS continuity counters jump at every
             # junction, which the pusher reports as "Packet corrupt". Marking
             # each chunk as an expected discontinuity silences that at the
             # source. Measured: 2 corrupt packets per junction -> 0, stream
             # intact. Masking it on the read side with +discardcorrupt does
             # not remove it and would drop packets.
             "-mpegts_flags", "+initial_discontinuity", "-f", "mpegts", "-"],
            stdout=pipe,
        )
        while process.poll() is None:
            if stop_when is not None and stop_when():
                process.kill()
                process.wait()
                return True
            time.sleep(POLL_SECONDS)
    return False


def main():
    filler = ensure_filler()
    offset = common.read_offset()

    while True:
        segments = common.ready_segments()
        source = segments[0] if segments else filler

        # The filler is recorded when it opens a session, and never asks for a
        # new one: standby is not worth a reconnect.
        if reopen_if_below(source, may_reopen=bool(segments)):
            print(f"session rouverte pour {source.name}: {profile_of(source)}", flush=True)
            # systemd stops this unit with the pusher; if it has not within
            # the wait, the next pass finds a new pusher and simply feeds.
            time.sleep(30)
            continue

        length = duration_of(source)

        # Claim the timeline range BEFORE sending it. Crashing mid-chunk then
        # leaves a gap, which the muxer tolerates, instead of replaying a range
        # already sent, which sends DTS backwards and is what actually breaks.
        offset += length
        common.write_offset(offset)
        granted = skip_stamp()
        cut = feed(source, offset - length, stop_when=lambda: skip_stamp() > granted)

        if segments:
            source.unlink(missing_ok=True)  # played chunks are purged immediately
            if cut:
                print(f"saut: {drop_item_of(source)} chunk(s) restant(s) ecarte(s)",
                      flush=True)
        else:
            time.sleep(IDLE_POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
