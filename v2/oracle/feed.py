#!/usr/bin/env python3
"""Send chunks into the pusher's FIFO, oldest first, forever. A service.

Kept from v1 because each line was paid for:
  - every chunk is remuxed with a cumulative -output_ts_offset; a raw cat
    restarts timestamps and the muxer ends on "DTS out of order".
  - the offset is written before a chunk is sent, so a crash leaves a gap the
    muxer tolerates instead of a range sent twice, which it does not.
  - +initial_discontinuity silences the continuity counter jump at each junction.
  - Kick fixes its ladder from the first picture of an RTMP session: a session
    opened at 720p30 serves a 1080p or 60 fps chunk below itself. So a chunk
    that exceeds the session ends it (SIGTERM to the pusher's ffmpeg, systemd
    brings both back in about 6 s), at most once every ten minutes.
  - with nothing to send, the standby clip, which never opens a new session.
This process may restart freely. The pusher may not.
"""
import os
import pathlib
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402

OFFSET = chan.STATE / "offset"
SESSION = chan.STATE / "session.json"
REOPEN_GAP_SECONDS = 10 * 60


def seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def profile_of(path):
    info = chan.probe(path)
    return None if info is None else (info["width"], info["height"], info["fps"])


def exceeds(profile, opened):
    return profile[0] * profile[1] > opened[0] * opened[1] or profile[2] > opened[2] + 1


def pusher_pid():
    try:
        return int(chan.systemctl("show", "-p", "MainPID", "--value", chan.unit("push")))
    except ValueError:
        return 0


def reopen_if_above(chunk, may_reopen, now=time.time):
    pid = pusher_pid()
    profile = profile_of(chunk)
    if pid <= 0 or profile is None:
        return False
    session = chan.read_json(SESSION, {})
    last = session.get("reopened_at", 0)
    if session.get("pid") != pid or not session.get("profile"):
        chan.write_json(SESSION, {"pid": pid, "profile": list(profile), "reopened_at": last})
        return False
    if not may_reopen or not exceeds(profile, tuple(session["profile"])):
        return False
    if now() - last < REOPEN_GAP_SECONDS:
        return False
    chan.write_json(SESSION, {"pid": 0, "profile": list(profile), "reopened_at": now()})
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        chan.write_json(SESSION, session)
        return False
    return True


def main():
    try:
        offset = float(OFFSET.read_text().strip())
    except (OSError, ValueError):
        offset = 0.0
    while not chan.FILLER.exists():
        chan.log("pas de clip d'attente, install.sh le fabrique")
        time.sleep(30)
    while True:
        chunks = sorted(chan.CHUNKS.glob("*.ts"))
        source = chunks[0] if chunks else chan.FILLER
        if reopen_if_above(source, may_reopen=bool(chunks)):
            chan.log(f"session rouverte pour {source.name}: {profile_of(source)}")
            time.sleep(30)
            continue
        length = seconds(source)
        offset += length
        tmp = OFFSET.with_suffix(".tmp")
        tmp.write_text(f"{offset:.3f}")
        tmp.replace(OFFSET)
        with open(chan.FIFO, "wb") as pipe:
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-c", "copy",
                            "-output_ts_offset", f"{offset - length:.3f}",
                            "-mpegts_flags", "+initial_discontinuity", "-f", "mpegts", "-"],
                           stdout=pipe)
        if chunks:
            source.unlink(missing_ok=True)
        else:
            time.sleep(2)


if __name__ == "__main__":
    main()
