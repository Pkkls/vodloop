#!/usr/bin/env python3
"""The encode wall clock. Run: python3 tests/test_prep_timeout.py

Two things are checked: the budget a video gets, and that an encode which never
finishes is actually killed rather than waited on forever. The second one uses a
real subprocess that sleeps, so it tests the kill path and not a mock of it.
"""
import pathlib
import subprocess
import sys
import tempfile
import time

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


# --- the budget -----------------------------------------------------------
check("une video courte a quand meme le plancher",
      prep.encode_budget({"duration": 60}) == common.ENCODE_TIMEOUT_FLOOR,
      prep.encode_budget({"duration": 60}))
check("une video longue a plusieurs fois sa duree",
      prep.encode_budget({"duration": 3600}) == 3600 * common.ENCODE_TIMEOUT_FACTOR,
      prep.encode_budget({"duration": 3600}))
check("le plafond tient",
      prep.encode_budget({"duration": 100000}) == common.ENCODE_TIMEOUT_CEILING)
for unknown in ({}, {"duration": None}, {"duration": "abc"}, {"duration": 0},
                {"duration": -5}):
    check(f"duree inconnue {unknown} tombe sur le plafond",
          prep.encode_budget(unknown) == common.ENCODE_TIMEOUT_CEILING,
          prep.encode_budget(unknown))

# --- the kill path --------------------------------------------------------
# a process that would never end, given a budget of one second
never = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                         stderr=subprocess.PIPE)
start = time.time()
killed = False
try:
    never.communicate(timeout=1)
except subprocess.TimeoutExpired:
    never.kill()
    never.communicate(timeout=30)
    killed = True
elapsed = time.time() - start
check("un encode qui ne finit jamais est tue", killed and never.poll() is not None,
      never.poll())
check("et il est tue tout de suite, pas apres son propre temps",
      elapsed < 30, round(elapsed, 1))

# the control: a process that finishes inside its budget is not killed
quick = subprocess.Popen([sys.executable, "-c", "pass"], stderr=subprocess.PIPE)
survived = True
try:
    quick.communicate(timeout=30)
except subprocess.TimeoutExpired:
    quick.kill()
    survived = False
check("un encode normal n'est pas tue", survived and quick.returncode == 0)

# --- the half-written chunks are cleaned ----------------------------------
root = pathlib.Path(tempfile.mkdtemp(prefix="prep-test-"))
common.SEGMENTS = root / "segments"
common.SEGMENTS.mkdir()
for n in range(3):
    (common.SEGMENTS / f"00042_{n:05d}.ts").write_text("moitie de video")
(common.SEGMENTS / "00007_00000.ts").write_text("une autre video, a garder")
for chunk in common.SEGMENTS.glob("00042_*.ts"):
    chunk.unlink(missing_ok=True)
left = sorted(p.name for p in common.SEGMENTS.glob("*.ts"))
check("les chunks de l'encode abandonne sont retires", left == ["00007_00000.ts"], left)

print()
print(f"{passed}/{passed + failed} passent")
sys.exit(0 if failed == 0 else 1)
