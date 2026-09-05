#!/usr/bin/env python3
"""Library rotation order. Run: python3 tests/test_prep_shuffle.py

Every item refill_from_library queues carries the same added_at, and
playback_order breaks that tie with a stable sort. So the order this function
builds its list in is literally the order the channel plays, and building it
sorted meant the library ran alphabetically from end to end, then did it again
identically on the next pass.

A test that only checked "the order is not alphabetical" could pass by luck on a
small library, and would pass on a function that scrambled things some other
unintended way. So the pair below runs the same code twice, once with the
shuffle neutralised, and asserts the order is alphabetical exactly when the
shuffle is absent.
"""
import pathlib
import random
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import prep  # noqa: E402

# The fixtures here are empty files, not videos, so the real probe answers "not
# copyable" for all of them and the refill queues nothing. What a real ffmpeg
# says about a real file is measured in test_normalise.py and
# test_prep_remux_safety.py; here it only has to stay out of the way of the
# question being asked, which is what the refill does with what it is given.
prep.remux_verdict = lambda p: True

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


NAMES = [f"vod_{i:02d}.mp4" for i in range(20)]


def queued_order(library):
    queue = {"items": [], "seq": 0}
    prep.refill_from_library(queue)
    return [pathlib.Path(i["path"]).name for i in queue["items"]]


with tempfile.TemporaryDirectory() as tmp:
    library = pathlib.Path(tmp)
    for name in NAMES:
        (library / name).write_bytes(b"x")

    real_lib, prep.LIBRARY = prep.LIBRARY, library
    real_disk, prep.seconds_on_disk = prep.seconds_on_disk, lambda: 0
    real_shuffle = prep.random.shuffle
    try:
        print("shuffled")
        random.seed(1234)
        order = queued_order(library)
        check("every file is queued exactly once",
              sorted(order) == sorted(NAMES), f"{len(order)} items")
        check("the order is not the alphabetical one", order != sorted(NAMES),
              " ".join(order[:4]))

        # the control: neutralise the shuffle and the same code must produce the
        # alphabetical order. Without this the check above could be passing on
        # any accidental reordering rather than on the shuffle being called.
        print("shuffle neutralised")
        prep.random.shuffle = lambda seq: None
        try:
            plain = queued_order(library)
            check("without the shuffle it is alphabetical again",
                  plain == sorted(NAMES), " ".join(plain[:4]))
        finally:
            prep.random.shuffle = real_shuffle

        print("two passes differ")
        # a rotation that repeats itself is barely a rotation
        random.seed(1)
        first = queued_order(library)
        random.seed(2)
        second = queued_order(library)
        check("a later pass does not replay the same order", first != second,
              f"{first[:3]} vs {second[:3]}")

        print("votes still outrank the shuffle")
        # Randomising added_at must not cost the chat its say: a voted item has
        # to jump the queue however the shuffle placed it.
        import chatlogic
        voted = {"id": 99, "status": "pending", "added_at": 9e9,
                 "votes": ["someone"], "title": "voted"}
        plain_items = [{"id": n, "status": "pending", "added_at": n,
                        "votes": [], "title": str(n)} for n in range(5)]
        order = chatlogic.playback_order({"items": plain_items + [voted]})
        check("one vote beats every unvoted item, whatever the shuffle did",
              order[0]["id"] == 99, f"head id={order[0]['id']}")
        # the control: strip the vote and the same item falls to the back,
        # proving the check above measured the vote and not its position
        voted["votes"] = []
        order = chatlogic.playback_order({"items": plain_items + [voted]})
        check("without the vote it sinks to its added_at position",
              order[-1]["id"] == 99, f"tail id={order[-1]['id']}")
    finally:
        prep.LIBRARY = real_lib
        prep.seconds_on_disk = real_disk

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
