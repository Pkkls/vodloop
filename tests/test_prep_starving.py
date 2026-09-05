#!/usr/bin/env python3
"""Item choice under starvation. Run: python3 tests/test_prep_starving.py

Taking the queue strictly in order is what blacked the channel out for hours:
one file two pixels short of the target sits at the head, costs a full re-encode
at 0.77x realtime, and everything cheap behind it waits.

The first fix used a flat threshold and was wrong in a way worth keeping a test
for. Ten minutes of queue is plenty before a remux and nothing at all before a
two hour encode, so at 1200s of queue it happily took the expensive head, drained
the queue, and went dark for the rest of the encode. That exact case is the
third check below.

So the property under test is not "it prefers cheap items". It is that the same
queue produces a different choice depending on whether the queue can outlast
what the head costs, which is the only form of the question that scales with
the job.
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


HOUR = 3600
# a 33 minute VOD, the shape that actually blacked the channel out
EXPENSIVE = {"id": 1, "path": "/lib/wrong_shape.mp4", "title": "A", "duration": 2000}
EXPENSIVE_B = {"id": 2, "path": "/lib/wrong_shape_b.mp4", "title": "B", "duration": 2000}
CHEAP = {"id": 3, "path": "/lib/right_shape.mp4", "title": "C", "duration": 2000}
QUEUE = [EXPENSIVE, EXPENSIVE_B, CHEAP]

real_match = prep.matches_target
real_disk = prep.seconds_on_disk
prep.matches_target = lambda p: pathlib.Path(p).name == "right_shape.mp4"
try:
    print("cost")
    check("a file in the target shape costs nothing", prep.prepare_cost(CHEAP) == 0.0)
    check("one that is not costs more than its own length",
          prep.prepare_cost(EXPENSIVE) > EXPENSIVE["duration"],
          f"{prep.prepare_cost(EXPENSIVE):.0f}s for {EXPENSIVE['duration']}s")
    unknown = {"id": 4, "path": "/lib/wrong_shape_c.mp4", "title": "D"}
    check("an unknown length is assumed expensive, never cheap",
          prep.prepare_cost(unknown) == float("inf"))

    print("normalising is never free, even for a conformant file")
    # prepare_cost asks "what does playing this cost", normalise_cost asks "what
    # does redrawing it cost", and for a file already in the target shape those
    # two differ: nothing, and a full re-encode. Collapsing them back into one
    # function would make a caption pass look free and let it start on a queue
    # that cannot outlast it, which is the gap this pair exists to close.
    check("preparing a conformant file costs nothing",
          prep.prepare_cost(CHEAP) == 0.0)
    check("normalising the same file costs a full encode",
          prep.normalise_cost(CHEAP) > CHEAP["duration"],
          f"{prep.normalise_cost(CHEAP):.0f}s")
    # Library items carry no duration. Writing them off as infinite would be a
    # quiet, total failure: every one would fail the affordability test forever
    # and never get its caption, with nothing in any log to say so. The file is
    # probed instead.
    real_dur = prep.duration_of
    prep.duration_of = lambda p: 1800.0
    try:
        cost = prep.normalise_cost({"id": 5, "path": "/lib/no_duration.mp4"})
        check("an item with no duration is probed, not written off",
              cost == 1800.0 * prep.ENCODE_COST_FACTOR, f"{cost}")
        # the control: nothing readable at all must still be treated as costly,
        # otherwise the probe failing would look like a free job
        prep.duration_of = lambda p: 0.0
        check("an unreadable file stays expensive",
              prep.normalise_cost({"id": 6, "path": "/lib/broken.mp4"}) == float("inf"))
        check("no path and no duration stays expensive",
              prep.normalise_cost({"id": 7}) == float("inf"))
    finally:
        prep.duration_of = real_dur

    print("empty queue")
    prep.seconds_on_disk = lambda: 0
    check("takes the first item needing no re-encode",
          prep.cheapest_when_starving(QUEUE)["id"] == 3)

    print("the calibration that failed on 2026-09-05")
    # 1200s of queue against a job costing 3000s: the old flat threshold called
    # this healthy, took the head, and the channel went dark for two hours
    prep.seconds_on_disk = lambda: 1200
    picked = prep.cheapest_when_starving(QUEUE)
    check("queue shorter than the job does not take the job",
          picked["id"] == 3, f"id={picked['id']}")

    print("queue that can pay")
    # the control: same list, same shapes, only the queue is longer. Without it
    # every check above would also pass on a function that always picks cheap,
    # which would reorder the queue for good and never prepare anything else.
    #
    # Derived from the cost rather than written as a number of hours, so tuning
    # ENCODE_COST_FACTOR retunes the test with it. Hardcoding two hours here
    # meant raising the factor turned this check red without anything being
    # wrong, which teaches the next person to edit the assertion.
    enough = prep.prepare_cost(EXPENSIVE) + prep.COST_MARGIN_SECONDS + 1
    prep.seconds_on_disk = lambda: enough
    picked = prep.cheapest_when_starving(QUEUE)
    check("a queue that outlasts the job keeps its own order",
          picked["id"] == 1, f"id={picked['id']} at {enough:.0f}s")

    prep.seconds_on_disk = lambda: enough - 2
    picked = prep.cheapest_when_starving(QUEUE)
    check("one second short of it does not", picked["id"] == 3,
          f"id={picked['id']}")

    print("edges")
    prep.seconds_on_disk = lambda: 0
    check("nothing cheap available falls back to the head",
          prep.cheapest_when_starving([EXPENSIVE, EXPENSIVE_B])["id"] == 1)
    check("an empty list yields nothing", prep.cheapest_when_starving([]) is None)
    check("a margin is kept rather than spending the queue to the last second",
          prep.COST_MARGIN_SECONDS > 0, f"{prep.COST_MARGIN_SECONDS}s")
finally:
    prep.matches_target = real_match
    prep.seconds_on_disk = real_disk

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
