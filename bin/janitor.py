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

LIBRARY = pathlib.Path("/home/ubuntu/videos")
MEDIA = (".mp4", ".mkv")
# Comfortably above prep's MIN_FREE_BYTES: reaching prep's floor is the failure,
# so the janitor has to act well before it.
TARGET_FREE_BYTES = 8 * 1024 ** 3
# Below this the rotation gets thin enough to be noticeable, and a disk that
# still cannot be satisfied is a problem to report rather than to keep cutting.
MIN_LIBRARY_FILES = 40


def free_bytes():
    return shutil.disk_usage(LIBRARY).free


def live_paths(queue):
    """Files a queue entry still needs. Retiring one of these cuts a video off
    mid-rotation, or leaves prep pointed at a path that no longer exists."""
    return {i.get("path") for i in queue["items"]
            if i.get("status") in ("pending", "preparing", "ready")}


def retirable(queue):
    """Eligible files, oldest first."""
    if not LIBRARY.is_dir():
        return []
    busy = live_paths(queue)
    files = [p for p in LIBRARY.iterdir()
             if p.is_file() and p.suffix.lower() in MEDIA
             and str(p) not in busy]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def main(argv):
    apply = "--apply" in argv
    queue = common.load_queue()
    before = free_bytes()
    total = len([p for p in LIBRARY.iterdir()
                 if p.is_file() and p.suffix.lower() in MEDIA]) if LIBRARY.is_dir() else 0

    print(f"libre={before / 1024 ** 3:.1f}G cible={TARGET_FREE_BYTES / 1024 ** 3:.1f}G "
          f"bibliotheque={total} fichiers plancher={MIN_LIBRARY_FILES}")
    if before >= TARGET_FREE_BYTES:
        print("rien a faire")
        return 0

    freed = 0
    kept = total
    for path in retirable(queue):
        if before + freed >= TARGET_FREE_BYTES:
            break
        if kept <= MIN_LIBRARY_FILES:
            print(f"plancher atteint a {kept} fichiers, il manque encore "
                  f"{(TARGET_FREE_BYTES - before - freed) / 1024 ** 3:.1f}G")
            print("-> le disque ne peut pas etre tenu en retirant des VOD seuls")
            return 1
        size = path.stat().st_size
        print(f"  {'retire' if apply else 'retirerait'} {size / 1024 ** 3:.2f}G  {path.name[:60]}")
        if apply:
            path.unlink(missing_ok=True)
        freed += size
        kept -= 1

    after = free_bytes() if apply else before + freed
    print(f"{'libere' if apply else 'liberable'}: {freed / 1024 ** 3:.2f}G, "
          f"libre {'maintenant' if apply else 'deviendrait'} {after / 1024 ** 3:.1f}G, "
          f"{kept} fichiers restants")
    if not apply:
        print("essai a blanc, rien n'a ete supprime. --apply pour agir.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
