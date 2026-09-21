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
import cut  # noqa: E402

OFFSET = chan.STATE / "offset"
# what is on the wire this second, written here because only this loop knows.
# The cutter runs an hour ahead and deletes its job when it finishes, so a title
# taken from the cutter announced the next hour an hour early and then froze.
ONAIR = chan.STATE / "onair.json"
# written by cut.py when the chat skips: the chunk in flight goes too, or the
# skip is invisible for as long as it has left to run
FLUSH = chan.STATE / "flush"
# kil, 2026-09-21: "lorsque ca vote pour une video, tu mets un short viral".
# The bot writes this when a vote sends the board shopping; one short goes out
# at the next junction and the file is left where it is for the next time.
SHORT = chan.STATE / "short"
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


def oldest_short():
    """The short whose turn it is, or None. Oldest first, so they take turns.

    Nothing is deleted: a short is a few megabytes and the rotation is the
    point. The one sent is touched, which puts it last in line, which is the
    whole of the bookkeeping.

    One above the session is skipped rather than sent. shorts.py builds them at
    the channel's ceiling so it should never happen, but a short is the one
    picture that reaches the wire without passing the cutter, and the net under
    a chunk does not cover it: reopen_if_above is armed only when there is a
    chunk to send, which is exactly not the case when the disk is dry.
    """
    session = chan.read_json(SESSION, {}).get("profile")
    for path in sorted(chan.SHORTS.glob("*.ts"), key=lambda p: p.stat().st_mtime):
        info = chan.probe(path)
        if info is None or info["seconds"] < 1:
            # a file ffmpeg sends in no time is one this loop would pick again
            # immediately, and again, as fast as the pipe accepts it
            chan.log(f"short ecarte, illisible ou vide: {path.name}")
            path.unlink(missing_ok=True)
            continue
        profile = (info["width"], info["height"], info["fps"])
        if session and exceeds(profile, tuple(session)):
            chan.log(f"short ecarte, plus grand que la session: {path.name} {profile}")
            continue
        return path
    return None


def next_short():
    """The short the chat has paid for, or None. The marker is spent either way."""
    if not SHORT.exists():
        return None
    SHORT.unlink(missing_ok=True)
    return oldest_short()


def note_on_air(chunk):
    """Say which hour of which file is going out, and since when."""
    if chunk == chan.FILLER:
        row = {"filler": True, "at": int(time.time())}
    elif chunk.parent == chan.SHORTS:
        # a short is not the channel's material: the hour on air has not moved
        # and nothing is spent in the ledger, so it is marked as a wait, which
        # is what the chat and the watchdog both need to read
        row = {"filler": True, "short": chunk.stem, "at": int(time.time())}
    else:
        found = chan.read_json(chan.STATE / "chunkmap.json", {}).get(chunk.name)
        if not found:
            return
        row = {"source": found[0], "number": found[1], "seconds": found[2]}
        # this is the moment a chunk is spent: it is going out. Written here
        # and not by the cutter, so minutes that a skip throws away before
        # they are sent stay unseen and come back in the draw.
        if len(found) >= 4:
            cut.record_units(found[0], [int(found[3])])
    keys = ("source", "number", "filler", "short")
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
        opened = open_session(pusher_pid())
        if opened:
            chan.log(f"session ouverte sur le clip d'attente: {profile_of(chan.FILLER)}")
            chunks = []
        short = None
        if chunks:
            source = chunks[0]
            short = next_short()
            if short is not None:
                # it goes out between two chunks, so nothing is cut short and
                # the hour on air resumes exactly where it was
                chan.log(f"short intercale: {short.name}")
                source = short
        elif opened:
            # the first picture of a session is always the clip: it is the one
            # file whose profile is certain, and Kick fixes its ladder on it
            source = chan.FILLER
        else:
            # kil, 2026-09-21: "si il n'y a pas de vod dispo, tu dois afficher
            # les shorts". Nothing to send is the case the clip was written for,
            # and twenty seconds of "vod loading" on a loop is the worst thing
            # the channel can show. A short says the same thing and is worth
            # watching. The clip stays underneath for the day there are none.
            short = oldest_short()
            source = short or chan.FILLER
            if short is not None:
                chan.log(f"rien a envoyer, short: {short.name}")
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
        if short is not None:
            source.touch()  # last in line for the next turn, and still there
        elif chunks:
            source.unlink(missing_ok=True)
        else:
            time.sleep(2)


if __name__ == "__main__":
    main()
