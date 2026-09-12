#!/usr/bin/env python3
"""Feed prepared chunks into the FIFO the pusher reads, in playback order.

Two things here are load-bearing and were established by measurement:

  - chunks are remuxed with a cumulative -output_ts_offset rather than cat'd.
    A raw cat makes the next chunk restart its timestamps at zero, which the
    muxer reports as "DTS out of order" and which does not survive a long run.
  - that cumulative offset is persisted, so restarting this service resumes
    where it left off instead of sending timestamps backwards.

A third thing here rests on a decision rather than on a measurement, and is
marked as such: a session the pusher has just opened is sent the standby clip
first, because Kick names the channel after the first picture it carries and
keeps that name for the whole live. See needs_greeting.

This process may be restarted freely. The pusher and its placeholder writer must
not be, which is why they live in a separate unit.
"""
import json
import subprocess
import sys
import time

import common

IDLE_POLL_SECONDS = 2


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


def ensure_filler():
    """A short standby clip, used when the queue runs dry so the feeder always
    has something to send. Identical encode settings to every other chunk."""
    filler = common.ROOT / "filler.ts"
    if filler.exists():
        return filler
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-t", "20", "-i", f"color=c=0x101014:s={common.WIDTH}x{common.HEIGHT}",
         "-f", "lavfi", "-t", "20", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        + common.ENCODE + ["-f", "mpegts", "-y", str(filler)],
        check=True,
    )
    return filler


SESSION = "push_session"
GREETED = "greeted_session"


def _read(name):
    try:
        return (common.STATE / name).read_text().strip() or None
    except OSError:
        return None


def push_session():
    """Which RTMP session the pusher is on, or None when it does not say.

    pusher.sh writes one line per exec and one exec is one connection to Kick.
    An old pusher that predates that line, or a state directory it could not
    write, both read as None, which is the answer that changes nothing.
    """
    return _read(SESSION)


def mark_greeted(session):
    try:
        common.STATE.mkdir(parents=True, exist_ok=True)
        tmp = common.STATE / (GREETED + ".tmp")
        tmp.write_text(session)
        tmp.replace(common.STATE / GREETED)
    except OSError:
        pass  # a greeting that cannot be recorded is repeated, not wrong


def needs_greeting(session, greeted):
    """Whether this session still has to be opened with the standby clip.

    Kick reads the resolution out of the sequence header when the session opens
    and advertises it for the whole live, so the first picture decides what the
    channel is called until the pusher dies. This library is mixed and most of
    it is 720p, so left to chance the channel is usually named after a 720p VOD
    and then announces every 1080p video in the rotation as something smaller
    than it is. filler.ts is common.WIDTH x common.HEIGHT at common.FPS by
    construction, so sending it first pins the label at the best shape the
    library holds. It costs twenty seconds of standby clip, paid at the one
    moment there are no viewers to spend it on: the session it opens is a
    session the old one just dropped.

    A session with no predecessor on record is not greeted. The first run after
    this shipped finds a pusher whose header was read hours ago, and greeting it
    would be twenty seconds of grey for a label already decided.

    What makes this land rather than race is the unit file: vodloop-feed is
    BindsTo= vodloop-push, so a pusher restart stops this process too and the
    pusher's Wants= starts it again. It therefore meets a new session at the top
    of its loop and not halfway through a chunk. Should it ever be mid-chunk
    anyway, that chunk reaches the new session first and the label is whatever it
    was going to be: the same as before this existed, never worse.
    """
    if session is None or greeted is None:
        return False
    return session != greeted


def next_source(greet, segments, filler):
    """What goes out next, and which chunk sending it consumes.

    The second value is what playing eats: the head of the playback order, or
    None when the standby clip is going out instead, whether because the queue
    is dry or because a new session is being opened on it. Keeping the two apart
    is not decoration. The old loop deleted "the source" whenever segments
    existed, so a greeting with a full queue behind it would have deleted
    filler.ts, which is the one file the next session needs to still be there.
    """
    playing = None if greet else (segments[0] if segments else None)
    return playing or filler, playing


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
    greeted = _read(GREETED)

    while True:
        session = push_session()
        greet = needs_greeting(session, greeted)
        if session is not None and session != greeted:
            # Claimed before it is sent, for the same reason the timeline range
            # below is: a crash in between costs one greeting, which is the
            # behaviour this had before it existed, while claiming it afterwards
            # would put the channel on the standby clip in a loop.
            mark_greeted(session)
            greeted = session
        if greet:
            print(f"nouvelle session rtmp {session}: clip d'attente en tete "
                  f"pour fixer l'etiquette a {common.WIDTH}x{common.HEIGHT}"
                  f"@{common.FPS}", flush=True)

        source, playing = next_source(greet, common.ready_segments(), filler)
        length = duration_of(source)

        # Claim the timeline range BEFORE sending it. Crashing mid-chunk then
        # leaves a gap, which the muxer tolerates, instead of replaying a range
        # already sent, which sends DTS backwards and is what actually breaks.
        offset += length
        common.write_offset(offset)
        granted = skip_stamp()
        cut = feed(source, offset - length, stop_when=lambda: skip_stamp() > granted)

        if playing is not None:
            playing.unlink(missing_ok=True)  # played chunks are purged immediately
            if cut:
                print(f"saut: {drop_item_of(playing)} chunk(s) restant(s) ecarte(s)",
                      flush=True)
        elif not greet:
            time.sleep(IDLE_POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
