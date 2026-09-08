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

import common
import prep

LIBRARY = pathlib.Path("/home/ubuntu/videos")
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


def live_paths(queue):
    """Files a queue entry still needs. Retiring one of these cuts a video off
    mid-rotation, or leaves prep pointed at a path that no longer exists."""
    return {i.get("path") for i in queue["items"]
            if i.get("status") in ("pending", "preparing", "ready")}


# The channel plays only files prep can copy, so that subset is the channel.
# Never cut it below this: emptying it is the same outage as an empty disk, and
# it arrives without warning because the files are still there.
MIN_PLAYABLE_FILES = 12


def retirable(queue):
    """Eligible files, the ones worth least to the channel first.

    Order matters more than it looks. A file prep cannot copy contributes
    nothing until normalise.py has converted it, while a copyable one is what
    is actually on air, so the uncopyable go first and buy the same disk at no
    cost to the rotation. Retiring by age alone deleted a playable file and left
    four unplayable ones sitting next to it.

    Within each group, oldest first, which on a rerun channel is the one seen
    longest ago.
    """
    if not LIBRARY.is_dir():
        return []
    busy = live_paths(queue)
    files = [p for p in LIBRARY.iterdir()
             if p.is_file() and p.suffix.lower() in MEDIA
             and str(p) not in busy]
    playable = sorted((p for p in files if prep.remux_verdict(p)),
                      key=lambda p: p.stat().st_mtime)
    # the newest playable ones are off the table entirely, whatever the disk says
    protected = set(playable[-MIN_PLAYABLE_FILES:])
    return sorted((p for p in files if p not in protected),
                  key=lambda p: (p in set(playable), p.stat().st_mtime))


def main(argv):
    apply = "--apply" in argv
    queue = common.load_queue()
    before = shutil.disk_usage(LIBRARY).free
    total = len([p for p in LIBRARY.iterdir()
                 if p.is_file() and p.suffix.lower() in MEDIA]) if LIBRARY.is_dir() else 0

    over = max(0, total - MAX_LIBRARY_FILES)
    print(f"libre={before / 1024 ** 3:.1f}G cible={TARGET_FREE_BYTES / 1024 ** 3:.1f}G "
          f"bibliotheque={total} plafond={MAX_LIBRARY_FILES} "
          f"reserve={int(prep.runway_seconds() / 60)}min "
          f"plancher={MIN_RUNWAY_SECONDS // 60}min")
    if before >= TARGET_FREE_BYTES and over == 0:
        print("rien a faire")
        return 0

    freed = 0
    kept = total
    for path in retirable(queue):
        # keep going while either the disk or the ceiling asks for it
        if before + freed >= TARGET_FREE_BYTES and kept <= MAX_LIBRARY_FILES:
            break
        left = prep.runway_seconds(ignoring=path)
        if left < MIN_RUNWAY_SECONDS:
            print(f"plancher atteint: sans {path.name[:40]} il resterait "
                  f"{int(left / 60)} min a diffuser, il manque encore "
                  f"{(TARGET_FREE_BYTES - before - freed) / 1024 ** 3:.1f}G")
            print("-> le disque ne peut pas etre tenu en retirant des VOD seuls")
            return 1
        size = path.stat().st_size
        print(f"  {'retire' if apply else 'retirerait'} {size / 1024 ** 3:.2f}G  {path.name[:60]}")
        if apply:
            path.unlink(missing_ok=True)
        freed += size
        kept -= 1

    after = shutil.disk_usage(LIBRARY).free if apply else before + freed
    print(f"{'libere' if apply else 'liberable'}: {freed / 1024 ** 3:.2f}G, "
          f"libre {'maintenant' if apply else 'deviendrait'} {after / 1024 ** 3:.1f}G, "
          f"{kept} fichiers restants")
    if not apply:
        print("essai a blanc, rien n'a ete supprime. --apply pour agir.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
