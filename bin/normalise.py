#!/usr/bin/env python3
"""Convert library files prep cannot copy, one at a time, off to the side.

    python3 bin/normalise.py            report what it would convert
    python3 bin/normalise.py --apply    convert one file
    python3 bin/normalise.py --apply --all   keep going until none are left

The channel lives by one number. The feeder consumes at 1x realtime forever, a
remux produces at roughly 10x, and a full re-encode at 0.06x measured on this
box with the pusher running. So a file prep has to encode is not slow, it is a
loss: the backlog drains for hours and the channel sits on the standby clip.
Five separate fixes tried to schedule around that and every one of them bought
a few days. There is nothing to schedule. The library has to be copyable.

On 2026-09-06 it was not: 38 of 62 files, six in ten. 25 of those were 1280x718,
two pixels short of the target, already h264 and already 50fps. The rest were
one vp9, one av1 and one 1080p, plus ten that hold the right picture but whose
copy comes out with packets carrying no PTS, which the pusher refuses.

This does that encoding once, here, where nobody is watching, so prep never has
to do it while the channel is live.

Three things keep it honest:

  - it refuses to run while the backlog is thin, because it shares two vCPUs
    with the pusher and would be taking from the thing it exists to protect
  - it checks its own output with the same probe prep uses, and keeps the
    original if the result still cannot be copied. Converting a file into
    another file prep will not touch is worse than leaving it alone, because it
    looks like progress
  - a file that fails is recorded, so the next run moves on instead of spending
    every night on the same broken video
"""
import json
import signal
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import common
import prep

LIBRARY = common.LIBRARY_DIR
MEDIA = (".mp4", ".mkv")
FAILED = common.STATE / "normalise_failed.json"

# Below this the channel is close enough to the standby clip that taking CPU
# from the pusher is the wrong trade. prep's own abandon floor is two chunks;
# this stays well above it, because unlike prep this work can always wait.
MIN_BACKLOG_SECONDS = 40 * 60
# One video is 25 minutes on average and this box encodes at well under
# realtime, so a run is hours. The ceiling stops one pathological file holding
# the slot for a day.
PER_FILE_TIMEOUT = 6 * 3600


RUNNING = []


def stop(child=None):
    """Kill the encoder we started, if any. Called on the way out of every path.

    An ffmpeg that outlives this process is not a tidiness problem: it keeps a
    core busy on a box with two of them, and the encoder it starves is the one
    keeping a picture on the wire.
    """
    for process in ([child] if child else list(RUNNING)):
        if process is None or process.poll() is not None:
            continue
        process.kill()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass


def _bye(*_):
    stop()
    sys.exit(143)


for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, _bye)
    except (ValueError, OSError):
        pass  # not the main thread, or a platform without it


def load_failed():
    try:
        data = json.loads(FAILED.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_failed(data):
    common.STATE.mkdir(parents=True, exist_ok=True)
    tmp = FAILED.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(FAILED)


def forget_gone(failed, present):
    """Drop failures for files that are no longer here.

    The janitor retires files while this runs, and one was deleted mid-list on
    2026-09-06: it failed with "No such file" and the name stayed in the ledger.
    The collector fetches from the same sources, so the day that video comes
    back it would be skipped forever on the strength of a failure that was
    really a deletion. A name that is gone has no verdict.
    """
    stale = [n for n in failed if n not in present]
    for name in stale:
        failed.pop(name, None)
    if stale:
        save_failed(failed)
    return len(stale)


def candidates():
    """Library files prep cannot copy, smallest first.

    Smallest first on purpose: the sooner one lands, the sooner the rotation has
    one more thing to play, and a 25 minute video is worth as much to the
    channel as a 90 minute one.
    """
    if not LIBRARY.is_dir():
        return []
    failed = load_failed()
    forget_gone(failed, {p.name for p in LIBRARY.iterdir() if p.is_file()})
    out = []
    for path in LIBRARY.iterdir():
        if not path.is_file() or path.suffix.lower() not in MEDIA:
            continue
        if path.name in failed:
            continue
        if prep.remux_verdict(path):
            continue
        out.append(path)
    return sorted(out, key=lambda p: p.stat().st_size)


def convert(path):
    """Re-encode into the target shape, and prove the result can be copied.

    Written beside the original and renamed over it only once the probe agrees,
    so a reader holding the old file keeps a whole video and a failure leaves
    the library exactly as it was.
    """
    target = path.with_suffix(".norm.mp4")
    target.unlink(missing_ok=True)
    started = time.time()
    # Popen and not run(), so a signal reaching this process can take the
    # encoder with it. Killing the parent alone leaves ffmpeg reparented to init
    # and still burning a core: two of them survived a stop on 2026-09-06 and
    # sat at 84% and 71% for two hours on a two vCPU box, which starved the
    # remuxes and blacked the channel out. The thing this exists to prevent.
    child = None
    try:
        child = subprocess.Popen(
            ["nice", "-n", "19", "ffmpeg", "-v", "error", "-y", "-i", str(path)]
            + list(common.ENCODE) + ["-f", "mp4", str(target)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        RUNNING.append(child)
        out, err = child.communicate(timeout=PER_FILE_TIMEOUT)
        done = subprocess.CompletedProcess(child.args, child.returncode, out, err)
    except (OSError, subprocess.SubprocessError) as exc:
        stop(child)
        target.unlink(missing_ok=True)
        return False, f"{type(exc).__name__}: {exc}"[:120]
    finally:
        if child in RUNNING:
            RUNNING.remove(child)
    took = time.time() - started
    if done.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        return False, ((done.stderr or "").strip().splitlines() or ["echec ffmpeg"])[-1][:120]

    # The check that matters. Re-encoding is supposed to produce a file the
    # pusher accepts; if it did not, replacing the original hides a file that
    # still cannot go on air behind one that looks converted.
    if not prep.remux_is_safe(target):
        target.unlink(missing_ok=True)
        return False, "reencode toujours pas copiable"
    if not prep.matches_target(target):
        target.unlink(missing_ok=True)
        return False, "reencode hors forme cible"

    target.replace(path.with_suffix(".mp4"))
    if path.suffix.lower() != ".mp4":
        path.unlink(missing_ok=True)
    return True, f"{took / 60:.0f} min"


def main(argv):
    apply = "--apply" in argv
    every = "--all" in argv

    backlog = prep.seconds_on_disk()
    todo = candidates()
    total = len([p for p in LIBRARY.iterdir()
                 if p.is_file() and p.suffix.lower() in MEDIA]) if LIBRARY.is_dir() else 0
    failed = load_failed()
    print(f"bibliotheque={total} a_convertir={len(todo)} echecs_connus={len(failed)} "
          f"tampon={backlog}s minimum={MIN_BACKLOG_SECONDS}s")

    if not todo:
        print("tout est copiable, rien a faire")
        return 0
    if backlog < MIN_BACKLOG_SECONDS:
        print("tampon trop mince: le pusher garde le CPU, on repassera")
        return 0
    if not apply:
        for path in todo[:5]:
            print(f"  convertirait {path.stat().st_size / 1024 ** 2:6.0f}Mo  {path.name[:58]}")
        if len(todo) > 5:
            print(f"  ... et {len(todo) - 5} autre(s)")
        print("essai a blanc, rien n'a ete touche. --apply pour agir.")
        return 0

    done = 0
    for path in todo:
        # re-read every time: the channel may have drained while the last one ran
        if prep.seconds_on_disk() < MIN_BACKLOG_SECONDS:
            print("tampon retombe sous le seuil, on s'arrete la")
            break
        print(f"conversion {path.name[:60]}", flush=True)
        ok, detail = convert(path)
        if ok:
            done += 1
            print(f"  fait en {detail}", flush=True)
        else:
            failed[path.name] = detail
            save_failed(failed)
            print(f"  echec: {detail}", flush=True)
        if not every:
            break
    print(f"{done} fichier(s) converti(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
