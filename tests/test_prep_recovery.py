#!/usr/bin/env python3
"""Orphan recovery. Run: python3 tests/test_prep_recovery.py

An item left in "preparing" by an interrupted run is invisible to the loop,
which only looks at "pending", so the queue stops moving behind it. This checks
that startup puts it back, takes its half-written chunks with it, and leaves
every other state alone.
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import common  # noqa: E402

root = pathlib.Path(tempfile.mkdtemp(prefix="prep-recovery-"))
common.SEGMENTS = root / "segments"
common.SEGMENTS.mkdir()

import prep  # noqa: E402

prep.common.SEGMENTS = common.SEGMENTS

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
for item_id in (3, 4):
    for n in range(2):
        (common.SEGMENTS / f"{item_id:05d}_{n:05d}.ts").write_text("chunk")

n = prep.recover_orphans(queue)
by_id = {i["id"]: i for i in queue["items"]}

check("l'item interrompu revient en attente", by_id[3]["status"] == "pending",
      by_id[3]["status"])
check("son ancien message d'erreur est efface", "error" not in by_id[3], by_id[3])
check("le compte est juste", n == 1, n)
check("un item joue n'est pas touche", by_id[1]["status"] == "played")
check("un item en erreur n'est pas touche", by_id[2]["status"] == "error")
check("un item pret n'est pas touche", by_id[4]["status"] == "ready")
check("un item en attente n'est pas touche", by_id[5]["status"] == "pending")

left = sorted(p.name for p in common.SEGMENTS.glob("*.ts"))
check("les chunks de l'item interrompu sont retires",
      left == ["00004_00000.ts", "00004_00001.ts"], left)

# the control: with nothing stranded it is a no-op and reports nothing
check("sans orphelin, rien n'est fait", prep.recover_orphans(queue) == 0)

print()
print(f"{passed}/{passed + failed} passent")
sys.exit(0 if failed == 0 else 1)
