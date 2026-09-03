#!/usr/bin/env python3
"""Owner-only skip. Run: python3 tests/test_owner_skip.py

Two halves that were both missing. The command itself, which answers to the
channel owner and to nobody else, and the half that makes a granted skip mean
something: nothing read last_skip, so every skip ever granted replied
"skipping" and let the video play to the end.
"""
import importlib
import json
import os
import pathlib
import sys
import tempfile

root = pathlib.Path(tempfile.mkdtemp(prefix="owner-skip-"))
for name in ("segments", "incoming", "state"):
    (root / name).mkdir()
os.environ["VODLOOP_ROOT"] = str(root)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import common  # noqa: E402

importlib.reload(common)
import chatlogic  # noqa: E402
import feeder  # noqa: E402

importlib.reload(feeder)

passed = 0
failed = 0

OWNER = "127469285"
MOD = "555"
STRANGER = "999"


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"ok   {label}")
    else:
        failed += 1
        print(f"RATE {label} {detail}")


def say(user_id, text, owner=OWNER):
    state = chatlogic.new_state()
    reply, changed = chatlogic.handle(
        {"user_id": user_id, "text": text}, {"seq": 0, "items": []}, state,
        mods=(MOD,), now=1000.0, owner=owner)
    return reply, changed, state


# --- who may use it -------------------------------------------------------
reply, changed, state = say(OWNER, "!nontent")
check("le proprietaire saute", reply == "skipping" and changed, reply)
check("et la demande est datee", state["last_skip"] == 1000.0, state["last_skip"])

check("un moderateur ne peut pas", say(MOD, "!nontent")[0] is None)
check("un inconnu ne peut pas", say(STRANGER, "!nontent")[0] is None)
check("sans proprietaire configure, personne ne peut",
      say(OWNER, "!nontent", owner="")[0] is None)
check("temoin: le meme utilisateur passe par !skip en tant que non-mod",
      say(OWNER, "!skip")[0] != "skipping")

# --- the half that makes it mean something --------------------------------
chat_json = common.STATE / "chat.json"
check("sans fichier d etat, aucune demande", feeder.skip_stamp() == 0.0)
chat_json.write_text("pas du json")
check("un etat illisible ne declenche pas un saut", feeder.skip_stamp() == 0.0)
chat_json.write_text(json.dumps({"last_skip": 1234.5}))
check("la demande du chat est lue", feeder.skip_stamp() == 1234.5, feeder.skip_stamp())

# --- a skip drops the video, not just the chunk in flight -----------------
for n in range(3):
    (common.SEGMENTS / f"00007_{n:05d}.ts").write_text("chunk")
for n in range(2):
    (common.SEGMENTS / f"00008_{n:05d}.ts").write_text("chunk")
dropped = feeder.drop_item_of(common.SEGMENTS / "00007_00000.ts")
check("les chunks restants de la video sautee partent", dropped == 3, dropped)
check("temoin: la video suivante est intacte",
      sorted(p.name for p in common.SEGMENTS.glob("*.ts"))
      == ["00008_00000.ts", "00008_00001.ts"],
      sorted(p.name for p in common.SEGMENTS.glob("*.ts")))

print()
print(f"{passed}/{passed + failed} passent")
sys.exit(0 if failed == 0 else 1)
