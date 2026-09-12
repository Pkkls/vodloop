#!/usr/bin/env python3
"""What airs, and whether it is worth airing. Run: python3 tests/test_profile_floor.py

Measured 2026-09-12 21:17, all at the same minute: the feeder pushing a 640x360
file at 30 fps and 534 kbps, Kick's master playlist still labelling its source
rung 1920x1080 at 60, the segment behind that label decoding to 640x360, and the
monitor reporting 1920x1080 at 4781 kbps because it read the newest chunk prep
had cut rather than the one on air. Three defects, one screen that "indique
1080p60" and looks like a bitmap.
"""
import os
import pathlib
import subprocess
import sys
import tempfile

os.environ["VODLOOP_ROOT"] = tempfile.mkdtemp(prefix="profile-floor-")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import common  # noqa: E402
import prep  # noqa: E402
import quality  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def probed(csv_line):
    """matches_target with ffprobe answering csv_line."""
    real = prep.subprocess.run
    prep.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, csv_line + "\n", "")
    try:
        return prep.matches_target("unused.mkv")
    finally:
        prep.subprocess.run = real


print("the four profiles have a floor")
check("640x360 at 30 is refused", probed("h264,640,360,30/1") is False)
check("temoin: 854x480 at 30 is taken", probed("h264,854,480,30/1") is True)
check("temoin: 1280x720 at 30 is taken", probed("h264,1280,720,30/1") is True)
check("temoin: 1920x1080 at 60 is taken", probed("h264,1920,1080,60/1") is True)
check("the ceiling still holds above 1080", probed("h264,2560,1440,60/1") is False)
check("and 50 fps is still refused at 1080", probed("h264,1920,1080,50/1") is False)

print("a yes recorded before the floor does not answer for it")
import json  # noqa: E402

video = pathlib.Path(os.environ["VODLOOP_ROOT"]) / "videos" / "A_Long_VOD-SPbIFtfeBnc.mkv"
video.parent.mkdir(parents=True, exist_ok=True)
video.write_bytes(b"stubbed, the probes below never read it")
st = video.stat()
old_key = f"{video.name}:{st.st_size}:{int(st.st_mtime)}"
common.STATE.mkdir(parents=True, exist_ok=True)
prep.REMUX_VERDICTS.write_text(json.dumps({old_key: True}))

real_run, real_safe = prep.subprocess.run, prep.remux_is_safe
try:
    prep.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "h264,640,360,30/1\n", "")
    prep.remux_is_safe = lambda _p: True
    check("the 360p file that aired is refused despite its old cached yes",
          prep.remux_verdict(video) is False)
    check("and the stale entry is gone from the cache",
          old_key not in json.loads(prep.REMUX_VERDICTS.read_text()))

    prep.REMUX_VERDICTS.unlink()
    prep.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "h264,1280,720,30/1\n", "")
    check("temoin: a 720p30 file is taken", prep.remux_verdict(video) is True)
    prep.remux_is_safe = lambda _p: False
    check("temoin: and its yes, taken under the current limits, is still not re-asked",
          prep.remux_verdict(video) is True)
finally:
    prep.subprocess.run, prep.remux_is_safe = real_run, real_safe

print("the monitor reads the chunk on air")
segments = common.SEGMENTS
segments.mkdir(parents=True, exist_ok=True)
airing = segments / "00508_00045.ts"
later = segments / "00512_00001.ts"
airing.write_bytes(b"x")
later.write_bytes(b"x")
# the one cut last is the newest on disk, which is exactly what fooled it
os.utime(airing, (1_000_000, 1_000_000))
check("the oldest ready chunk, the one the feeder sends first",
      quality.on_air_chunk() == airing, str(quality.on_air_chunk()))
airing.unlink()
check("temoin: once sent and deleted, the next one takes its place",
      quality.on_air_chunk() == later, str(quality.on_air_chunk()))
later.unlink()
check("nothing ready reads as nothing", quality.on_air_chunk() is None)

print("a copied variable rate file is a copy")
base = {"vcodec": "h264", "width": 1280, "height": 720}
check("29.25 in mpegts against 29.263 in the mkv is a copy",
      quality.compare({**base, "fps": 29.25}, {**base, "fps": 29.263})["copied_video"] is True)
check("temoin: 30 against 60 is not",
      quality.compare({**base, "fps": 30.0}, {**base, "fps": 60.0})["copied_video"] is False)
check("temoin: 25 against 30 is not",
      quality.compare({**base, "fps": 25.0}, {**base, "fps": 30.0})["copied_video"] is False)
check("two unreadable rates still compare as before",
      quality.compare({**base, "fps": None}, {**base, "fps": None})["copied_video"] is True)

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
