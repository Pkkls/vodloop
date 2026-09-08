#!/usr/bin/env python3
"""A probe that could not run. Run: python3 tests/test_prep_unmeasured.py

remux_verdict answers one question for two callers who need opposite defaults
when it cannot measure at all.

prepare() choosing what to cut next can say no and look again a pass later; the
cost is one pass. drop_unremuxable deciding whether to throw an item out of the
queue cannot: a no there removes the video from the rotation, and refill only
puts it back once the queue has drained.

Measured 2026-09-08: a probe that could not run on a busy box took the only
fresh video in a two file library out of the queue, and the channel looped the
one it had already played. matches_target and audio_matches_target both said
True on that file the whole time.
"""
import os
import pathlib
import sys
import tempfile

os.environ["VODLOOP_ROOT"] = tempfile.mkdtemp(prefix="prep-unmeasured-")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import common  # noqa: E402
import prep  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


video = pathlib.Path(os.environ["VODLOOP_ROOT"]) / "videos" / "A_Film-abcDEF12345.mp4"
video.parent.mkdir(parents=True, exist_ok=True)
video.write_bytes(b"not really a video, the probes below are stubbed")

real_matches, real_safe = prep.matches_target, prep.remux_is_safe
prep.matches_target = lambda _p: True

try:
    print("the probe cannot be taken")
    prep.remux_is_safe = lambda _p: None
    check("choosing what to cut next refuses, and looks again later",
          prep.remux_verdict(video) is False)
    check("deciding what to throw away does not throw it away",
          prep.remux_verdict(video, unmeasured=True) is True)
    check("and nothing was written down about it",
          not (common.STATE / "remux.json").exists()
          or "abcDEF12345" not in (common.STATE / "remux.json").read_text())

    print("a measured refusal, for comparison")
    # the control: when the probe DOES run and says no, both callers must say no,
    # or the checks above would pass on a function that never refuses anything
    prep.remux_is_safe = lambda _p: False
    check("choosing refuses", prep.remux_verdict(video) is False)
    check("and throwing away also refuses",
          prep.remux_verdict(video, unmeasured=True) is False)

    print("the queue keeps what was never measured")
    queue = {"seq": 2, "items": [
        {"id": 1, "status": "pending", "by": "file", "path": str(video)},
        {"id": 2, "status": "pending", "by": "file", "path": str(video)},
    ]}
    prep.remux_is_safe = lambda _p: None
    # the cache remembers the measured False from the control above, so it has to
    # go or this measures the cache rather than the change
    (common.STATE / "remux.json").unlink(missing_ok=True)
    dropped = prep.drop_unremuxable(queue)
    check("an unmeasured item stays in the queue",
          dropped == 0 and len(queue["items"]) == 2, f"{dropped} retire(s)")

    prep.remux_is_safe = lambda _p: False
    (common.STATE / "remux.json").unlink(missing_ok=True)
    dropped = prep.drop_unremuxable(queue)
    check("a measured refusal is still removed",
          dropped == 2 and queue["items"] == [], f"{dropped} retire(s)")
finally:
    prep.matches_target, prep.remux_is_safe = real_matches, real_safe

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
