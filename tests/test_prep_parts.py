#!/usr/bin/env python3
"""Long streams that arrive in pieces. Run: python3 tests/test_prep_parts.py

The board's card cannot hold an eleven hour stream whole, so a video that long
is fetched in parts and lands in the library as several files. Everything
downstream is meant to treat them as ordinary library files. Two places cannot,
and both fail silently if they are wrong, which is why they are asserted here
rather than left to be noticed on air.

The draw has to pick videos, not files. A plain shuffle over the files scatters
the parts of one stream through the rotation and airs hour nine before hour one,
and nothing in the pipeline would report that as a fault.

The collector has to read the video id back off a part's filename. If it cannot,
library_ids() comes back empty, every video already downloaded looks absent, and
the collector queues the entire pool again on every run until the disk fills.
"""
import os
import pathlib
import random
import sys
import tempfile

os.environ["VODLOOP_ROOT"] = tempfile.mkdtemp(prefix="prep-parts-")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import collector  # noqa: E402
import prep  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def p(name):
    return pathlib.Path("/home/ubuntu/videos") / name


LONG = [p(f"A_Very_Long_Stream-aaaaaaaaaaa.p{n:02d}of04.mp4") for n in (1, 2, 3, 4)]
OTHER = [p(f"Another_One-bbbbbbbbbbb.p{n:02d}of03.mp4") for n in (1, 2, 3)]
WHOLE = [p("Short_Enough-ccccccccccc.mp4"), p("Also_Short-ddddddddddd.mp4")]

print("reading a part's name")
check("the video and the position come off the filename",
      prep.video_of(LONG[2]) == ("aaaaaaaaaaa", 3), str(prep.video_of(LONG[2])))
check("a whole video is its own group at position zero",
      prep.video_of(WHOLE[0])[1] == 0, str(prep.video_of(WHOLE[0])))
check("two whole videos are two different groups",
      prep.video_of(WHOLE[0])[0] != prep.video_of(WHOLE[1])[0])

print("the draw")
# Run it enough times that an ordering bug cannot hide behind one lucky shuffle.
seen_orders = set()
for _ in range(200):
    out = prep.shuffled_by_video(list(LONG + OTHER + WHOLE))
    check_order = [prep.video_of(x) for x in out]
    a = [i for _, i in check_order if _ == "aaaaaaaaaaa"]
    b = [i for _, i in check_order if _ == "bbbbbbbbbbb"]
    if a != [1, 2, 3, 4] or b != [1, 2, 3]:
        failures.append("parts came out of order")
        print("  FAIL  parts came out of order", a, b)
        break
    # and consecutive: the positions of one video's parts must be adjacent
    idx = [n for n, x in enumerate(out) if prep.video_of(x)[0] == "aaaaaaaaaaa"]
    if idx != list(range(idx[0], idx[0] + 4)):
        failures.append("parts were interleaved with another video")
        print("  FAIL  parts were interleaved", idx)
        break
    seen_orders.add(tuple(str(x) for x in out))
else:
    check("over 200 draws the parts are always in order and adjacent", True)

# The control. If the draw were not random at all the checks above would still
# pass, and the rotation would be identical every pass, which is the bug the
# shuffle exists to prevent.
check("the draw is still random across videos", len(seen_orders) > 1,
      f"{len(seen_orders)} ordres distincts sur 200 tirages")

print("a library with no parts behaves as before")
plain = [p(f"V{n}-{'e' * 10}{n}.mp4") for n in range(6)]
orders = {tuple(str(x) for x in prep.shuffled_by_video(list(plain)))
          for _ in range(200)}
check("it is still shuffled", len(orders) > 1, f"{len(orders)} ordres distincts")
check("and nothing is lost or duplicated",
      sorted(prep.shuffled_by_video(list(plain))) == sorted(plain))

print("the collector reads the id off a part")
# This is the one that empties library_ids() and refetches the pool forever.
for path in LONG[:1] + OTHER[:1] + WHOLE:
    found = collector.VIDEO_ID.search(path.name)
    check(f"id read from {path.name[:34]}", found is not None,
          found.group(1) if found else "AUCUNE CORRESPONDANCE")
check("a part and its whole video give the same id",
      collector.VIDEO_ID.search(LONG[0].name).group(1) == "aaaaaaaaaaa")

# the control: a name with no id must still not be mistaken for one, or the
# looser pattern would be matching things it should not
check("a name without an id is still refused",
      collector.VIDEO_ID.search("no_id_here.p01of04.mp4") is None)
check("a part suffix alone does not invent an id",
      collector.VIDEO_ID.search("Titre.p01of04.mp4") is None)

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(sorted(set(failures))))
    sys.exit(1)
print("all passed")
