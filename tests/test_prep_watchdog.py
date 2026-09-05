#!/usr/bin/env python3
"""An encode is abandoned before it takes the channel off the air. Run:

    python3 tests/test_prep_watchdog.py

Four times the channel went to the standby clip for hours, always the same way:
prep started an encode it could not finish before the backlog ran out. Each time
the fix was a better estimate of what an encode costs. A flat threshold, then a
factor of 1.5, then 4, then a cost model that had not been told remux_is_safe
moved ten files onto the expensive path. Each better guess bought time until the
next wrong one.

wait_for_encode stops guessing. The backlog is measured while the encoder runs,
and an encode still going when it falls to two chunks is losing the race by
definition, whatever any estimate claimed. That is what this file pins down.

The property is not "it abandons encodes". It is that the SAME encoder, with
the same budget, is kept or abandoned purely on what the backlog does, which is
why every check below is paired with its opposite.
"""
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import prep  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


class Encoder:
    """An ffmpeg that never finishes on its own, like a two hour re-encode."""

    def __init__(self, finishes_after=None):
        self.calls = 0
        self.finishes_after = finishes_after
        self.killed = False

    def communicate(self, timeout=None):
        self.calls += 1
        if self.finishes_after is not None and self.calls >= self.finishes_after:
            return (b"", b"done")
        raise subprocess.TimeoutExpired("ffmpeg", timeout)

    def kill(self):
        self.killed = True


real_disk = prep.seconds_on_disk
real_poll = prep.ENCODE_POLL_SECONDS
prep.ENCODE_POLL_SECONDS = 0  # the loop's pace is not what is under test
try:
    FLOOR = prep.ABANDON_BELOW_SECONDS

    print("the backlog decides, not the clock")
    prep.seconds_on_disk = lambda: FLOOR
    _, starved = prep.wait_for_encode(Encoder(), budget=9999)
    check("at the floor the encode is abandoned", starved is True)

    prep.seconds_on_disk = lambda: FLOOR - 1
    _, starved = prep.wait_for_encode(Encoder(), budget=9999)
    check("below it too", starved is True)

    # The control. Same encoder, same budget, same endless job: only the backlog
    # differs. Without it every check here would also pass on a watchdog that
    # abandoned everything, which would mean no expensive file is ever prepared
    # and the library slowly stops rotating.
    print("one second more of backlog, and it is left alone")
    encoder = Encoder(finishes_after=3)
    prep.seconds_on_disk = lambda: FLOOR + 1
    err, starved = prep.wait_for_encode(encoder, budget=9999)
    check("a healthy backlog lets the encode run", starved is False)
    check("and it really did keep waiting, not return at once",
          encoder.calls == 3, f"{encoder.calls} tours")
    check("the encoder's own output is passed back", err == "done", err)

    print("a backlog that drains while the encode runs")
    # the realistic shape: fine at the start, gone four polls later
    levels = iter([FLOOR + 3000, FLOOR + 2000, FLOOR + 900, FLOOR - 10])
    prep.seconds_on_disk = lambda: next(levels)
    encoder = Encoder()
    _, starved = prep.wait_for_encode(encoder, budget=9999)
    check("it is caught on the way down, not only at zero", starved is True)

    print("the wall clock still applies")
    prep.seconds_on_disk = lambda: FLOOR + 10000
    try:
        prep.wait_for_encode(Encoder(), budget=-1)
        check("an encode past its budget still times out", False)
    except subprocess.TimeoutExpired:
        check("an encode past its budget still times out", True)

    print("what the floor is worth")
    check("the floor is more than one chunk, so there is something left to play",
          prep.ABANDON_BELOW_SECONDS >= 2 * prep.common.CHUNK_SECONDS,
          f"{prep.ABANDON_BELOW_SECONDS}s")

    print("an abandoned item is not a failed one")
    source = (pathlib.Path(__file__).resolve().parent.parent
              / "bin" / "prep.py").read_text(encoding="utf-8")
    check("it goes back to pending, not to error",
          'entry["status"] = ("ready" if ok\n' in source
          and '"pending" if deferred else "error")' in source)
    check("and a dropped file is not swept into failed/",
          'if not ok and not item.get("defer"):' in source)
finally:
    prep.seconds_on_disk = real_disk
    prep.ENCODE_POLL_SECONDS = real_poll

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
