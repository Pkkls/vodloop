#!/usr/bin/env python3
"""Chunks eaten mid-encode. Run: python3 tests/test_prep_consumed.py

The feeder deletes each chunk as soon as it has played it, so on a dry queue a
whole video can be gone from disk before ffmpeg returns. Judging the encode by
what is left on disk then reports a perfectly good video as a failure and exiles
the file to incoming/failed/, which is how the channel ends up on the standby
clip with nothing left to play.
"""
import os
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(tempfile.mkdtemp(prefix="prep-consumed-"))
for name in ("segments", "incoming", "state"):
    (root / name).mkdir()
os.environ["VODLOOP_ROOT"] = str(root)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import common  # noqa: E402
import prep  # noqa: E402

passed = 0
failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"ok   {label}")
    else:
        failed += 1
        print(f"RATE {label} {detail}")


class FakeEncoder:
    """ffmpeg as far as prepare() can tell: writes the segment list the muxer
    would write, then lets the feeder take the chunk before returning."""

    segments = 1
    consume = True

    def __init__(self, args, **kwargs):
        # tolerated missing on purpose, so this test can be pointed at a version
        # that does not ask the muxer for a list and be seen to fail
        i = args.index("-segment_list") if "-segment_list" in args else -1
        listing = pathlib.Path(args[i + 1]) if i > 0 else None
        names = []
        for n in range(self.segments):
            chunk = pathlib.Path(args[-1] % n)
            chunk.write_text("chunk")
            names.append(chunk.name)
            if self.consume:
                chunk.unlink()  # the feeder played it and moved on
        if listing is not None:
            listing.write_text("".join(f"{n}\n" for n in names))
        self.returncode = 0

    def communicate(self, timeout=None):
        return (b"", b"")


def run(item):
    # This is about how a finished encode is judged, so the source is declared
    # not already in target shape. Without it the probe inside matches_target
    # goes through subprocess.run, which reaches the same patched Popen.
    real, prep.subprocess.Popen = prep.subprocess.Popen, FakeEncoder
    real_match, prep.matches_target = prep.matches_target, lambda path: False
    try:
        return prep.prepare(item)
    finally:
        prep.subprocess.Popen = real
        prep.matches_target = real_match


item = {"id": 8, "path": str(common.INCOMING / "v.mp4"), "url": "v.mp4",
        "title": "V", "by_name": "", "duration": 128}
ok = run(item)
check("un encode dont les chunks sont deja joues reste un succes", ok, item.get("error"))
check("le nombre de chunks vient du muxeur", item.get("chunks") == 1, item.get("chunks"))
check("rien ne reste dans incoming/failed", not (common.INCOMING / "failed").exists())

FakeEncoder.segments = 3
item3 = dict(item, id=9)
run(item3)
check("plusieurs chunks sont comptes", item3.get("chunks") == 3, item3.get("chunks"))

# the control: a real failure still has to be one
FakeEncoder.segments = 0
item0 = dict(item, id=10)
check("temoin: zero chunk produit reste un echec", not run(item0))
check("temoin: l'echec est explique", bool(item0.get("error")), item0)

# the list file is scratch, it must not pile up in state/
check("aucune liste laissee derriere",
      not list(common.STATE.glob("list_*.txt")),
      list(common.STATE.glob("list_*.txt")))

print()
print(f"{passed}/{passed + failed} passent")
sys.exit(0 if failed == 0 else 1)
