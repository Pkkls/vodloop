#!/usr/bin/env python3
"""Rotation memory. Run: python3 tests/test_prep_history.py

Two faults, one function. Nothing recorded what had already been on air, so
every refill was a fresh shuffle of the whole library and the last video of one
pass could be the first of the next. And the refill dropped every queue entry
naming a library file, including the one being played: reap() then deleted the
chunks that entry owned, the video was cut off mid-play, and the file went
straight back in the draw. That is how the same video came round twice.

So the questions here are: does a file that has just been on air stand aside,
does one that has not come back, is the queue ever left empty by the rule, and
does the entry that is currently feeding the channel survive a refill.

Each check has its control. "Excluded after a day" would pass on a function that
excluded everything, so the same library is asked again with only the dates
moved and has to answer differently.
"""
import importlib
import json
import os
import pathlib
import sys
import tempfile
import time

root = pathlib.Path(tempfile.mkdtemp(prefix="prep-history-"))
(root / "segments").mkdir()
(root / "incoming").mkdir()
(root / "state").mkdir()
library = root / "videos"
library.mkdir()
NAMES = ["a.mp4", "b.mp4", "c.mp4"]
for name in NAMES:
    (library / name).write_text("video")

os.environ["VODLOOP_ROOT"] = str(root)
os.environ["VODLOOP_LIBRARY"] = str(library)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import common  # noqa: E402

importlib.reload(common)
import prep  # noqa: E402

importlib.reload(prep)

# The fixtures are empty files, so the real probe answers "not copyable" for all
# of them and the refill would queue nothing. What a real ffmpeg says about a
# real file is measured in test_prep_remux_safety.py; here it only has to stay
# out of the way of the question being asked.
prep.remux_verdict = lambda p: True

DAY = 86400
NOW = time.time()

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


def set_history(ages_in_days):
    prep.HISTORY.write_text(json.dumps(
        {str(library / name): NOW - days * DAY for name, days in ages_in_days.items()}))


def refill(items=None):
    """Run one refill on an empty queue and return the names it put in it."""
    queue = {"seq": 0, "items": list(items or [])}
    count = prep.refill_from_library(queue)
    queued = [pathlib.Path(i["path"]).name for i in queue["items"]
              if i["status"] == "pending"]
    assert count == len(queued), (count, queued)
    return queue, queued


# --- a file on air this week stands aside, one from last week comes back ---
set_history({"a.mp4": 1, "b.mp4": 8, "c.mp4": 8})
queue, queued = refill()
check("un fichier passe il y a 1 jour est ecarte", "a.mp4" not in queued, queued)
check("ceux passes il y a 8 jours reviennent",
      sorted(queued) == ["b.mp4", "c.mp4"], queued)

# the control: only the dates move, and the same library answers differently.
# Without this the check above would pass on a function that excluded a.mp4 for
# any other reason, its name included.
set_history({"a.mp4": 8, "b.mp4": 8, "c.mp4": 8})
queue, queued = refill()
check("temoin: passe il y a 8 jours, le meme fichier revient",
      sorted(queued) == sorted(NAMES), queued)

# --- the window is a preference, not a lock -------------------------------
# Every file inside the week and none outside it. A rule that emptied the queue
# here would be a grey screen, so the fallback has to fire and it has to order
# by what has waited longest.
set_history({"a.mp4": 1, "b.mp4": 2, "c.mp4": 3})
queue, queued = refill()
check("tout dans la fenetre: la file n'est pas vide",
      sorted(queued) == sorted(NAMES), queued)
check("et le plus ancien passe en premier", queued == ["c.mp4", "b.mp4", "a.mp4"],
      queued)

# the control: reverse the ages and the order reverses with them, so the check
# above measured the dates and not the alphabet
set_history({"a.mp4": 3, "b.mp4": 2, "c.mp4": 1})
queue, queued = refill()
check("temoin: dates inversees, ordre inverse",
      queued == ["a.mp4", "b.mp4", "c.mp4"], queued)

# --- what refill queues is what it writes down ----------------------------
prep.HISTORY.unlink(missing_ok=True)
queue, queued = refill()
written = json.loads(prep.HISTORY.read_text())
check("un historique absent laisse tout passer", sorted(queued) == sorted(NAMES), queued)
check("et refill estampille ce qu'il met en file",
      sorted(pathlib.Path(p).name for p in written) == sorted(NAMES), written)
check("avec une date d'aujourd'hui",
      all(abs(v - time.time()) < 300 for v in written.values()), written)

# a file the janitor deleted keeps no place in the rotation
prep.HISTORY.write_text(json.dumps(
    {str(library / "a.mp4"): NOW, str(library / "disparu.mp4"): NOW}))
queue, queued = refill()
check("une entree pour un fichier disparu est oubliee",
      "disparu.mp4" not in json.loads(prep.HISTORY.read_text()),
      json.loads(prep.HISTORY.read_text()))

# --- the entry feeding the channel survives the refill --------------------
# This is the doubling itself. The item is "ready" and its chunks are on the
# disk, which is what "on air" looks like from here. Dropped, reap() deletes
# those chunks because nothing owns them any more, and the file is back in the
# draw at once.
chunk = common.SEGMENTS / "00007_00000.ts"
chunk.write_text("chunk")
playing = {"id": 7, "status": "ready", "url": "a.mp4",
           "path": str(library / "a.mp4"), "votes": [], "added_at": 1.0}
set_history({"a.mp4": 1, "b.mp4": 1, "c.mp4": 1})
queue, queued = refill([playing])
check("l'item dont les chunks jouent encore reste en file",
      any(i["id"] == 7 and i["status"] == "ready" for i in queue["items"]),
      [(i["id"], i["status"]) for i in queue["items"]])
check("et son fichier ne repart pas dans le tirage tout de suite",
      "a.mp4" not in queued, queued)

# the control: the same entry with no chunks left is what "finished" looks
# like, and that one does get cleared out and does go back in the draw
chunk.unlink()
played = dict(playing, status="played")
queue, queued = refill([played])
check("temoin: sans chunks, l'ancienne entree est retiree",
      all(i["id"] != 7 for i in queue["items"]),
      [(i["id"], i["status"]) for i in queue["items"]])
check("temoin: et son fichier revient dans le tirage", "a.mp4" in queued, queued)

print()
print(f"{passed}/{passed + failed} passent")
sys.exit(0 if failed == 0 else 1)
