#!/usr/bin/env python3
"""The library must only ever offer prep files it can copy. Run:

    python3 tests/test_normalise.py

One number decides whether this channel stays on air. The feeder consumes at 1x
realtime forever; a remux produces at about 10x and a full re-encode at 0.06x,
measured on this box with the pusher running. A file prep has to encode is not
slow, it is a loss the schedule cannot recover from.

Five fixes tried to schedule around that. The sixth stops trying: files that
cannot be copied do not go in the queue at all, they go to normalise.py, and
they come back once it has converted them. Measured 2026-09-06, 38 of 62 files
could not be copied, and refill_from_library could not even fire because it
waits for an empty pending list and those items never cleared.

So the properties here are the two halves of that invariant: nothing
uncopyable reaches the queue, and nothing leaves normalise.py still uncopyable.
Every check is paired with its opposite, because a filter that rejects
everything would satisfy the first half while emptying the channel.
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import common  # noqa: E402
import normalise  # noqa: E402
import prep  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def item(n, path, by="file", status="pending"):
    return {"id": n, "path": path, "status": status, "by": by, "votes": [],
            "added_at": 0.0, "url": path, "title": "t"}


COPYABLE = "/lib/good.mp4"
STUCK = "/lib/bad.mp4"

real_verdict = prep.remux_verdict
real_save = common.save_queue
prep.remux_verdict = lambda p: str(p) == COPYABLE
common.save_queue = lambda q: None
try:
    print("what the queue is allowed to hold")
    queue = {"items": [item(1, COPYABLE), item(2, STUCK), item(3, STUCK)], "seq": 3}
    dropped = prep.drop_unremuxable(queue)
    check("files prep cannot copy are taken out", dropped == 2, f"{dropped}")
    check("and the copyable one stays",
          [i["id"] for i in queue["items"]] == [1],
          str([i["id"] for i in queue["items"]]))

    # The control. Without it, a drop_unremuxable that emptied the queue outright
    # would pass every check above, and the channel would have nothing to play.
    print("a library that is entirely fine is left alone")
    queue = {"items": [item(1, COPYABLE), item(2, COPYABLE)], "seq": 2}
    check("nothing is dropped when nothing needs it",
          prep.drop_unremuxable(queue) == 0 and len(queue["items"]) == 2)

    print("what it must not touch")
    queue = {"items": [item(1, STUCK, by="u42"), item(2, STUCK)], "seq": 2}
    prep.drop_unremuxable(queue)
    check("a request someone made is not silently dropped",
          [i["id"] for i in queue["items"]] == [1],
          str([i["id"] for i in queue["items"]]))
    queue = {"items": [item(1, STUCK, status="ready"),
                       item(2, STUCK, status="preparing")], "seq": 2}
    check("only pending items are considered",
          prep.drop_unremuxable(queue) == 0 and len(queue["items"]) == 2)

    print("the refill only offers what can be copied")
    with tempfile.TemporaryDirectory() as tmp:
        lib = pathlib.Path(tmp)
        good, bad = lib / "good.mp4", lib / "bad.mp4"
        for p in (good, bad):
            p.write_bytes(b"x" * 10)
        prep.remux_verdict = lambda p: pathlib.Path(p).name == "good.mp4"
        real_lib, prep.LIBRARY = prep.LIBRARY, lib
        real_disk, prep.seconds_on_disk = prep.seconds_on_disk, lambda: 0
        # The refill remembers what it queued and keeps it out of the draw for a
        # week, which is its own property and is measured in
        # test_prep_history.py. Here the same two files are asked for three
        # times in a row, so the memory is switched off: what is being measured
        # is the copyable filter, not the rotation.
        real_load, prep.load_history = prep.load_history, lambda: {}
        real_save_hist, prep.save_history = prep.save_history, lambda h: None
        try:
            queue = {"items": [], "seq": 0}
            added = prep.refill_from_library(queue)
            names = [pathlib.Path(i["path"]).name for i in queue["items"]]
            check("only the copyable file is queued",
                  added == 1 and names == ["good.mp4"], str(names))

            # the other control: with every file copyable the refill must queue
            # them all, or this filter would quietly shrink the rotation
            prep.remux_verdict = lambda p: True
            queue = {"items": [], "seq": 0}
            check("with nothing to convert, the whole library is queued",
                  prep.refill_from_library(queue) == 2)

            prep.remux_verdict = lambda p: False
            queue = {"items": [], "seq": 0}
            check("and none of it when nothing can be copied",
                  prep.refill_from_library(queue) == 0)
        finally:
            prep.LIBRARY, prep.seconds_on_disk = real_lib, real_disk
            prep.load_history, prep.save_history = real_load, real_save_hist
finally:
    prep.remux_verdict = real_verdict
    common.save_queue = real_save

print("the converter does not take CPU from a thin channel")
check("its floor is far above prep's own abandon floor",
      normalise.MIN_BACKLOG_SECONDS > prep.ABANDON_BELOW_SECONDS,
      f"{normalise.MIN_BACKLOG_SECONDS}s contre {prep.ABANDON_BELOW_SECONDS}s")

source = (pathlib.Path(__file__).resolve().parent.parent
          / "bin" / "normalise.py").read_text(encoding="utf-8")
print("what it must prove before replacing a file")
check("the result is probed with the same check prep uses",
      "prep.remux_is_safe(target)" in source)
check("and rejected if it still cannot be copied",
      "target.unlink(missing_ok=True)\n        return False, \"reencode toujours pas copiable\"" in source)
check("the original is only replaced after that",
      source.index("prep.remux_is_safe(target)") < source.index("target.replace("))
check("it runs at the lowest priority",
      '"nice", "-n", "19"' in source)
check("a file that fails is remembered, not retried nightly",
      "failed[path.name] = detail" in source and "if path.name in failed" in source)

print("a failure on a file that no longer exists is not a verdict")
# The janitor deletes while this runs. One file went mid-list on 2026-09-06 and
# failed with "No such file"; the name stayed in the ledger, and the collector
# fetches from the same sources, so the day it came back it would be skipped
# forever on the strength of a deletion.
ledger = {"gone.mp4": "No such file", "real.mp4": "reencode hors forme cible"}
real_save_f, normalise.save_failed = normalise.save_failed, lambda d: None
try:
    dropped = normalise.forget_gone(ledger, {"real.mp4", "other.mp4"})
    check("the vanished name is forgotten", dropped == 1 and "gone.mp4" not in ledger)
    # the control: a genuine failure on a file still present must survive, or
    # this would clear the ledger every run and retry broken videos nightly
    check("a real failure on a file still there is kept", "real.mp4" in ledger)
finally:
    normalise.save_failed = real_save_f

print("the verdict is the same pair prepare() tests")
# Asking only remux_is_safe let 18 files at 1280x718 into the queue. Their
# copies are valid, so the probe said yes; matches_target still refuses them and
# prep re-encoded them anyway, which is the exact cost this filter exists to
# keep out. Both halves, or the filter is decorative.
real_m, real_s = prep.matches_target, prep.remux_is_safe
real_file = prep.REMUX_VERDICTS
with tempfile.TemporaryDirectory() as tmp:
    prep.REMUX_VERDICTS = pathlib.Path(tmp) / "v.json"
    sample = pathlib.Path(tmp) / "s.mp4"
    try:
        for shape, copyable, want, label in (
                (True, True, True, "bonne forme et copie saine: copiable"),
                (False, True, False, "copie saine mais mauvaise forme: refuse"),
                (True, False, False, "bonne forme mais copie cassee: refuse"),
                (False, False, False, "ni l'un ni l'autre: refuse")):
            sample.write_bytes(b"x" * (10 + int(shape) * 2 + int(copyable)))
            prep.matches_target = lambda p, v=shape: v
            prep.remux_is_safe = lambda p, v=copyable: v
            check(label, prep.remux_verdict(sample) is want)
    finally:
        prep.matches_target, prep.remux_is_safe = real_m, real_s
        prep.REMUX_VERDICTS = real_file

print("the cache answers for the file it was asked about")
with tempfile.TemporaryDirectory() as tmp:
    probe_calls = []
    real_safe, real_state = prep.remux_is_safe, prep.REMUX_VERDICTS
    real_shape = prep.matches_target
    prep.REMUX_VERDICTS = pathlib.Path(tmp) / "remux.json"
    sample = pathlib.Path(tmp) / "f.mp4"
    sample.write_bytes(b"a" * 100)
    try:
        # the shape check runs first and short circuits, so it has to say yes
        # for the count below to be measuring the cache and not that
        prep.matches_target = lambda p: True
        prep.remux_is_safe = lambda p: probe_calls.append(1) or True
        prep.remux_verdict(sample)
        prep.remux_verdict(sample)
        check("a second question costs no second probe", len(probe_calls) == 1,
              f"{len(probe_calls)} sonde(s)")
        # the control: a replaced file must be asked again, or the normaliser's
        # own output would inherit the verdict of the file it replaced
        sample.write_bytes(b"b" * 200)
        prep.remux_verdict(sample)
        check("a file that changed is asked again", len(probe_calls) == 2,
              f"{len(probe_calls)} sonde(s)")
        check("the cache holds one entry per file, not one per version",
              len(json.loads(prep.REMUX_VERDICTS.read_text())) == 1)
        check("a missing file is not safe",
              prep.remux_verdict(pathlib.Path(tmp) / "absent.mp4") is False)
    finally:
        prep.remux_is_safe, prep.REMUX_VERDICTS = real_safe, real_state
        prep.matches_target = real_shape

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
