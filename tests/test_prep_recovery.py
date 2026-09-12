#!/usr/bin/env python3
"""Orphan recovery. Run: python3 tests/test_prep_recovery.py

An item left in "preparing" by an interrupted run is invisible to the loop,
which only looks at "pending", so the queue stops moving behind it. This checks
that startup puts it back, and that it keeps what that run had already finished.

The keeping is the part with a channel behind it. prep writes chunks into
segments/ while the feeder is already eating them and only looks at
AHEAD_LIMIT_SECONDS between items, so the item in flight can be holding hours of
runway. Throwing all of it away, which is what this did until 2026-09-12, made
every restart of the unit a chance to empty the buffer: the hardening pass
restarts it, medic restarts it, a deploy restarts it.
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import common  # noqa: E402

root = pathlib.Path(tempfile.mkdtemp(prefix="prep-recovery-"))
common.SEGMENTS = root / "segments"
common.SEGMENTS.mkdir()
common.STATE = root / "state"
common.STATE.mkdir()

import prep  # noqa: E402

prep.common.SEGMENTS = common.SEGMENTS
prep.common.STATE = common.STATE

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


queue = {"seq": 5, "items": [
    {"id": 1, "status": "played"},
    {"id": 2, "status": "error", "error": "no output"},
    {"id": 3, "status": "preparing", "error": "vieux message"},
    {"id": 4, "status": "ready"},
    {"id": 5, "status": "pending"},
]}
# four chunks written, so three of them were closed before the fourth existed
for n in range(4):
    (common.SEGMENTS / f"{3:05d}_{n:05d}.ts").write_text("chunk")
for n in range(2):
    (common.SEGMENTS / f"{4:05d}_{n:05d}.ts").write_text("chunk")
listing = common.STATE / "list_00003.txt"
listing.write_text("00003_00000.ts\n")

n = prep.recover_orphans(queue)
by_id = {i["id"]: i for i in queue["items"]}

check("l'item interrompu qui a produit quelque chose passe a pret",
      by_id[3]["status"] == "ready", by_id[3]["status"])
check("son ancien message d'erreur est efface", "error" not in by_id[3], by_id[3])
check("le compte est juste", n == 1, n)
check("un item joue n'est pas touche", by_id[1]["status"] == "played")
check("un item en erreur n'est pas touche", by_id[2]["status"] == "error")
check("un item pret n'est pas touche", by_id[4]["status"] == "ready")
check("un item en attente n'est pas touche", by_id[5]["status"] == "pending")

left = sorted(p.name for p in common.SEGMENTS.glob("*.ts"))
check("seul le dernier chunk, le seul qui puisse etre a moitie ecrit, part",
      left == ["00003_00000.ts", "00003_00001.ts", "00003_00002.ts",
               "00004_00000.ts", "00004_00001.ts"], str(left))
check("la liste du muxeur de la course interrompue est nettoyee",
      not listing.exists())

# the control: with nothing stranded it is a no-op and reports nothing
check("sans orphelin, rien n'est fait", prep.recover_orphans(queue) == 0)

# An item interrupted before it closed a single chunk has nothing to show for
# itself. It goes back in the pile exactly as before, and nothing of it is left
# to be reaped as air time it never had.
alone = {"seq": 9, "items": [{"id": 9, "status": "preparing"}]}
(common.SEGMENTS / "00009_00000.ts").write_text("moitie")
prep.recover_orphans(alone)
check("un item interrompu avant le premier chunk complet revient en attente",
      alone["items"][0]["status"] == "pending", alone["items"][0]["status"])
check("et il ne laisse rien derriere lui",
      not list(common.SEGMENTS.glob("00009_*.ts")))

# What the whole change is for: the runway in front of the interrupted item is
# what keeps the channel on a picture, and it is worth more than one re-cut.
big = {"seq": 20, "items": [{"id": 20, "status": "preparing"}]}
for n in range(25):  # 25 chunks of 300s: just over two hours of buffer
    (common.SEGMENTS / f"{20:05d}_{n:05d}.ts").write_text("chunk")
prep.recover_orphans(big)
kept = list(common.SEGMENTS.glob("00020_*.ts"))
check("deux heures de tampon survivent a un redemarrage de prep",
      len(kept) * common.CHUNK_SECONDS >= 2 * 3600,
      f"{len(kept)} chunk(s)")

print()
print(f"{passed}/{passed + failed} passent")
sys.exit(0 if failed == 0 else 1)
