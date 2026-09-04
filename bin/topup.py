#!/usr/bin/env python3
"""Keep the chunk queue above the level prep needs to stop losing ground.

prepare() only normalises a library file when the queue already holds
NORMALISE_ABOVE_SECONDS of video. Below that line every file takes the slow
path, which encodes at 0.77x realtime, so it hands the feeder chunks more slowly
than the feeder eats them and the channel sits on the standby clip. The queue
cannot rise while that is true, so the one thing that would end it is gated on
it having already ended. Left alone the channel stays grey and every recovery is
somebody topping the queue up by hand.

This breaks the circle from outside. When the queue is low it remuxes a library
file that is already in the target shape straight into segments/, which costs a
container rewrite rather than an encode, and it stops the moment the queue is
over the line. Once prep can normalise again, every file it touches becomes
cheap forever, the queue stays up on its own, and this finds nothing to do.

So the success condition is that this script becomes a no-op. It is scaffolding
for a pipeline that cannot currently stand up, not a permanent part of it.

    python3 bin/topup.py           top up if needed, then exit
    python3 bin/topup.py --status  report and change nothing

Two things it must never do. It must not run twice at once, because two remuxes
racing on two vCPUs is exactly the CPU starvation that breaks the RTMP session,
so it holds a lock and exits quietly when another run has it. And it must not
outrank the pusher, so the work runs at a low priority.
"""
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import common
import prep

# Above prep's own NORMALISE_ABOVE_SECONDS, so reaching it opens the gate that
# lets prep normalise, with room for the feeder to drain while prep works.
TARGET_SECONDS = 4200
# Only act once the queue has fallen well below the target, so a queue that is
# merely draining normally is left alone.
TRIGGER_SECONDS = 3300
# A reserved high range: chunks sort after everything prep produces, so real
# queue content always plays first and this is only reached when nothing else
# is ready.
FIRST_ID = 90000
LAST_ID = 99999
# A remux of a four hour VOD runs in minutes even on a loaded box, so this is
# far above any honest run and only ever catches a stuck one.
REMUX_TIMEOUT_SECONDS = 30 * 60

LIBRARY = pathlib.Path(
    os.environ.get("VODLOOP_LIBRARY") or (pathlib.Path.home() / "videos"))
LOCK = common.STATE / "topup.lock"
LEDGER = common.STATE / "topup.json"


def log(message):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} topup: {message}", flush=True)


def ledger():
    try:
        return json.loads(LEDGER.read_text())
    except (OSError, ValueError):
        return {"seeded": {}, "next_id": FIRST_ID}


def save_ledger(data):
    common.STATE.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(LEDGER)


def candidates(seeded):
    """Library files already in the target shape, least recently used first.

    Ordering by last use is what stops the channel replaying the same three
    files: without it every run picks the same alphabetical head.
    """
    if not LIBRARY.is_dir():
        return []
    ready = [p for p in sorted(LIBRARY.iterdir())
             if p.is_file() and p.suffix.lower() in (".mp4", ".mkv")
             and prep.matches_target(p)]
    return sorted(ready, key=lambda p: seeded.get(p.name, 0))


def seed(path, chunk_id):
    """Remux one file into the chunk directory. True on success.

    -c:v copy is the whole point: the picture is already in the target shape, so
    only the container and the audio codec change, which MPEG-TS requires.

    The timeout is the difference between this script being autonomous and being
    silently dead. flock releases when a process dies, so a crash costs one run,
    but a hung ffmpeg holds the lock for as long as it hangs and every later run
    exits reporting that someone else is working. A remux runs far faster than
    realtime, so a ceiling well above any honest run still catches a stuck one.
    """
    pattern = str(common.SEGMENTS / f"{chunk_id:05d}_%05d.ts")
    common.SEGMENTS.mkdir(parents=True, exist_ok=True)

    def discard_partial():
        # a partial run leaves chunks the feeder would play as a broken tail
        for leftover in common.SEGMENTS.glob(f"{chunk_id:05d}_*.ts"):
            leftover.unlink(missing_ok=True)

    try:
        out = subprocess.run(
            ["nice", "-n", "15", "ffmpeg", "-v", "error", "-i", str(path),
             "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             "-ac", "2", "-f", "segment", "-segment_time",
             str(common.CHUNK_SECONDS), "-segment_format", "mpegts", pattern],
            capture_output=True, text=True, timeout=REMUX_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log(f"timeout {path.name} apres {REMUX_TIMEOUT_SECONDS}s, abandon")
        discard_partial()
        return False
    if out.returncode != 0:
        log(f"echec {path.name}: {(out.stderr or '').strip().splitlines()[-1:]}")
        discard_partial()
        return False
    return True


def main(argv):
    if "--status" in argv:
        data = ledger()
        print(f"buffer={int(prep.seconds_on_disk())}s trigger={TRIGGER_SECONDS}s "
              f"target={TARGET_SECONDS}s gate={prep.NORMALISE_ABOVE_SECONDS}s")
        print(f"conformant in library: {len(candidates(data['seeded']))}")
        print(f"seeded so far: {len(data['seeded'])}")
        return 0

    # imported here rather than at the top: this is a POSIX-only module, and a
    # top-level import would stop the whole file being importable, and so
    # testable, anywhere else
    import fcntl

    common.STATE.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Silent, and before any measurement. A held lock is the normal
            # state while a run works, not an event worth a log line every few
            # minutes. The first version measured the queue before reaching
            # here, and seconds_on_disk() runs ffprobe once per chunk: every
            # cron tick then paid that cost even though it was about to do
            # nothing. Four copies overlapping took the load average to ten on
            # two vCPUs, which is the CPU starvation that drops the RTMP
            # session. Nothing expensive belongs outside this lock.
            return 0

        buffered = int(prep.seconds_on_disk())
        if buffered >= TRIGGER_SECONDS:
            return 0

        data = ledger()
        pool = candidates(data["seeded"])
        if not pool:
            log(f"nothing conformant in {LIBRARY}, cannot top up")
            return 1

        added = 0
        for path in pool:
            buffered = int(prep.seconds_on_disk())
            if buffered >= TARGET_SECONDS:
                break
            chunk_id = data["next_id"]
            data["next_id"] = FIRST_ID if chunk_id >= LAST_ID else chunk_id + 1
            if seed(path, chunk_id):
                data["seeded"][path.name] = int(time.time())
                added += 1
                log(f"seeded {path.name} (buffer was {buffered}s)")
            save_ledger(data)

        final = int(prep.seconds_on_disk())
        if added:
            log(f"{added} file(s) added, buffer {final}s, "
                f"prep gate {'open' if final >= prep.NORMALISE_ABOVE_SECONDS else 'still shut'}")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
