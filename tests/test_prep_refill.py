#!/usr/bin/env python3
"""Library refill. Run: python3 tests/test_prep_refill.py

A 24/7 channel that runs out of queue does not go offline, it sits on a 20 second
standby clip, which passes every check except looking at it. This is the loop
that stops that, and the thing it has to get right is that relinking a file is
not enough: a played entry naming the same path makes take_dropped_files skip it.
"""
import importlib
import os
import pathlib
import sys
import tempfile
import time

root = pathlib.Path(tempfile.mkdtemp(prefix="prep-refill-"))
(root / "segments").mkdir()
(root / "incoming").mkdir()
(root / "state").mkdir()
library = root / "videos"
library.mkdir()
for name in ("a.mp4", "b.mp4"):
    (library / name).write_text("video")
    # a library file is not a file being copied right now: hard links keep the
    # inode's mtime, so prep's settle delay must not hold them back
    old = time.time() - 3600
    os.utime(library / name, (old, old))
(library / "notes.txt").write_text("pas une video")

os.environ["VODLOOP_ROOT"] = str(root)
os.environ["VODLOOP_LIBRARY"] = str(library)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import common  # noqa: E402

importlib.reload(common)
import prep  # noqa: E402

importlib.reload(prep)

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


inc = common.INCOMING
played = {"seq": 2, "items": [
    {"id": 1, "status": "played", "path": str(inc / "a.mp4"), "url": "a.mp4"},
    {"id": 2, "status": "played", "path": str(inc / "b.mp4"), "url": "b.mp4"},
]}

# --- it does not fire while there is still work ---------------------------
busy = {"seq": 3, "items": played["items"] + [{"id": 3, "status": "pending", "url": "x"}]}
check("rien a faire tant qu'un item attend", prep.refill_from_library(busy) == 0)
busy2 = {"seq": 3, "items": played["items"] + [{"id": 3, "status": "preparing", "url": "x"}]}
check("rien a faire tant qu'un item s'encode", prep.refill_from_library(busy2) == 0)

# a big backlog on disk is also a reason to wait
for n in range(10):
    (common.SEGMENTS / f"00002_{n:05d}.ts").write_text("chunk")
check("rien a faire tant que la reserve est grande",
      prep.refill_from_library(dict(played, items=list(played["items"]))) == 0,
      prep.seconds_on_disk())
for chunk in common.SEGMENTS.glob("*.ts"):
    chunk.unlink()

# --- it fires when the queue is done --------------------------------------
queue = {"seq": 2, "items": list(played["items"])}
n = prep.refill_from_library(queue)
check("la bibliotheque revient en file", n == 2, n)
check("les fichiers sont dans incoming",
      sorted(p.name for p in inc.glob("*.mp4")) == ["a.mp4", "b.mp4"])
check("ce sont des liens durs, pas des copies",
      (inc / "a.mp4").stat().st_ino == (library / "a.mp4").stat().st_ino)
check("les anciennes entrees jouees sont retirees", queue["items"] == [], queue["items"])
check("un fichier qui n'est pas une video est ignore", not (inc / "notes.txt").exists())

# under the unit's sandbox incoming/ is its own bind mount, so linking out of
# the library is cross-device and only a copy gets the file in
for f in inc.glob("*.mp4"):
    f.unlink()
real_link, os.link = os.link, lambda *a: (_ for _ in ()).throw(OSError(18, "cross-device"))
try:
    prep.refill_from_library({"seq": 2, "items": []})
finally:
    os.link = real_link
check("un lien impossible tombe sur une copie",
      sorted(p.name for p in inc.glob("*.mp4")) == ["a.mp4", "b.mp4"])
for f in inc.glob("*.mp4"):
    f.unlink()
prep.refill_from_library({"seq": 2, "items": []})

# the point of the whole thing: take_dropped_files must now see them
prep.take_dropped_files(queue)
check("prep les reprend vraiment", len(queue["items"]) == 2, queue["items"])
check("et en attente", all(i["status"] == "pending" for i in queue["items"]))

# --- the control: without removing the old entries, prep skips them --------
for f in inc.glob("*.mp4"):
    f.unlink()
stale = {"seq": 2, "items": list(played["items"])}
for src in library.glob("*.mp4"):
    os.link(src, inc / src.name)
prep.take_dropped_files(stale)
check("temoin: relier sans nettoyer la file ne relance rien",
      len(stale["items"]) == 2 and all(i["status"] == "played" for i in stale["items"]),
      stale["items"])

# --- unset means the old behaviour, exactly ------------------------------
prep.LIBRARY = None
check("sans VODLOOP_LIBRARY, la fonction ne fait rien",
      prep.refill_from_library({"seq": 0, "items": []}) == 0)

print()
print(f"{passed}/{passed + failed} passent")
sys.exit(0 if failed == 0 else 1)
