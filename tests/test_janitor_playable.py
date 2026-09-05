#!/usr/bin/env python3
"""The janitor must not delete the channel. Run:

    python3 tests/test_janitor_playable.py

Since refill_from_library only queues files prep can copy, that subset IS the
channel. The rest is material waiting for normalise.py and contributes nothing
until it has been through it.

The janitor did not know that. It retired by age alone, so on 2026-09-06 it
could take a playable file off the disk and leave four unplayable ones sitting
next to it, buying the same space at the cost of the only thing on air. Worse,
it can empty the playable set entirely while the library still looks full,
which is an outage with nothing visible to explain it.

Two properties, and both need their opposite next to them: the unplayable go
first, and the playable floor is never crossed however hungry the disk is.
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import janitor  # noqa: E402
import prep  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


EMPTY = {"items": []}


def build(tmp, playable, unplayable):
    """Files with distinct mtimes, oldest first in each list."""
    lib = pathlib.Path(tmp)
    stamp = 1000
    made = {}
    for name in playable + unplayable:
        p = lib / name
        p.write_bytes(b"x" * 10)
        import os
        os.utime(p, (stamp, stamp))
        stamp += 10
        made[name] = p
    return lib, made


real_lib, real_verdict = janitor.LIBRARY, prep.remux_verdict
real_floor = janitor.MIN_PLAYABLE_FILES
try:
    janitor.MIN_PLAYABLE_FILES = 2

    print("what gets retired first")
    with tempfile.TemporaryDirectory() as tmp:
        # the playable ones are the OLDEST, so age alone would take them first
        playable = ["p1.mp4", "p2.mp4", "p3.mp4", "p4.mp4"]
        unplayable = ["u1.mp4", "u2.mp4"]
        lib, _ = build(tmp, playable, unplayable)
        janitor.LIBRARY = lib
        prep.remux_verdict = lambda p: pathlib.Path(p).name.startswith("p")

        order = [p.name for p in janitor.retirable(EMPTY)]
        check("the unplayable go before the playable",
              order[:2] == ["u1.mp4", "u2.mp4"], str(order))
        check("and the newest playable are not offered at all",
              "p3.mp4" not in order and "p4.mp4" not in order, str(order))
        check("the older playable ones still are, once the floor is met",
              "p1.mp4" in order and "p2.mp4" in order, str(order))

        # The control. Same files, same ages, but nothing is playable: then the
        # split has nothing to sort on and pure age must come back. Without it
        # every check above would also pass on a retirable() that had simply
        # started returning its input in a fixed order.
        print("with nothing playable it is age again")
        prep.remux_verdict = lambda p: False
        order = [p.name for p in janitor.retirable(EMPTY)]
        check("oldest first", order == playable + unplayable, str(order))

        print("with everything playable the floor still holds")
        prep.remux_verdict = lambda p: True
        order = [p.name for p in janitor.retirable(EMPTY)]
        # u1 and u2 were written last, so with every file playable they are the
        # two newest and it is those the floor keeps, not the p names
        check("exactly the two newest are held back",
              order == ["p1.mp4", "p2.mp4", "p3.mp4", "p4.mp4"], str(order))

    print("a library smaller than the floor is untouchable")
    with tempfile.TemporaryDirectory() as tmp:
        lib, _ = build(tmp, ["p1.mp4"], [])
        janitor.LIBRARY = lib
        prep.remux_verdict = lambda p: True
        check("the only playable file is never offered",
              janitor.retirable(EMPTY) == [], str(janitor.retirable(EMPTY)))
        # the control: the same single file, now unplayable, is fair game
        prep.remux_verdict = lambda p: False
        check("an unplayable one with no floor to protect is",
              [p.name for p in janitor.retirable(EMPTY)] == ["p1.mp4"])

    print("busy files are still off limits")
    with tempfile.TemporaryDirectory() as tmp:
        lib, made = build(tmp, [], ["u1.mp4", "u2.mp4"])
        janitor.LIBRARY = lib
        prep.remux_verdict = lambda p: False
        queue = {"items": [{"path": str(made["u1.mp4"]), "status": "ready"}]}
        check("a file a queue entry still needs is not retired",
              [p.name for p in janitor.retirable(queue)] == ["u2.mp4"])
finally:
    janitor.LIBRARY, prep.remux_verdict = real_lib, real_verdict
    janitor.MIN_PLAYABLE_FILES = real_floor

check("the floor leaves a real rotation, not one file",
      janitor.MIN_PLAYABLE_FILES >= 10, f"{janitor.MIN_PLAYABLE_FILES}")

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
