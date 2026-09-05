#!/usr/bin/env python3
"""Item choice under starvation. Run: python3 tests/test_prep_starving.py

Taking the queue strictly in order is what blacked the channel out for hours:
one file two pixels short of the target sits at the head, costs a full
re-encode at 0.77x realtime, and everything cheap behind it waits. The fix bends
the order only while the queue is nearly empty.

The claim worth testing is therefore not "it picks a cheap file". It is that the
same queue produces a different choice depending on how much video is on disk,
because a function that ignored the buffer would pass a one-sided check and
would reorder the queue forever, which is a different bug with the same shape.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import prep  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


# expensive first, cheap third: strict order picks the expensive one
QUEUE = [
    {"id": 1, "path": "/lib/wrong_shape_a.mp4", "title": "A"},
    {"id": 2, "path": "/lib/wrong_shape_b.mp4", "title": "B"},
    {"id": 3, "path": "/lib/right_shape.mp4", "title": "C"},
]

real_match = prep.matches_target
real_disk = prep.seconds_on_disk
prep.matches_target = lambda p: pathlib.Path(p).name == "right_shape.mp4"
try:
    print("starving")
    prep.seconds_on_disk = lambda: 0
    picked = prep.cheapest_when_starving(QUEUE)
    check("an empty queue takes the first item needing no re-encode",
          picked["id"] == 3, f"id={picked['id']}")

    print("healthy")
    prep.seconds_on_disk = lambda: prep.STARVING_SECONDS + 1
    picked = prep.cheapest_when_starving(QUEUE)
    # the control: same list, same shapes, only the buffer differs. Without it
    # the check above would also pass on a function that always picks the cheap
    # item, which would silently reorder the queue for good.
    check("a healthy queue keeps its own order", picked["id"] == 1,
          f"id={picked['id']}")

    print("nothing cheap available")
    prep.seconds_on_disk = lambda: 0
    only_expensive = [QUEUE[0], QUEUE[1]]
    picked = prep.cheapest_when_starving(only_expensive)
    check("falls back to the head rather than stalling", picked["id"] == 1,
          f"id={picked['id']}")

    print("edges")
    check("an empty list yields nothing", prep.cheapest_when_starving([]) is None)
    prep.seconds_on_disk = lambda: 0
    no_path = [{"id": 9, "url": "https://example.invalid/v", "title": "url item"}]
    picked = prep.cheapest_when_starving(no_path)
    check("an item with no local file is not probed and still chosen",
          picked["id"] == 9, f"id={picked['id']}")

    check("the threshold leaves room for a remux to finish",
          prep.STARVING_SECONDS >= 5 * 60, f"{prep.STARVING_SECONDS}s")
    check("it triggers well below the point prep stops preparing",
          prep.STARVING_SECONDS < prep.common.AHEAD_LIMIT_SECONDS,
          f"{prep.STARVING_SECONDS} vs {prep.common.AHEAD_LIMIT_SECONDS}")
finally:
    prep.matches_target = real_match
    prep.seconds_on_disk = real_disk

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
