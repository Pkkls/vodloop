#!/usr/bin/env python3
"""One server, two channels. Run: python3 tests/test_coexistence.py

Every check here is about the same failure: a process of one channel acting on
the other channel's things. None of them would show up as an error anywhere.
They show up as the first channel going off air while its own logs say it is
fine, because the second channel's medic restarted its pusher, or its chat
reordered the first one's queue.

The file runs in two halves on purpose. The first measures what a channel with
nothing set in its environment sees, which is the deployed channel and must not
change at all. The second reloads the same module with a second channel's
environment and measures again. A difference between the two halves is the
whole feature; identical halves would mean the environment is being ignored.
"""
import importlib
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import chat  # noqa: E402
import common  # noqa: E402
import quality  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def cgroup_dir(text):
    """A stand-in for /proc/<pid>, holding one cgroup line."""
    entry = pathlib.Path(tempfile.mkdtemp())
    (entry / "cgroup").write_text(text)
    return entry


print("the deployed channel, with nothing set")
FIRST = {"unit": common.unit("push"), "library": common.LIBRARY_DIR,
         "label": common.LABEL, "fifo": common.FIFO,
         "budget": common.BUDGET_BYTES}
check("son unite est celle qui tourne aujourd'hui", FIRST["unit"] == "vodloop-push",
      FIRST["unit"])
# as_posix, because this file also has to pass on the machine it is written
# on, where a path prints with the other separator
check("sa bibliotheque est celle qui existe",
      FIRST["library"].as_posix() == "/home/ubuntu/videos",
      FIRST["library"].as_posix())
check("il ne prefixe rien dans le chat Telegram", FIRST["label"] == "")
check("et il n'a pas de part de disque", FIRST["budget"] == 0)

print("finding a process by what it does is not finding whose it is")
check("l'aiguille du pusher est liee a SON tuyau",
      str(common.FIFO) in quality.WATCHED["push"], str(quality.WATCHED["push"]))
mine = cgroup_dir("0::/system.slice/vodloop-push.service\n")
theirs = cgroup_dir("0::/system.slice/vodloop-push@second.service\n")
check("un process de cette chaine est reconnu", quality._owned(mine, "push"))
# the trap: vodloop-push IS a substring of vodloop-push@second, so a plain
# containment test hands the neighbour's pusher back as this channel's own
check("celui du voisin ne l'est pas, malgre le prefixe commun",
      not quality._owned(theirs, "push"))
check("et un process sans cgroup lisible n'appartient a personne",
      not quality._owned(pathlib.Path("/nonexistent"), "push"))

print("chat: one bus, every broadcaster on the same feed")
OURS = "127469285"
envelope = {"type": "chat.message.sent", "broadcaster": OURS,
            "data": {"content": "!skip", "sender": {"user_id": 1, "username": "x"}}}
check("un evenement de cette chaine passe", chat.for_us(envelope, OURS))
check("celui du voisin est jete",
      not chat.for_us(dict(envelope, broadcaster="129383464"), OURS))
# an envelope with no broadcaster is what an older bus publishes, and refusing
# it would take the channel off chat over a shape change rather than a mismatch
check("une enveloppe sans diffuseur passe quand meme",
      chat.for_us({"data": envelope["data"]}, OURS))
check("temoin: sans proprietaire configure, tout passe",
      chat.for_us(dict(envelope, broadcaster="129383464"), None))
check("et l'URL du bus demande deja le filtre au serveur",
      chat.sse_url(OURS).endswith("&broadcaster=" + OURS), chat.sse_url(OURS))
check("temoin: sans proprietaire, l'URL reste celle d'avant",
      chat.sse_url(None) == chat.SSE_URL)

print("the same code, with a second channel's environment")
root = pathlib.Path(tempfile.mkdtemp())
os.environ.update({
    "VODLOOP_ROOT": str(root),
    "VODLOOP_UNIT": "vodloop-%s@second",
    "VODLOOP_LIBRARY": "/home/ubuntu/videos-second",
    "VODLOOP_LABEL": "[second]",
    "VODLOOP_BUDGET_GB": "13.5",
})
importlib.reload(common)
# Reloaded, not merely imported: another module in this file's own imports
# already pulled it in under the first channel, and a cached module would
# hand back the first channel's unit names and pass the checks below for
# the wrong reason. A reload re-runs the module body, which is what a fresh
# process does.
import tgbot  # noqa: E402
importlib.reload(tgbot)

check("l'unite n'est plus celle de la premiere chaine",
      common.unit("push") == "vodloop-push@second" != FIRST["unit"],
      common.unit("push"))
check("la bibliotheque non plus", common.LIBRARY_DIR != FIRST["library"],
      str(common.LIBRARY_DIR))
check("le tuyau non plus", common.FIFO != FIRST["fifo"], str(common.FIFO))
check("et elle a sa part du disque",
      abs(common.BUDGET_BYTES - 13.5 * 1024 ** 3) < 1, str(common.BUDGET_BYTES))

check("le bot parle des unites de SA chaine", tgbot.PUSH == "vodloop-push@second",
      tgbot.PUSH)
check("et il regarde SA bibliotheque", tgbot.LIBRARY == common.LIBRARY_DIR)

asked = []
tgbot.sh = lambda args, timeout=30: asked.append(args) or ""
check("le pusher qu'il mesure est celui de son propre tuyau",
      tgbot.emitting() is None
      and str(common.FIFO) in " ".join(asked[0]), str(asked[0]))
# the control: the first channel's pipe must NOT be in that pattern, or the
# scoping would be decorative and medic would still restart the neighbour
check("temoin: le tuyau de la premiere chaine n'y est pas",
      str(FIRST["fifo"]) not in " ".join(asked[0]), str(asked[0]))

said = []
tgbot.env = lambda: {"TG_CHAT": "1"}
tgbot.api = lambda method, **kw: said.append(kw.get("text")) or {"ok": True}
tgbot.say("le tampon est vide")
check("ce qu'il dit porte le nom de sa chaine",
      said[-1] == "[second] le tampon est vide", said[-1])
check("et son alarme disque est celle du plancher partage",
      tgbot.LOW_FREE_BYTES == common.SHARED_FREE_FLOOR_BYTES
      and tgbot.LOW_FREE_BYTES < 6 * 1024 ** 3,
      f"{tgbot.LOW_FREE_BYTES / 1024 ** 3:.0f}G")

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
