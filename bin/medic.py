#!/usr/bin/env python3
"""Act on what the bot can only report. From cron, every few minutes.

    python3 bin/medic.py            say what it would do, change nothing
    python3 bin/medic.py --apply    do it

The alarms told the owner. The owner then had to go and fix it, which on
2026-09-06 meant killing two ffmpeg by hand at four in the morning. Detection
without action is still a person on call, so this closes that loop for the three
faults that actually happened.

It also carries out one thing that is not a fault at all: a prep restart the
operator asked for from the panel. That lands here rather than in the panel
because the panel is sandboxed with NoNewPrivileges and this is not, and because
the restart, its cooldown and its announcement already live here. See
restart_requested.

Every one of them follows the same shape, learned the hard way today:

  - measure first, never infer. A thing that cannot be measured is unknown, and
    unknown is not broken. Two probes of mine called "no answer" an outage and
    sent me hunting one that did not exist.
  - act at most once per cooldown. A repair that can fire in a loop is a worse
    fault than the one it repairs, which is exactly how the abandon watchdog
    took the channel down.
  - say what was done. A silent repair is indistinguishable from a system that
    never broke, and the difference matters when it happens nightly.
"""
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import common
import prep
import tgbot

STATEFILE = common.STATE / "medic.json"

# An ffmpeg with no parent left and no vodloop unit around it. Two of these sat
# at 84% and 71% for two hours on a two vCPU box, starved the remuxes and
# blacked the channel out. Given a grace period so a job that is simply between
# parents is not shot on sight.
STRAY_MIN_AGE_SECONDS = 300
# The pusher can hold its socket open and send nothing. systemd sees a running
# process and is satisfied; viewers see a frozen frame. Nothing detected this.
SILENT_PUSHER_COOLDOWN = 20 * 60
# Restarting prep throws away the chunk being cut, so this waits until the
# channel has been on the standby clip for three passes running.
DRY_PASSES_BEFORE_RESTART = 3
PREP_RESTART_COOLDOWN = 30 * 60
# A restart the operator asked for from the panel, rather than one deduced from
# an empty buffer. The panel cannot run systemctl itself: harden-oracle.sh gives
# that unit NoNewPrivileges=yes, which is what stops sudo elevating. This runs
# from cron outside any sandbox, so the ask lands here instead.
#
# It is bounded in time because a request written while this was not running
# describes a situation that has since changed, and restarting prep an hour late
# helps nobody. At one tick every four minutes, fifteen leaves room for three
# missed passes.
REQUEST_MAX_AGE_SECONDS = 15 * 60


def load():
    try:
        data = json.loads(STATEFILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    common.STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATEFILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(STATEFILE)


def sh(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def strays(proc="/proc"):
    """ffmpeg processes that nothing owns any more.

    proc is a parameter only so a test can hand it a directory of its own. A
    test that has to patch pathlib globally to reach this is a test that will
    break something else instead.

    Two tests, and both are needed. ppid==1 alone is wrong: the pusher's own
    ffmpeg has ppid 1 because systemd started it, and killing that is the outage
    this is supposed to prevent. The cgroup says which unit a process belongs
    to, and one still inside vodloop-*.service is doing its job whatever its
    parent is.

    A live parent also means owned, so an encode normalise.py is running from
    cron is left alone.
    """
    out = []
    for pid in sh(["pgrep", "-x", "ffmpeg"]).split():
        try:
            base = pathlib.Path(proc) / str(pid)
            ppid = int((base / "stat").read_text().split()[3])
            cgroup = (base / "cgroup").read_text()
            age = time.time() - base.stat().st_mtime
        except (OSError, ValueError, IndexError):
            continue
        if ppid != 1:
            continue                       # somebody is still waiting on it
        if "vodloop-" in cgroup and ".service" in cgroup:
            continue                       # a unit owns it
        if age < STRAY_MIN_AGE_SECONDS:
            continue                       # too young to call abandoned
        out.append(pid)
    return out


def wire_bytes(seconds=8):
    """Bytes the pusher actually put on the wire, or None if unmeasurable."""
    first = tgbot.emitting()
    if first is None:
        return None
    time.sleep(seconds)
    second = tgbot.emitting()
    return None if second is None else first + second


def restart_requested():
    """Whether the panel has asked for a prep restart recently.

    Only the file's age is read. Its contents are never opened, so nothing a
    caller can write into it reaches this process, and a stale one left on the
    disk is inert rather than a restart waiting to go off.

    This asks nothing and changes nothing, which is what lets a dry run report
    the request without consuming it.
    """
    try:
        age = time.time() - common.PREP_RESTART_REQUEST.stat().st_mtime
    except OSError:
        return False
    return age <= REQUEST_MAX_AGE_SECONDS


def clear_request():
    try:
        common.PREP_RESTART_REQUEST.unlink(missing_ok=True)
    except OSError:
        pass  # a request that cannot be cleared re-fires once, and is bounded


def act(apply, note, command):
    if not apply:
        print(f"  ferait: {note}")
        return False
    print(f"  fait: {note}", flush=True)
    if command:
        subprocess.run(command, capture_output=True, timeout=120)
    tgbot.say(f"reparation automatique: {note}")
    return True


def main(argv):
    apply = "--apply" in argv
    data = load()
    now = time.time()
    did = 0

    # 1. processes nothing owns, eating the CPU the channel needs
    dead = strays()
    if dead:
        did += act(apply, f"{len(dead)} ffmpeg orphelin(s) tue(s) (pid {' '.join(dead)})",
                   ["kill", "-9"] + dead)
    else:
        print("  aucun ffmpeg orphelin")

    # 2. a pusher that holds its socket and sends nothing
    moved = wire_bytes()
    if moved is None:
        print("  debit du pusher non mesurable, on ne touche a rien")
    elif moved == 0:
        if now - data.get("pusher_restarted", 0) < SILENT_PUSHER_COOLDOWN:
            print("  pusher muet mais deja relance recemment, on attend")
        else:
            if act(apply, "pusher muet malgre un process vivant, relance",
                   ["sudo", "systemctl", "restart", "vodloop-push"]):
                data["pusher_restarted"] = now
                did += 1
    else:
        print(f"  pusher emet ({moved} octets)")

    # 3. a restart the operator asked for, rather than one deduced from a fault
    if restart_requested():
        # Deliberately outside PREP_RESTART_COOLDOWN. That cooldown exists to
        # stop this repair firing in a loop against a condition it cannot fix,
        # and a person asking once is not a loop: the request is cleared as soon
        # as it is honoured, and only the token-gated panel can write one.
        if act(apply, "redemarrage de prep demande depuis le panneau",
               ["sudo", "systemctl", "restart", "vodloop-prep"]):
            data["prep_restarted"] = now
            data["dry_passes"] = 0
            clear_request()
            did += 1
    else:
        print("  aucune demande de redemarrage de prep")

    # 4. a channel that has been on the standby clip for several passes
    backlog = prep.seconds_on_disk()
    dry = data.get("dry_passes", 0) + 1 if backlog == 0 else 0
    data["dry_passes"] = dry
    if backlog == 0:
        print(f"  tampon vide ({dry}/{DRY_PASSES_BEFORE_RESTART} passes)")
    else:
        print(f"  tampon {backlog}s")
    if dry >= DRY_PASSES_BEFORE_RESTART:
        if now - data.get("prep_restarted", 0) < PREP_RESTART_COOLDOWN:
            print("  prep deja relance recemment, on attend")
        elif act(apply, f"tampon vide depuis {dry} passes, prep relance",
                 ["sudo", "systemctl", "restart", "vodloop-prep"]):
            data["prep_restarted"] = now
            data["dry_passes"] = 0
            did += 1

    if apply:
        save(data)
    print(f"{did} action(s)" + ("" if apply else " -- essai a blanc, --apply pour agir"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
