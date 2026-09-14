#!/usr/bin/env python3
"""Library refill. Run: python3 tests/test_prep_refill.py

A 24/7 channel that runs out of queue does not go offline, it sits on a 20 second
standby clip, which passes every check except looking at it. This is the loop
that stops that.

Two things it has to get right. The library is queued where it lives, because
copying it into incoming/ first doubled the disk for nothing. And playing a
library file must not consume it: prep deletes a source once it has encoded it,
which is correct for a file handed over for one play and would delete the whole
library on its first pass through the rotation.
"""
import importlib
import os
import pathlib
import sys
import tempfile

root = pathlib.Path(tempfile.mkdtemp(prefix="prep-refill-"))
(root / "segments").mkdir()
(root / "incoming").mkdir()
(root / "state").mkdir()
library = root / "videos"
library.mkdir()
for name in ("a.mp4", "b.mp4"):
    (library / name).write_text("video")
(library / "notes.txt").write_text("pas une video")

os.environ["VODLOOP_ROOT"] = str(root)
os.environ["VODLOOP_LIBRARY"] = str(library)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import common  # noqa: E402

importlib.reload(common)
import prep  # noqa: E402

importlib.reload(prep)

# The fixtures here are empty files, not videos, so the real probe answers "not
# copyable" for all of them and the refill queues nothing. What a real ffmpeg
# says about a real file is measured in test_normalise.py and
# test_prep_remux_safety.py; here it only has to stay out of the way of the
# question being asked, which is what the refill does with what it is given.
prep.remux_verdict = lambda p: True

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
    {"id": 1, "status": "played", "path": str(library / "a.mp4"), "url": "a.mp4"},
    {"id": 2, "status": "played", "path": str(library / "b.mp4"), "url": "b.mp4"},
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
check("les items pointent sur la bibliotheque, pas sur une copie",
      sorted(i["path"] for i in queue["items"])
      == sorted(str(library / f) for f in ("a.mp4", "b.mp4")),
      [i["path"] for i in queue["items"]])
check("rien n'est copie dans incoming", list(inc.iterdir()) == [], list(inc.iterdir()))
check("les anciennes entrees jouees sont retirees", len(queue["items"]) == 2, queue["items"])
check("et les nouvelles attendent", all(i["status"] == "pending" for i in queue["items"]))
check("un fichier qui n'est pas une video est ignore",
      all("notes" not in i["path"] for i in queue["items"]))
check("les ids restent uniques", queue["seq"] == 4 and {i["id"] for i in queue["items"]} == {3, 4},
      queue["seq"])

# --- the guard that stands between prep and the whole library -------------
check("un fichier de la bibliotheque n'est pas consommable",
      not prep.consumable(str(library / "a.mp4")))
check("un fichier depose dans incoming l'est",
      prep.consumable(str(inc / "depose.mp4")))
check("temoin: le repertoire seul decide, pas le nom",
      prep.consumable(str(inc / "a.mp4")) and not prep.consumable(str(library / "a.mp4")))

# --- un seul passage par video, et le filet dessous ----------------------
# MAX_PLAYS vaut 1: une video deja diffusee sort du tirage tant qu'il reste
# quelque chose d'inedit. Ce qui compte autant, c'est ce qui arrive quand il ne
# reste rien: le tirage doit rendre la bibliotheque entiere plutot que rien du
# tout, sinon la chaine se repose sur le clip d'attente au lieu de se repeter.
histoire = {}
prep.load_history = lambda: dict(histoire)
prep.save_history = lambda h: histoire.update(h)

check("le reglage est bien d'un seul passage", common.MAX_PLAYS == 1, common.MAX_PLAYS)

(library / "c.mp4").write_text("video")
histoire[str(library / "a.mp4")] = {"at": 0.0, "plays": 1}
tirage = {"seq": 0, "items": []}
prep.refill_from_library(tirage)
tires = sorted(pathlib.Path(i["path"]).name for i in tirage["items"])
check("une video deja diffusee reste en dehors du tirage",
      "a.mp4" not in tires, tires)
check("temoin: les inedites, elles, y sont", tires == ["b.mp4", "c.mp4"], tires)

for nom in ("a.mp4", "b.mp4", "c.mp4"):
    histoire[str(library / nom)] = {"at": 0.0, "plays": 1}
tirage = {"seq": 0, "items": []}
combien = prep.refill_from_library(tirage)
check("tout ayant ete diffuse, le repli rend quand meme la bibliotheque",
      combien == 3, combien)

(library / "c.mp4").unlink()
histoire.clear()

# --- unset means the old behaviour, exactly ------------------------------
prep.LIBRARY = None
check("sans VODLOOP_LIBRARY, la fonction ne fait rien",
      prep.refill_from_library({"seq": 0, "items": []}) == 0)

print()
print(f"{passed}/{passed + failed} passent")
sys.exit(0 if failed == 0 else 1)
