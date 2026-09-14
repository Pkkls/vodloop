#!/usr/bin/env python3
"""Cutting long files into parts. Run: python3 tests/test_slice.py

The cut itself is the easy half. The half worth testing is everything it refuses
to do, because each refusal stands between the channel and a fault it has already
had: deleting a file that is on air, replacing one playable file with several
unplayable ones, or filling the disk with a copy it cannot finish.

Every refusal below is paired with a case that does go through, so none of them
can pass on a function that has simply stopped doing anything.
"""
import importlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(tempfile.mkdtemp(prefix="slice-"))
(root / "segments").mkdir()
(root / "state").mkdir()
library = root / "videos"
library.mkdir()

os.environ["VODLOOP_ROOT"] = str(root)
os.environ["VODLOOP_LIBRARY"] = str(library)
os.environ["VODLOOP_SLICE_SECONDS"] = "4"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import common  # noqa: E402

importlib.reload(common)
import prep  # noqa: E402

importlib.reload(prep)
import slice as slicer  # noqa: E402

importlib.reload(slicer)

# The fixtures are 160x90, so the real verdict refuses them for their shape,
# which is correct and is not what these checks are about. It is stubbed for the
# happy path and exercised on purpose further down, with its control.
prep.remux_verdict = lambda p: True

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def make(name, seconds):
    """A real h264 file, because the whole job is an ffmpeg copy."""
    path = library / name
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-t", str(seconds), "-i", f"testsrc=s=160x90:r=10",
         "-f", "lavfi", "-t", str(seconds), "-i",
         "anullsrc=channel_layout=stereo:sample_rate=44100",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "10",
         "-c:a", "aac", "-y", str(path)],
        check=True, capture_output=True)
    return path


print("what it picks up, and what it leaves alone")
long_one = make("un_long-AAAAAAAAAAA.mkv", 20)
short_one = make("un_court-BBBBBBBBBBB.mkv", 5)
no_id = make("sans_identifiant.mkv", 20)

noms = {p.name for p, _ in slicer.candidates()}
check("a file long enough is picked up", "un_long-AAAAAAAAAAA.mkv" in noms, str(noms))
check("one shorter than the ratio is left alone", "un_court-BBBBBBBBBBB.mkv" not in noms)
check("and one with no id is left alone, it could not be named",
      "sans_identifiant.mkv" not in noms)

# on air: the queue names it and its chunks are on the disk
(common.SEGMENTS / "00003_00000.ts").write_text("chunk")
(common.STATE / "queue.json").write_text(json.dumps({"items": [
    {"id": 3, "path": str(long_one), "status": "ready"}]}))
check("a file that is on air is never a candidate",
      long_one.name not in {p.name for p, _ in slicer.candidates()})
(common.SEGMENTS / "00003_00000.ts").unlink()
(common.STATE / "queue.json").write_text(json.dumps({"items": []}))
check("temoin: once its chunks are gone it is a candidate again",
      long_one.name in {p.name for p, _ in slicer.candidates()})

print("a dry run changes nothing")
avant = sorted(p.name for p in library.iterdir())
slicer.main([])
check("nothing is created, nothing is deleted",
      sorted(p.name for p in library.iterdir()) == avant)

print("the cut itself")
slicer.main(["--apply"])
parts = sorted(p for p in library.iterdir() if prep.PART.search(p.name))
check("the long file became parts", len(parts) >= 4, str(len(parts)))
check("and the original is gone", not long_one.exists())
check("the parts are named so the draw groups them",
      {prep.video_of(p)[0] for p in parts} == {"AAAAAAAAAAA"},
      str({prep.video_of(p)[0] for p in parts}))
check("and numbered in order from one",
      [prep.video_of(p)[1] for p in parts] == list(range(1, len(parts) + 1)),
      str([prep.video_of(p)[1] for p in parts]))
check("the count in the name matches how many there are",
      all(f"of{len(parts):02d}" in p.name for p in parts), str(parts[0].name))

total = sum(slicer.duration(p) for p in parts)
check("the parts hold the whole video, not most of it",
      total >= 20 * 0.98, f"{total:.1f}s sur 20s")

# even parts, not a full slice and a stub: the stub is a rotation entry nobody
# wants, and the control is that they are all within a second of each other
durees = [slicer.duration(p) for p in parts]
check("the parts are even, with no stub at the end",
      max(durees) - min(durees) < 1.5, str([round(d, 1) for d in durees]))

check("the short file was never touched", short_one.exists())
check("nor the one with no id", no_id.exists())

print("a cut that produces something unplayable is undone")
encore = make("encore_long-DDDDDDDDDDD.mkv", 20)
prep.remux_verdict = lambda p: False
try:
    fait = slicer.cut(encore, 20.0, True)
    check("parts prep could not copy are refused", fait == 0, str(fait))
    check("the original is kept rather than replaced by them", encore.exists())
    check("and no part is left behind in the library",
          not [p for p in library.iterdir() if "DDDDDDDDDDD" in p.name
               and prep.PART.search(p.name)])
finally:
    prep.remux_verdict = lambda p: True
# the control: the same file, same cut, with a verdict that says yes
fait = slicer.cut(encore, 20.0, True)
check("temoin: the same cut goes through when the parts are copyable",
      fait >= 4, str(fait))

print("what it does when it cannot finish")
autre = make("autre_long-CCCCCCCCCCC.mkv", 20)
vrai_libre = slicer.shutil.disk_usage
slicer.shutil.disk_usage = lambda p: type("d", (), {"free": 1})()
try:
    fait = slicer.cut(autre, 20.0, True)
    check("a disk that cannot hold the copy stops it before it starts", fait == 0)
    check("and the original is still there", autre.exists())
finally:
    slicer.shutil.disk_usage = vrai_libre

# the control for the refusal above: with room, the same file does get cut
fait = slicer.cut(autre, 20.0, True)
check("temoin: with room the same file is cut", fait >= 4, str(fait))
check("and only then is the original gone", not autre.exists())

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
