#!/usr/bin/env python3
"""Queue top-up. Run: python3 tests/test_topup.py

Two claims carry this script.

It must pick the file it used longest ago. Topping up by hand all day replayed
the same three VODs, because every run walked the library alphabetically and
took the head. A rotation that repeats is barely better than the standby clip it
replaces, so the ordering is the feature, not an incidental detail.

And it must do nothing when the queue is healthy. This exists to break a
starvation loop, not to add a second writer competing with prep for two vCPUs.
A run that seeds while the queue is full is the failure mode that would cut the
RTMP session, so the check below asserts the healthy case touches nothing.
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import common  # noqa: E402
import prep  # noqa: E402
import topup  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    library = root / "videos"
    library.mkdir()
    for name in ("alpha.mp4", "bravo.mp4", "charlie.mp4"):
        (library / name).write_bytes(b"x")

    real_lib, topup.LIBRARY = topup.LIBRARY, library
    real_match, prep.matches_target = prep.matches_target, lambda p: True
    try:
        print("rotation")
        never = topup.candidates({})
        check("an untouched library keeps its own order",
              [p.name for p in never] == ["alpha.mp4", "bravo.mp4", "charlie.mp4"],
              str([p.name for p in never]))

        # alpha used most recently, bravo before it, charlie never
        seeded = {"alpha.mp4": 3000, "bravo.mp4": 1000}
        order = [p.name for p in topup.candidates(seeded)]
        check("the file never used comes first", order[0] == "charlie.mp4", str(order))
        check("the most recently used comes last", order[-1] == "alpha.mp4", str(order))

        # the control: reverse which one is stale and the order must follow. A
        # sort that ignored the ledger would return the same list both times.
        flipped = [p.name for p in topup.candidates({"charlie.mp4": 3000, "bravo.mp4": 1000})]
        check("the order tracks the ledger rather than the filename",
              flipped != order and flipped[0] == "alpha.mp4", str(flipped))

        print("does nothing when the queue is healthy")
        # every path through main() now takes the lock before it measures, so
        # all of this needs POSIX. Reported as not run rather than as passes it
        # did not earn; the server runs them for real.
        try:
            import fcntl  # noqa: F401
        except ImportError:
            print("  SKIP  a full queue is left alone  (needs POSIX, run on the server)")
            print("  SKIP  an empty queue does seed  (needs POSIX, run on the server)")
        else:
            calls = []
            real_seed, topup.seed = topup.seed, lambda p, i: calls.append(p) or True
            real_disk = prep.seconds_on_disk
            prep.seconds_on_disk = lambda: topup.TARGET_SECONDS + 1
            real_state, common.STATE = common.STATE, root / "state"
            common.STATE.mkdir(parents=True, exist_ok=True)
            topup.LEDGER = common.STATE / "topup.json"
            topup.LOCK = common.STATE / "topup.lock"
            try:
                rc = topup.main([])
                check("a full queue is left alone", calls == [] and rc == 0, str(calls))

                # the control: the same call on an empty queue must seed,
                # otherwise the check above passes on a script that never seeds
                prep.seconds_on_disk = lambda: 0
                topup.main([])
                check("an empty queue does seed", len(calls) > 0, f"{len(calls)} seeded")
            finally:
                topup.seed = real_seed
                prep.seconds_on_disk = real_disk
                common.STATE = real_state

        print("a held lock costs nothing")
        try:
            import fcntl
        except ImportError:
            print("  SKIP  a held lock measures nothing  (needs POSIX, run on the server)")
        else:
            lock_state = root / "lockstate"
            lock_state.mkdir(parents=True, exist_ok=True)
            real_state2, common.STATE = common.STATE, lock_state
            topup.LOCK = lock_state / "topup.lock"
            topup.LEDGER = lock_state / "topup.json"
            probed = []
            real_disk2 = prep.seconds_on_disk
            prep.seconds_on_disk = lambda: (probed.append(1), 0)[1]
            real_seed2, topup.seed = topup.seed, lambda p, i: True
            holder = open(topup.LOCK, "w")
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                rc = topup.main([])
                check("a held lock returns without measuring the queue",
                      probed == [] and rc == 0, f"{len(probed)} probe(s)")

                # the control: release the lock and the same call must measure.
                # Without it the check above would pass on a main() that never
                # measures at all, which is not the property being claimed.
                fcntl.flock(holder, fcntl.LOCK_UN)
                topup.main([])
                check("a free lock does measure", len(probed) > 0,
                      f"{len(probed)} probe(s)")
            finally:
                holder.close()
                prep.seconds_on_disk = real_disk2
                topup.seed = real_seed2
                common.STATE = real_state2

        print("a stuck remux is abandoned, not waited on forever")
        segments = root / "segments"
        real_seg, common.SEGMENTS = common.SEGMENTS, segments
        real_run = topup.subprocess.run

        def hang(*_a, **_kw):
            # what a stuck ffmpeg looks like from here, after it has already
            # written part of its output
            segments.mkdir(parents=True, exist_ok=True)
            (segments / "90000_00000.ts").write_bytes(b"partial")
            raise topup.subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

        topup.subprocess.run = hang
        try:
            ok = topup.seed(library / "alpha.mp4", 90000)
            check("a timed out remux reports failure", ok is False)
            check("its partial chunks are removed, not left for the feeder",
                  list(segments.glob("90000_*.ts")) == [],
                  str(list(segments.glob("90000_*.ts"))))
        finally:
            topup.subprocess.run = real_run
            common.SEGMENTS = real_seg
        check("the timeout is above any honest remux",
              topup.REMUX_TIMEOUT_SECONDS >= 10 * 60,
              f"{topup.REMUX_TIMEOUT_SECONDS}s")

        print("thresholds")
        check("the target clears prep's normalisation gate",
              topup.TARGET_SECONDS > prep.NORMALISE_ABOVE_SECONDS,
              f"{topup.TARGET_SECONDS} vs {prep.NORMALISE_ABOVE_SECONDS}")
        check("it stops below the level where prep itself stops",
              topup.TARGET_SECONDS < common.AHEAD_LIMIT_SECONDS,
              f"{topup.TARGET_SECONDS} vs {common.AHEAD_LIMIT_SECONDS}")
        check("chunk ids sort after anything prep produces",
              topup.FIRST_ID > 99, str(topup.FIRST_ID))
    finally:
        topup.LIBRARY = real_lib
        prep.matches_target = real_match

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
