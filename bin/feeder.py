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

    while True:
        segments = common.ready_segments()
        source = segments[0] if segments else filler
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
