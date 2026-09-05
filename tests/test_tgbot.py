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

print("thresholds warn before the floor prep abandons at")
check("the backlog alarm fires above prep's abandon floor",
      tgbot.LOW_BACKLOG_SECONDS > tgbot.prep.ABANDON_BELOW_SECONDS,
      f"{tgbot.LOW_BACKLOG_SECONDS}s contre {tgbot.prep.ABANDON_BELOW_SECONDS}s")

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
