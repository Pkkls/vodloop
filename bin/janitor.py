#!/usr/bin/env python3
"""Keep enough free disk that prep never stops. Run from cron.

The channel does not run out of video: refill_from_library puts the whole
library back in the queue every pass, so it rotates forever. What it runs out of
is disk. yt2oracle adds a VOD every few minutes and nothing has ever removed
one, so the library only grows, and prep halts outright below MIN_FREE_BYTES.
Measured 2026-09-05: 31G of library on a 45G disk, 6.3G free against a 4G floor.
Left alone that ends in a standby clip that never goes away, and no amount of
fetching more video prevents it. This is the part that makes 24/7 real.

Moving files elsewhere frees nothing, it is all one filesystem, so retiring a
VOD means deleting it. That is softened by what these files are: yt2oracle can
fetch any of them again from the source, so this costs download time, not
material.

    python3 bin/janitor.py            report what it would retire, change nothing
    python3 bin/janitor.py --apply    retire until the target is met

Three rules bound it. Only files every queue entry has finished playing are
eligible, so nothing vanishes mid-rotation. The library never falls below
MIN_LIBRARY_FILES, so a disk problem cannot empty the channel instead. And the
oldest go first, which on a rerun channel is the one seen longest ago.
"""
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import collector
import common
import prep

LIBRARY = common.LIBRARY_DIR
MEDIA = (".mp4", ".mkv")
# Comfortably above prep's MIN_FREE_BYTES: reaching prep's floor is the failure,
# so the janitor has to act well before it. Lowered from 8G when the channel
# went to 1080p: the backlog alone is 4.5G at this bitrate, and a target the
# disk cannot reach makes this refuse to act at all, which is worse than a
# smaller margin.
# Above the collector's own floor, so the two of them compose into a loop
# instead of cancelling out: this frees to here, the collector fills while it
# has more than 8 Go to work with, and the cycle repeats. At 5 Go, which is
# what this was until 2026-09-08, free space settled just under the collector's
# threshold and no video was ever fetched again.
TARGET_FREE_BYTES = 10 * 1024 ** 3
# With a share of the disk (common.BUDGET_BYTES) the two numbers above change
# meaning. Retiring is decided by this channel's own bytes, down to this far
# under its share. Free space keeps only a floor, one Go above where prep stops,
# because it belongs to both channels: set at 10 Go it would have this retire
# its own files for the neighbour's arrivals.
#
# Far enough under for the collector to hand over its heaviest possible video.
# At 3.5 Go the two deadlocked: the collector asks for its headroom plus the
# weight of the next video, up to 5.5 Go, so it stopped around 21 Go of a 28 Go
# share, while this only woke above 24.5. Measured 2026-09-17: 23.3 Go held,
# "rien a faire" here and "pas la place d'un fichier de plus" there every run
# for a day, four stocked videos waiting on the board's card, and the channel
# replaying the same twelve videos, 33 h of them, round and round.
BUDGET_RETIRE_UNDER_BYTES = collector.BUDGET_HEADROOM_BYTES + collector.MAX_FETCH_BYTES
SHARED_FREE_FLOOR_BYTES = common.SHARED_FREE_FLOOR_BYTES
# Below this much unplayed video, nothing more is retired whatever the disk
# says: a full disk is a problem, dead air is the problem. Shared with prep,
# which retires for a different reason and would empty the same library through
# the same door. It used to be a file count here, and a count froze the rotation
# outright once the library fell below it.
MIN_RUNWAY_SECONDS = common.MIN_RUNWAY_SECONDS
# Above this, played files are retired even with disk to spare. Without it
# nothing ever leaves: the collector stops at its own ceiling, the janitor only
# woke on disk pressure, and the library sat at a fixed 69 files forever, which
# is a rerun channel with no reruns to add. This is what makes room for new
# material rather than merely surviving a full disk.
MAX_LIBRARY_FILES = 62


def live_paths(queue, history=None):
    """Files a queue entry still needs. Retiring one of these cuts a video off
    mid-rotation, or leaves prep pointed at a path that no longer exists.

    Given prep's history, a pending entry for a file that has already been on
    air stops counting. When nothing new is left prep queues the whole library
    again, so every file is pending for the length of that pass, 33 h on
    2026-09-17, and nothing could leave to make room for what was waiting. A
    pending entry has no chunks yet, and prep drops one whose file is gone
    before it reads the queue, so taking its file costs a replay, not a cut.
    """
    return {i.get("path") for i in queue["items"]
            if i.get("status") in ("preparing", "ready")
            or (i.get("status") == "pending"
                and (history is None or prep.played_count(history, i.get("path")) == 0))}


# The channel plays only files prep can copy, so that subset is the channel.
# Never cut it below this: emptying it is the same outage as an empty disk, and
# it arrives without warning because the files are still there.
MIN_PLAYABLE_FILES = 12


def retirable(queue, history=None):
    """Eligible files, the ones worth least to the channel first.

    Order matters more than it looks. A file prep cannot copy contributes
    nothing until normalise.py has converted it, while a copyable one is what
    is actually on air, so the uncopyable go first and buy the same disk at no
    cost to the rotation. Retiring by age alone deleted a playable file and left
    four unplayable ones sitting next to it.

    Within each group, oldest first, which on a rerun channel is the one seen
    longest ago. Given prep's history, what has been on air goes before what
    has not, queued longest ago first.
    """
    if not LIBRARY.is_dir():
        return []
    busy = live_paths(queue, history)
    files = [p for p in LIBRARY.iterdir()
             if p.is_file() and p.suffix.lower() in MEDIA
             and str(p) not in busy]
    playable = sorted((p for p in files if prep.remux_verdict(p)),
                      key=lambda p: p.stat().st_mtime)
    # the newest playable ones are off the table entirely, whatever the disk says
    protected = set(playable[-MIN_PLAYABLE_FILES:])

    def order(p):
        if history is None:
            return (p in set(playable), p.stat().st_mtime)
        return (p in set(playable), prep.played_count(history, p) == 0,
                prep.last_queued(history, p), p.stat().st_mtime)
    return sorted((p for p in files if p not in protected), key=order)


def main(argv):
    apply = "--apply" in argv
    queue = common.load_queue()
    before = shutil.disk_usage(LIBRARY).free
    total = len([p for p in LIBRARY.iterdir()
                 if p.is_file() and p.suffix.lower() in MEDIA]) if LIBRARY.is_dir() else 0

    budget = common.BUDGET_BYTES
    target_free = SHARED_FREE_FLOOR_BYTES if budget else TARGET_FREE_BYTES
    used = common.bytes_used(LIBRARY) if budget else 0
    target_used = budget - BUDGET_RETIRE_UNDER_BYTES

    def satisfied(freed, kept):
        # keep going while the disk, the share or the ceiling asks for it
        return (before + freed >= target_free
                and (not budget or used - freed <= target_used)
                and kept <= MAX_LIBRARY_FILES)

    # durations land in this dict, so a dry run can subtract what it would
    # retire instead of asking the disk, where nothing has been deleted
    history = prep.load_history()
    runway = prep.runway_seconds(history=history)
    share = (f" part={used / 1024 ** 3:.1f}/{budget / 1024 ** 3:.1f}G "
             f"cible<={target_used / 1024 ** 3:.1f}G" if budget else "")
    print(f"libre={before / 1024 ** 3:.1f}G cible={target_free / 1024 ** 3:.1f}G{share} "
          f"bibliotheque={total} plafond={MAX_LIBRARY_FILES} "
          f"reserve={int(runway / 60)}min "
          f"plancher={MIN_RUNWAY_SECONDS // 60}min")
    if satisfied(0, total):
        print("rien a faire")
        return 0

    freed = 0
    kept = total
    for path in retirable(queue, history):
        if satisfied(freed, kept):
            break
        # Only the share is asking: then a video nobody has seen yet stays. It
        # is what the room is being made for, and retiring it to fetch another
        # would turn the rotation over without ever putting anything on air.
        if (before + freed >= target_free and kept <= MAX_LIBRARY_FILES
                and prep.played_count(history, path) == 0 and prep.remux_verdict(path)):
            continue
        left = runway - ((history.get(str(path)) or {}).get("secs") or 0)
        if left < MIN_RUNWAY_SECONDS:
            print(f"plancher atteint: sans {path.name[:40]} il resterait "
                  f"{int(left / 60)} min a diffuser, il manque encore "
                  f"{max(target_free - before - freed, used - freed - target_used if budget else 0) / 1024 ** 3:.1f}G")
            print("-> le disque ne peut pas etre tenu en retirant des VOD seuls")
            return 1
        size = path.stat().st_size
        print(f"  {'retire' if apply else 'retirerait'} {size / 1024 ** 3:.2f}G  {path.name[:60]}")
        if apply:
            path.unlink(missing_ok=True)
        freed += size
        kept -= 1
        runway = left

    after = shutil.disk_usage(LIBRARY).free if apply else before + freed
    print(f"{'libere' if apply else 'liberable'}: {freed / 1024 ** 3:.2f}G, "
          f"libre {'maintenant' if apply else 'deviendrait'} {after / 1024 ** 3:.1f}G, "
          f"{kept} fichiers restants")
    if not apply:
        print("essai a blanc, rien n'a ete supprime. --apply pour agir.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
