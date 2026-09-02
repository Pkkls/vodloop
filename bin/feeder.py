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


def feed(path, offset):
    """Remux one chunk into the FIFO at the given timeline offset."""
    with open(common.FIFO, "wb") as pipe:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-c", "copy",
             "-output_ts_offset", f"{offset:.3f}", "-f", "mpegts", "-"],
            stdout=pipe, check=False,
        )


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
        feed(source, offset - length)

        if segments:
            source.unlink(missing_ok=True)  # played chunks are purged immediately
        else:
            time.sleep(IDLE_POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
