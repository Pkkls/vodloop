#!/usr/bin/env python3
"""The repairs that used to need a person. Run: python3 tests/test_medic.py

The alarms told the owner and the owner went and fixed it, which on 2026-09-06
meant killing two ffmpeg by hand at four in the morning. This file is about the
one thing that can go wrong with automating that: a repair that fires when it
should not is worse than the fault it repairs.

So the checks here are mostly about restraint. The single most important one is
that the pusher's own ffmpeg is never mistaken for a stray: it has ppid 1
because systemd started it, so the naive test kills the stream it exists to
protect.
"""
import pathlib
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import medic  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


class FakeProc:
    """A /proc tree with exactly the processes we want medic to look at."""

    def __init__(self, root, entries):
        self.root = pathlib.Path(root)
        for pid, (ppid, cgroup, age) in entries.items():
            d = self.root / str(pid)
            d.mkdir(parents=True)
            (d / "stat").write_text(f"{pid} (ffmpeg) S {ppid} " + "0 " * 40)
            (d / "cgroup").write_text(cgroup)
            old = time.time() - age
            import os
            os.utime(d, (old, old))
        self.pids = list(entries)


OLD = medic.STRAY_MIN_AGE_SECONDS + 60

with tempfile.TemporaryDirectory() as tmp:
    tree = FakeProc(tmp, {
        # the pusher: ppid 1 because systemd started it. Never a stray.
        "100": (1, "0::/system.slice/vodloop-push.service\n", OLD),
        # the feeder's remux: a live parent
        "200": (150, "0::/system.slice/vodloop-feed.service\n", OLD),
        # normalise.py's encoder, run from cron: a live parent, so owned
        "300": (250, "0::/user.slice/session-1.scope\n", OLD),
        # the real thing: nobody waiting, no unit, old enough
        "400": (1, "0::/user.slice/session-2.scope\n", OLD),
        # same shape but just started: too young to call abandoned
        "500": (1, "0::/user.slice/session-3.scope\n", 10),
    })
    real_sh = medic.sh
    def only_pgrep(args, timeout=30):
        return "\n".join(tree.pids) if args[:2] == ["pgrep", "-x"] else ""

    medic.sh = only_pgrep
    try:
        found = medic.strays(proc=tmp)
        print("what counts as abandoned")
        # THE check. ppid==1 alone would kill this and take the stream with it.
        check("the pusher is never a stray", "100" not in found, str(found))
        check("nor is anything else inside a vodloop unit", "200" not in found)
        check("nor an encoder whose parent is still alive", "300" not in found)
        check("nor one too young to be sure about", "500" not in found)
        # the control: with all of those excluded something must still be found,
        # or this function has simply stopped returning anything
        check("the genuinely orphaned one is found", found == ["400"], str(found))
    finally:
        medic.sh = real_sh

print("restraint")
source = (pathlib.Path(__file__).resolve().parent.parent
          / "bin" / "medic.py").read_text(encoding="utf-8")
check("a silent pusher is not restarted twice in a row",
      "pusher_restarted" in source and "SILENT_PUSHER_COOLDOWN" in source)
check("prep is not restarted twice in a row",
      "prep_restarted" in source and "PREP_RESTART_COOLDOWN" in source)
check("prep waits several empty passes, not one",
      medic.DRY_PASSES_BEFORE_RESTART >= 3, f"{medic.DRY_PASSES_BEFORE_RESTART}")
check("an unmeasurable wire is left alone, not repaired",
      "debit du pusher non mesurable, on ne touche a rien" in source)
check("nothing is done without --apply",
      'apply = "--apply" in argv' in source and "if not apply:" in source)
check("every repair is announced", "tgbot.say(" in source)

print("dry run changes nothing")
real_strays, real_wire = medic.strays, medic.wire_bytes
real_disk = medic.prep.seconds_on_disk
real_say = medic.tgbot.say
said = []
try:
    medic.strays = lambda: ["999"]
    medic.wire_bytes = lambda seconds=8: 0
    medic.prep.seconds_on_disk = lambda: 0
    medic.tgbot.say = lambda t: said.append(t) or True
    medic.main([])
    check("a dry run says nothing to telegram", said == [], str(said))
    # the control: the same conditions with --apply must produce messages, or
    # the check above passes on a medic that never speaks at all
    calls = []
    real_run = medic.subprocess.run
    medic.subprocess.run = lambda *a, **k: calls.append(a[0]) or subprocess.CompletedProcess(a[0], 0, "", "")
    real_save, medic.save = medic.save, lambda d: None
    try:
        medic.main(["--apply"])
        check("applying does speak", len(said) >= 1, str(len(said)))
        check("and actually runs commands", len(calls) >= 1, str(calls[:2]))
    finally:
        medic.subprocess.run = real_run
        medic.save = real_save
finally:
    medic.strays, medic.wire_bytes = real_strays, real_wire
    medic.prep.seconds_on_disk = real_disk
    medic.tgbot.say = real_say

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
