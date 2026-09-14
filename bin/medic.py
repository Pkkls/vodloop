#!/usr/bin/env python3
"""Act on what the bot can only report. From cron, every few minutes.

    python3 bin/medic.py            say what it would do, change nothing
    python3 bin/medic.py --apply    do it

The alarms told the owner. The owner then had to go and fix it, which on
2026-09-06 meant killing two ffmpeg by hand at four in the morning. Detection
without action is still a person on call, so this closes that loop for the three
faults that actually happened.

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
import os
import pathlib
import shutil
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
# Restarting prep throws away an encode in progress, so this waits until the
# channel has been on the standby clip for three passes running.
DRY_PASSES_BEFORE_RESTART = 3
PREP_RESTART_COOLDOWN = 30 * 60

# The floor that makes the restart above pointless. On 2026-09-14 prep was
# restarted onto a disk holding 3.8 Go free, three passes running:
# disk_is_tight() stops it before it writes a single chunk, so the buffer never
# refilled, cx247 sat on the standby clip for half an hour, and the repair fired
# again and again with nothing to repair. Space first, then the restart has
# something to do.
#
# 4.5 Go of that disk was diagnostic scratch left in /tmp, mine. That half is
# free to give back and it goes first, because it costs the channel nothing.
SCRATCH_MIN_AGE_SECONDS = 2 * 3600
SCRATCH_MIN_BYTES = 64 * 1024 ** 2
DISK_REPAIR_COOLDOWN = 15 * 60
# The expensive half. A library file is given up only once the disk is already
# under the floor, and never below what the channel needs to rotate at all: a
# grey screen now is worse than less variety later, but an empty library is the
# same grey screen tomorrow.
KEEP_PLAYABLE_FILES = 3


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


def tree_bytes(path):
    """Bytes a file or a directory holds. Unreadable parts count as nothing."""
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    try:
        for sub in path.rglob("*"):
            try:
                if sub.is_file():
                    total += sub.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def scratch_debris(now, where="/tmp"):
    """Old, large leftovers under /tmp that belong to us, biggest first.

    The cron locks and prep's own remux probes are excluded by name: both are in
    use by definition, and a repair that shoots a lock is a worse fault than the
    one it came to fix. Ownership and age do the rest, so nothing another
    service is holding is ever a candidate.
    """
    out = []
    try:
        entries = list(pathlib.Path(where).iterdir())
    except OSError:
        return out
    for item in entries:
        if item.name.endswith(".lock") or item.name.startswith("remuxprobe_"):
            continue
        try:
            st = item.stat()
        except OSError:
            continue
        # ownership keeps us off anything another account put here. Windows has
        # no getuid, and the test suite has to run wherever the repo is cloned,
        # so there the filter simply does not apply.
        mine = getattr(os, "getuid", None)
        if mine is not None and st.st_uid != mine():
            continue
        if now - st.st_mtime < SCRATCH_MIN_AGE_SECONDS:
            continue
        size = tree_bytes(item)
        if size >= SCRATCH_MIN_BYTES:
            out.append((item, size))
    return sorted(out, key=lambda pair: -pair[1])


def spare_library_file():
    """The biggest library file this channel can give up now, or None.

    Played at least once, holding no chunks on the disk, and never one of the
    last few. The janitor already retires on budget and refuses when every file
    is protected, which is correct for a healthy disk and is exactly what left
    this one full. This is the emergency underneath it, not a second policy.
    """
    try:
        queue = json.loads((common.STATE / "queue.json").read_text())
        items = queue.get("items", []) if isinstance(queue, dict) else queue
    except (OSError, ValueError):
        items = []
    busy = {i["path"] for i in items
            if i.get("path") and list(common.SEGMENTS.glob("%05d_*.ts" % i["id"]))}
    try:
        playable = [f for f in common.LIBRARY_DIR.iterdir()
                    if f.is_file() and f.suffix.lower() in prep.MEDIA_SUFFIXES]
    except OSError:
        return None
    if len(playable) <= KEEP_PLAYABLE_FILES:
        return None
    history = prep.load_history()
    spare = [f for f in playable
             if str(f) not in busy and prep.played_count(history, f) > 0]
    if not spare:
        return None
    return max(spare, key=lambda f: f.stat().st_size)


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
                   ["sudo", "systemctl", "restart", common.unit("push")]):
                data["pusher_restarted"] = now
                did += 1
    else:
        print(f"  pusher emet ({moved} octets)")

    # 3. the disk floor. Nothing below this point can work without it.
    free = shutil.disk_usage(common.ROOT).free
    if free >= common.MIN_FREE_BYTES:
        print(f"  disque {free / 1024 ** 3:.1f}G libres")
    elif now - data.get("disk_freed", 0) < DISK_REPAIR_COOLDOWN:
        print("  disque sous le plancher, deja degage recemment, on attend")
    else:
        debris = scratch_debris(now)
        spare = None if debris else spare_library_file()
        if debris:
            gained = sum(size for _, size in debris)
            if act(apply,
                   "disque sous le plancher, %d reste(s) de diagnostic effaces"
                   " dans /tmp (%.1fG rendus)" % (len(debris), gained / 1024 ** 3),
                   ["rm", "-rf"] + [str(item) for item, _ in debris]):
                data["disk_freed"] = now
                did += 1
        elif spare is None:
            print("  disque sous le plancher et rien a rendre sans casser la rotation")
        elif act(apply,
                 "disque sous le plancher, %s retire (%.1fG, deja diffuse)"
                 % (spare.name, spare.stat().st_size / 1024 ** 3),
                 ["rm", "-f", str(spare)]):
            data["disk_freed"] = now
            did += 1

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
                 ["sudo", "systemctl", "restart", common.unit("prep")]):
            data["prep_restarted"] = now
            data["dry_passes"] = 0
            did += 1

    if apply:
        save(data)
    print(f"{did} action(s)" + ("" if apply else " -- essai a blanc, --apply pour agir"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
