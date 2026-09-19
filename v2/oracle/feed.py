#!/usr/bin/env python3
"""Send chunks into the pusher's FIFO, oldest first, forever. A service.

Kept from v1 because each line was paid for:
  - every chunk is remuxed with a cumulative -output_ts_offset; a raw cat
    restarts timestamps and the muxer ends on "DTS out of order".
  - the offset is written before a chunk is sent, so a crash leaves a gap the
    muxer tolerates instead of a range sent twice, which it does not.
  - +initial_discontinuity silences the continuity counter jump at each junction.
  - Kick fixes its ladder from the first picture of an RTMP session: a session
    opened at 720p30 serves a 1080p or 60 fps chunk below itself. So every
    session is opened on the standby clip, which is built at the channel's
    ceiling, and nothing that follows can exceed it. A chunk that does anyway
    still ends the session (SIGTERM to the pusher's ffmpeg, systemd brings both
    back in about 6 s), at most once every ten minutes.
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
# what is on the wire this second, written here because only this loop knows.
# The cutter runs an hour ahead and deletes its job when it finishes, so a title
# taken from the cutter announced the next hour an hour early and then froze.
ONAIR = chan.STATE / "onair.json"
# written by cut.py when the chat skips: the chunk in flight goes too, or the
# skip is invisible for as long as it has left to run
FLUSH = chan.STATE / "flush"
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


def open_session(pid):
    """Fix a new session's ladder on the standby clip, and say it was done.

    Kick reads its ladder from the first picture of an RTMP session and ends
    the live on any later picture above it. The clip carries the channel's
    ceiling, so opening on it means nothing that follows can ever exceed the
    session: no reopen, no cut VOD, no dropped viewers, whatever order the
    material arrives in. Before this the first chunk set the ladder, and a
    30 fps one followed by a 60 fps one cost a cut per rotation.
    """
    profile = profile_of(chan.FILLER)
    if pid <= 0 or profile is None:
        return False
    session = chan.read_json(SESSION, {})
    if session.get("pid") == pid and session.get("profile"):
        return False
    chan.write_json(SESSION, {"pid": pid, "profile": list(profile),
                              "reopened_at": session.get("reopened_at", 0)})
    return True


def reopen_if_above(chunk, may_reopen, now=time.time):
    """The net under open_session: a chunk above the clip still has to cut.

    It cannot fire on material the channel accepts, since the clip is built at
    MAXH and 60 fps. It stays for the day that ceiling is raised under a live
    pusher, where the alternative is a session serving its own upscale.
    """
    pid = pusher_pid()
    profile = profile_of(chunk)
    if pid <= 0 or profile is None:
        return False
    session = chan.read_json(SESSION, {})
    last = session.get("reopened_at", 0)
    if session.get("pid") != pid or not session.get("profile"):
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


def note_on_air(chunk):
    """Say which hour of which file is going out, and since when."""
    if chunk == chan.FILLER:
        row = {"filler": True, "at": int(time.time())}
    else:
        found = chan.read_json(chan.STATE / "chunkmap.json", {}).get(chunk.name)
        if not found:
            return
        row = {"source": found[0], "number": found[1], "seconds": found[2]}
    keys = ("source", "number", "filler")
    was = chan.read_json(ONAIR, {})
    if [was.get(k) for k in keys] == [row.get(k) for k in keys]:
        return  # same hour still going out, so the clock on it does not restart
    row["at"] = int(time.time())
    chan.write_json(ONAIR, row)


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
        if open_session(pusher_pid()):
            chan.log(f"session ouverte sur le clip d'attente: {profile_of(chan.FILLER)}")
            chunks = []
        source = chunks[0] if chunks else chan.FILLER
        if reopen_if_above(source, may_reopen=bool(chunks)):
            chan.log(f"session rouverte pour {source.name}: {profile_of(source)}")
            time.sleep(30)
            continue
        note_on_air(source)
        length = seconds(source)
        offset += length
        tmp = OFFSET.with_suffix(".tmp")
        tmp.write_text(f"{offset:.3f}")
        tmp.replace(OFFSET)
        FLUSH.unlink(missing_ok=True)
        with open(chan.FIFO, "wb") as pipe:
            sending = subprocess.Popen(
                ["ffmpeg", "-v", "error", "-i", str(source), "-c", "copy",
                 "-output_ts_offset", f"{offset - length:.3f}",
                 "-mpegts_flags", "+initial_discontinuity", "-f", "mpegts", "-"],
                stdout=pipe)
            while sending.poll() is None:
                if FLUSH.exists():
                    FLUSH.unlink(missing_ok=True)
                    sending.terminate()
                    chan.log(f"chunk en vol interrompu pour un saut: {source.name}")
                    break
                time.sleep(0.5)
            sending.wait()
        if chunks:
            source.unlink(missing_ok=True)
        else:
            time.sleep(2)


if __name__ == "__main__":
    main()
