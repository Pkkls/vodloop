#!/usr/bin/env python3
"""The bot that speaks first. Run: python3 tests/test_tgbot.py

On 2026-09-05 the channel sat on the standby clip four separate times, and every
one of them was found the same way: the owner looked at it. Nothing watched the
two numbers that predict it. This bot exists to notice first, so the two
properties that matter are not the commands.

The first is that alarms are edge triggered. A message on every pass while a
condition holds is a message nobody reads by the second day, and a monitor
nobody reads is the state we were already in.

The second is that only one chat is obeyed. /skip on an open bot is a stranger's
lever on someone else's channel.
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import tgbot  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


sent = []
real_say, real_state, real_save = tgbot.say, tgbot.state, tgbot.save_state
real_disk, real_lib, real_free = tgbot.prep.seconds_on_disk, tgbot.library, tgbot.free_bytes
real_emit, real_restarts = tgbot.emitting, tgbot.restarts

store = {}
tgbot.say = lambda t: sent.append(t) or True
tgbot.state = lambda: dict(store)
tgbot.save_state = lambda d: store.update(d)
try:
    # healthy: plenty of backlog, plenty playable, plenty of disk, wire busy
    tgbot.prep.seconds_on_disk = lambda: 10 * 3600
    tgbot.library = lambda: (60, 40)
    tgbot.free_bytes = lambda: 30 * 1024 ** 3
    tgbot.emitting = lambda: 900000
    tgbot.restarts = lambda n: "1"

    print("silence while everything is fine")
    tgbot.health()
    check("nothing is said about a healthy channel", sent == [], str(sent))

    print("the backlog falls")
    tgbot.prep.seconds_on_disk = lambda: 0
    tgbot.health()
    check("it speaks once", len(sent) == 1 and "tampon" in sent[0].lower(), str(sent))

    # The control that matters most. Same broken state, another pass: a bot that
    # repeats itself every two minutes gets muted, and a muted bot is exactly
    # the situation this replaces.
    before = len(sent)
    tgbot.health()
    tgbot.health()
    check("and does not repeat while it stays broken", len(sent) == before,
          f"{len(sent) - before} message(s) de trop")

    print("and comes back")
    tgbot.prep.seconds_on_disk = lambda: 10 * 3600
    tgbot.health()
    check("recovery is announced once",
          len(sent) == before + 1 and "normale" in sent[-1], str(sent[-1:]))
    before = len(sent)
    tgbot.health()
    check("and that is not repeated either", len(sent) == before)

    print("each condition is its own alarm")
    sent.clear()
    store.clear()
    tgbot.library = lambda: (60, 2)
    tgbot.free_bytes = lambda: 1024 ** 3
    tgbot.health()
    check("two independent problems give two messages", len(sent) == 2, str(len(sent)))
    # the control: a bot that fired on everything at once would pass the line
    # above whatever the real conditions were
    sent.clear()
    store.clear()
    tgbot.library = lambda: (60, 40)
    tgbot.free_bytes = lambda: 30 * 1024 ** 3
    tgbot.health()
    check("and none when neither holds", sent == [], str(sent))

    print("a muted bot stays muted")
    store["quiet_until"] = 1e12
    tgbot.prep.seconds_on_disk = lambda: 0
    sent.clear()
    tgbot.health()
    check("nothing is sent while muted", sent == [], str(sent))
    store.pop("quiet_until")

    print("a silent wire is an alarm, an unmeasurable one is not")
    sent.clear()
    store.clear()
    tgbot.prep.seconds_on_disk = lambda: 10 * 3600
    tgbot.emitting = lambda: 0
    tgbot.health()
    check("zero bytes on the wire is reported", any("pusher" in s for s in sent), str(sent))
    sent.clear()
    store.clear()
    # None means "could not measure", which is not the same as "sending
    # nothing", and calling it one is how a probe invents an outage
    tgbot.emitting = lambda: None
    tgbot.health()
    check("no measurement is not an outage", sent == [], str(sent))
finally:
    tgbot.say, tgbot.state, tgbot.save_state = real_say, real_state, real_save
    tgbot.prep.seconds_on_disk, tgbot.library = real_disk, real_lib
    tgbot.free_bytes, tgbot.emitting, tgbot.restarts = real_free, real_emit, real_restarts

print("black on the viewer's side, told apart from unknown")
# The question that cost the most time today. Our side said healthy on every
# count, and a black screen was reported anyway; only fetching what the player
# fetches settled it. Three outcomes, and the third is what makes it honest.
real_vf = tgbot.viewer_frame
try:
    tgbot.viewer_frame = lambda: (True, "min=0 max=244 moy=61")
    out = tgbot.handle("/black")
    check("a real picture is reported as real", "REELLE" in out, out[:60])
    check("and it points at the stale player", "recharge" in out)

    # the control: same command, opposite measurement
    tgbot.viewer_frame = lambda: (False, "min=16 max=20 moy=17")
    out = tgbot.handle("/black")
    check("a flat frame is reported as black", "NOIRE" in out, out[:60])
    check("and called a real fault", "vraie panne" in out)

    # the one that matters. A probe that could not measure must not be allowed
    # to say "black": mine did exactly that this morning and sent me hunting an
    # outage that was not there.
    tgbot.viewer_frame = lambda: (None, "API 404")
    out = tgbot.handle("/black")
    check("no measurement is neither real nor black",
          "indetermine" in out and "NOIRE" not in out, out[:60])
finally:
    tgbot.viewer_frame = real_vf

print("only the bound chat is obeyed")
source = (pathlib.Path(__file__).resolve().parent.parent
          / "bin" / "tgbot.py").read_text(encoding="utf-8")
check("commands from any other chat are dropped",
      "if chat != bound:" in source and "continue" in source)
check("the binding happens once, on /start",
      'if not bound and text.strip().startswith("/start")' in source)
check("the token is read from a file, never written in the code",
      "TG_TOKEN" in source and "8669" not in source)

print("the commands answer something")
for cmd in ("/status", "/now", "/lib", "/conv", "/help", "/start"):
    check(f"{cmd} is handled", f'"{cmd}"' in source or f"'{cmd}'" in source)
check("an unknown word is ignored rather than answered",
      "return None" in source.split("def handle")[1].split("def ")[0])

print("one bot, one token, two channels")
# Deux pollers sur le meme token se volent leurs getUpdates et chacun ne voit
# que la moitie des commandes. Le seul poller repond donc pour les autres en
# relancant ce meme fichier avec LEUR environnement, qui est la seule chose qui
# les distingue.
_real_run, _real_others = tgbot.subprocess.run, tgbot.OTHERS
try:
    vu = {}

    class _Res:
        stdout = "reponse de l'autre chaine"

    def _fake_run(cmd, **kw):
        vu["cmd"] = cmd
        vu["env"] = kw.get("env", {})
        return _Res()

    tgbot.subprocess.run = _fake_run
    tgbot.OTHERS = ["nanatty247"]
    out = tgbot.handle("/status nanatty247")
    check("la commande part vers l'autre chaine",
          "reponse de l'autre chaine" in out and out.startswith("[nanatty247]"), out)
    check("avec la racine de CETTE chaine la",
          vu["env"].get("VODLOOP_ROOT") == "/home/ubuntu/nanatty247",
          vu["env"].get("VODLOOP_ROOT"))
    check("et sa bibliotheque, et son motif d'unite",
          vu["env"].get("VODLOOP_LIBRARY") == "/home/ubuntu/videos-nanatty247"
          and vu["env"].get("VODLOOP_UNIT") == "vodloop-%s@nanatty247",
          str(vu["env"].get("VODLOOP_UNIT")))
    check("l'enfant ne croit pas repondre pour d'autres a son tour",
          "VODLOOP_CHANNELS" not in vu["env"])
    check("c'est bien la meme commande qui est reposee",
          vu["cmd"][-2:] == ["--answer", "/status"], str(vu["cmd"][-2:]))
    # le temoin: sans chaine declaree, la meme phrase reste locale
    vu.clear()
    tgbot.OTHERS = []
    tgbot.status_text = lambda: "reponse locale"
    check("temoin: sans chaine declaree, rien n'est delegue",
          tgbot.handle("/status nanatty247") == "reponse locale" and not vu)
finally:
    tgbot.subprocess.run, tgbot.OTHERS = _real_run, _real_others

print("a fault that lasts keeps saying so")
# Mesure 2026-09-14: l'alarme bibliotheque de la 2e chaine etait bloquee sur
# "3 fichiers jouables" depuis la veille et n'avait parle qu'une fois, parce que
# health() ne disait quelque chose que sur le front montant. Une panne de 24 h
# produisait UN message, dans un fil que deux chaines partagent.
import tempfile as _tf, pathlib as _pl, time as _t
_root = _pl.Path(_tf.mkdtemp())
_lib = _pl.Path(_tf.mkdtemp())
saved = (tgbot.STATEFILE, tgbot.LIBRARY, tgbot.common.STATE, tgbot.say,
         tgbot.emitting, tgbot.library, tgbot.prep.seconds_on_disk,
         tgbot.free_bytes, tgbot.restarts)
try:
    tgbot.STATEFILE = _root / "tg.json"
    tgbot.common.STATE = _root
    tgbot.LIBRARY = _lib
    spoken = []
    tgbot.say = lambda text: spoken.append(text) or True
    tgbot.emitting = lambda: 1000
    tgbot.library = lambda: (3, 3)          # sous le seuil, l'alarme doit tenir
    tgbot.prep.seconds_on_disk = lambda: 99999
    tgbot.free_bytes = lambda: 99 * 1024 ** 3
    tgbot.restarts = lambda name: "0"

    tgbot.health()
    check("la premiere fois, elle alerte", any("ALERTE" in s for s in spoken),
          str(spoken[:1]))
    spoken.clear()
    tgbot.health()
    check("juste apres, elle se tait", spoken == [], str(spoken))
    # on recule la date du dernier message: la panne dure
    data = tgbot.state()
    data["alarm_said"] = {k: _t.time() - tgbot.REMIND_SECONDS - 1
                          for k in data.get("alarms", {})}
    tgbot.save_state(data)
    tgbot.health()
    check("mais passe le delai de rappel, elle le redit",
          any("TOUJOURS EN PANNE" in s for s in spoken), str(spoken[:1]))

    print("rien qui arrive est une panne en soi")
    # le temoin d'abord: une bibliotheque qui vient de recevoir ne dit rien
    (_lib / "frais-abcDEF12345.mkv").write_bytes(b"x")
    check("temoin: un fichier qui vient darriver ne declenche rien",
          tgbot.newest_arrival() < 60, str(tgbot.newest_arrival()))
    import os as _os
    vieux = _t.time() - tgbot.STALE_ARRIVAL_SECONDS - 3600
    _os.utime(_lib / "frais-abcDEF12345.mkv", (vieux, vieux))
    check("un fichier trop vieux rend bien son age",
          tgbot.newest_arrival() > tgbot.STALE_ARRIVAL_SECONDS)
    spoken.clear()
    tgbot.STATEFILE = _root / "tg2.json"
    tgbot.health()
    check("et la chaine alerte sur labsence darrivee",
          any("rien de neuf" in s for s in spoken), str(spoken))
    # une bibliotheque vide n'a pas d'age: c'est un autre probleme, couvert par
    # l'alarme bibliotheque, et en faire une alarme d'arrivee dirait deux fois
    # la meme chose
    (_lib / "frais-abcDEF12345.mkv").unlink()
    check("une bibliotheque vide ne fabrique pas de fausse alarme darrivee",
          tgbot.newest_arrival() is None)
finally:
    (tgbot.STATEFILE, tgbot.LIBRARY, tgbot.common.STATE, tgbot.say,
     tgbot.emitting, tgbot.library, tgbot.prep.seconds_on_disk,
     tgbot.free_bytes, tgbot.restarts) = saved

print("thresholds warn before the floor prep abandons at")
check("the backlog alarm fires above prep's abandon floor",
      tgbot.LOW_BACKLOG_SECONDS > tgbot.prep.ABANDON_BELOW_SECONDS,
      f"{tgbot.LOW_BACKLOG_SECONDS}s contre {tgbot.prep.ABANDON_BELOW_SECONDS}s")

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
