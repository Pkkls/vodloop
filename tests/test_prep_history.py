#!/usr/bin/env python3
"""Rotation memory. Run: python3 tests/test_prep_history.py

Three faults, one function. Nothing recorded what had already been on air, so
every refill was a fresh shuffle of the whole library and the last video of one
pass could be the first of the next. The refill dropped every queue entry naming
a library file, including the one being played: reap() then deleted the chunks
that entry owned, the video was cut off mid-play, and the file went straight back
in the draw. And a file stayed in the rotation for ever, so a library that stops
growing is a library the viewer has seen end to end, twice, and then again.

So the questions here are: does a file that has just been on air stand aside, is
the queue ever left empty by that rule, does the entry feeding the channel
survive a refill, and is a file that has had its two plays actually gone.

Each check has its control. "Excluded after a day" would pass on a function that
excluded everything, so the same library is asked again with only the dates
moved and has to answer differently. "Deleted after two plays" would pass on a
function that deleted on the first, so one play is asked for too.
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
NAMES = ["a.mp4", "b.mp4", "c.mp4", "d.mp4", "e.mp4"]


def rebuild_library():
    for old in library.iterdir():
        old.unlink()
    for name in NAMES:
        (library / name).write_text("video")


rebuild_library()

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
# The fixtures are empty files, so the real probe reads no duration from them
# and every one would look like nothing left to play. Half an hour each, against
# a floor of an hour, makes five files two and a half hours of runway and puts
# the floor where it can be watched biting.
prep.duration_of = lambda p: 1800.0
common.MIN_RUNWAY_SECONDS = 3600

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


def set_history(entries):
    """{name: days_since_last_queued} or {name: (days, plays)}."""
    out = {}
    for name, value in entries.items():
        days, plays = value if isinstance(value, tuple) else (value, 0)
        out[str(library / name)] = {"at": NOW - days * DAY, "plays": plays}
    prep.HISTORY.write_text(json.dumps(out))


def refill(items=None):
    """Run one refill on an empty queue and return the names it put in it."""
    queue = {"seq": 0, "items": list(items or [])}
    count = prep.refill_from_library(queue)
    queued = [pathlib.Path(i["path"]).name for i in queue["items"]
              if i["status"] == "pending"]
    assert count == len(queued), (count, queued)
    return queue, queued


# --- a file on air this week stands aside, one from last week comes back ---
set_history({"a.mp4": 1, "b.mp4": 8, "c.mp4": 8, "d.mp4": 8, "e.mp4": 8})
queue, queued = refill()
check("un fichier passe il y a 1 jour est ecarte", "a.mp4" not in queued, queued)
check("ceux passes il y a 8 jours reviennent",
      sorted(queued) == ["b.mp4", "c.mp4", "d.mp4", "e.mp4"], queued)

# the control: only the dates move, and the same library answers differently.
# Without this the check above would pass on a function that excluded a.mp4 for
# any other reason, its name included.
set_history({name: 8 for name in NAMES})
queue, queued = refill()
check("temoin: passe il y a 8 jours, le meme fichier revient",
      sorted(queued) == sorted(NAMES), queued)

# --- the window is a preference, not a lock -------------------------------
# Every file inside the week and none outside it. A rule that emptied the queue
# here would be a grey screen, so the whole pool has to come back anyway.
set_history({"a.mp4": 1, "b.mp4": 2, "c.mp4": 3, "d.mp4": 4, "e.mp4": 5})
queue, queued = refill()
check("tout dans la fenetre: la file n'est pas vide",
      sorted(queued) == sorted(NAMES), queued)
check("et personne n'y est deux fois", len(queued) == len(set(queued)), queued)

# --- the order is drawn, not computed -------------------------------------
# Same history, same library, ten passes: an order that is decided by anything
# but chance gives the same answer every time.
orders = set()
for _ in range(10):
    orders.add(tuple(refill()[1]))
check("l'ordre change d'une passe a l'autre", len(orders) > 1, f"{len(orders)} ordres")

# the control: neutralise the shuffle and the same code has to fall back to the
# listing order, which proves the check above measured the shuffle and not some
# other reordering
real_shuffle, prep.random.shuffle = prep.random.shuffle, lambda seq: None
try:
    queue, queued = refill()
    check("temoin: sans le tirage, c'est l'ordre du repertoire",
          queued == sorted(NAMES), queued)
finally:
    prep.random.shuffle = real_shuffle

# --- what refill queues is what it writes down ----------------------------
prep.HISTORY.unlink(missing_ok=True)
queue, queued = refill()
written = json.loads(prep.HISTORY.read_text())
check("un historique absent laisse tout passer", sorted(queued) == sorted(NAMES), queued)
check("et refill estampille ce qu'il met en file",
      sorted(pathlib.Path(p).name for p in written) == sorted(NAMES), written)
check("avec une date d'aujourd'hui",
      all(abs(e["at"] - time.time()) < 300 for e in written.values()), written)

# an entry written before the play count existed is a date, not a record
prep.HISTORY.write_text(json.dumps({str(library / "a.mp4"): NOW - 8 * DAY}))
history = prep.load_history()
check("un ancien historique plat est relu sans casser",
      prep.played_count(history, library / "a.mp4") == 0
      and abs(prep.last_queued(history, library / "a.mp4") - (NOW - 8 * DAY)) < 1,
      history)

# a file the janitor deleted keeps no place in the rotation
set_history({"a.mp4": 0})
entries = json.loads(prep.HISTORY.read_text())
entries[str(library / "disparu.mp4")] = {"at": NOW, "plays": 1}
prep.HISTORY.write_text(json.dumps(entries))
queue, queued = refill()
check("une entree pour un fichier disparu est oubliee",
      "disparu.mp4" not in json.loads(prep.HISTORY.read_text()),
      list(json.loads(prep.HISTORY.read_text())))

# --- the entry feeding the channel survives the refill --------------------
# This is the doubling itself. The item is "ready" and its chunks are on the
# disk, which is what "on air" looks like from here. Dropped, reap() deletes
# those chunks because nothing owns them any more, and the file is back in the
# draw at once.
chunk = common.SEGMENTS / "00007_00000.ts"
chunk.write_text("chunk")
playing = {"id": 7, "status": "ready", "url": "a.mp4",
           "path": str(library / "a.mp4"), "votes": [], "added_at": 1.0}
set_history({name: 1 for name in NAMES})
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

# --- it plays, it goes -----------------------------------------------------
rebuild_library()
prep.HISTORY.unlink(missing_ok=True)
for chunk in common.SEGMENTS.glob("*.ts"):
    chunk.unlink()
check("cinq fichiers font 150 min de reserve",
      prep.runway_seconds() == 5 * 1800, prep.runway_seconds())

# Playing a file used to retire it, and that is what made the channel fragile:
# a rerun channel that eats its own library depends on the supply never
# stopping. On 2026-09-08 YouTube closed its player API at 08:41, every fetch
# failed for the rest of the day, and the library was deleted one video at a
# time until a single unplayable file was left and the channel sat on the
# standby clip. Deletion now belongs to the janitor and to the disk pressure it
# answers to, which is the only real limit.
prep.record_play(library / "a.mp4")
check("un passage ne supprime rien", (library / "a.mp4").is_file())
check("mais il est compte",
      prep.load_history()[str(library / "a.mp4")]["plays"] == 1,
      prep.load_history().get(str(library / "a.mp4")))
check("et la reserve ne bouge pas: le fichier est toujours diffusable",
      prep.runway_seconds() == 5 * 1800, prep.runway_seconds())

prep.record_play(library / "a.mp4")
prep.record_play(library / "a.mp4")
check("rejoue trois fois, il est toujours la",
      (library / "a.mp4").is_file()
      and prep.load_history()[str(library / "a.mp4")]["plays"] == 3)
check("et il compte encore une fois, pas trois",
      prep.runway_seconds() == 5 * 1800, prep.runway_seconds())

# the control: the counter is what moves, so the checks above are measuring the
# play and not a function that simply does nothing at all
check("temoin: un autre fichier n'a pas ete compte",
      prep.load_history().get(str(library / "b.mp4"), {}).get("plays", 0) == 0)
check("temoin: la bibliotheque est intacte", prep.library_size() == 5,
      sorted(q.name for q in library.iterdir()))

# a file handed over for a single play is not the library's business either
dropped = common.INCOMING / "depose.mp4"
dropped.write_text("video")
prep.record_play(dropped)
check("un fichier depose dans incoming n'entre pas dans l'historique",
      dropped.is_file() and str(dropped) not in prep.load_history())

# --- everything can play, and playing everything empties nothing ------------
for name in ("b.mp4", "c.mp4", "d.mp4", "e.mp4"):
    prep.record_play(library / name)
check("toute la bibliotheque passee une fois, rien n'a disparu",
      prep.library_size() == 5, sorted(q.name for q in library.iterdir()))
check("et la reserve tient toujours ses 150 min",
      prep.runway_seconds() == 5 * 1800, prep.runway_seconds())
# This is the property the channel lives by now: a supply outage costs repeats,
# never silence. Before this change the same six calls left an empty library.
check("donc une coupure d'approvisionnement coute des repetitions, pas du vide",
      prep.runway_seconds() >= common.MIN_RUNWAY_SECONDS,
      f"{prep.runway_seconds()} >= {common.MIN_RUNWAY_SECONDS}")

print()
print(f"{passed}/{passed + failed} passent")
sys.exit(0 if failed == 0 else 1)
