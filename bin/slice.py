#!/usr/bin/env python3
"""Cut long library files into parts the rotation can spread out.

    python3 bin/slice.py            say what it would do, change nothing
    python3 bin/slice.py --apply    do it

Most of what these channels are fed is six to twelve hours long. Aired whole,
one of them is most of a day on the same evening, and a viewer who comes back
twice sees the same stream both times. The draw already knows how to deal a
video's parts evenly across a pass rather than back to back, and prep, the
janitor and the collector already treat a part as an ordinary library file. The
only thing missing was something to make the parts.

Not done at fetch time, and that is not an accident. Asking the downloader for a
byte range hands the request to ffmpeg without its own headers and the CDN
refuses it, every time, so a long video has to arrive whole. It is cut here,
once, with a copy: about ten times real time, against 0.06x for a re-encode.

What this refuses to do matters more than what it does. It never touches a file
that is on air or holds chunks, it never deletes an original until every part it
produced has been probed and measured against it, and it never starts a job the
disk cannot hold, because a cut needs room for the copy before the original goes.
"""
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import common
import prep

# 50 minutes. Long enough that a part is still a stretch of stream rather than a
# clip, short enough that a twelve hour source becomes fifteen entries the draw
# can scatter through a day instead of one block nobody outlasts.
SLICE_SECONDS = int(os.environ.get("VODLOOP_SLICE_SECONDS") or 3000)

# Below this there is nothing to gain: a 55 minute file cut at 50 leaves a five
# minute tail, which is a worse rotation entry than the whole thing was.
SLICE_MIN_RATIO = 1.5

# Room for the copy before the original can go, plus what the encoder needs to
# keep working while it happens.
SLICE_HEADROOM_BYTES = 2 * 1024 ** 3


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def busy_paths():
    """Files the channel is holding: on air, or with chunks waiting to play."""
    try:
        queue = json.loads((common.STATE / "queue.json").read_text())
        items = queue.get("items", []) if isinstance(queue, dict) else queue
    except (OSError, ValueError):
        return set()
    return {i["path"] for i in items
            if i.get("path") and list(common.SEGMENTS.glob("%05d_*.ts" % i["id"]))}


def candidates():
    """Library files long enough to be worth cutting, and safe to cut now."""
    busy = busy_paths()
    out = []
    for path in sorted(common.LIBRARY_DIR.glob("*")):
        if not path.is_file() or path.suffix.lower() not in common.MEDIA_SUFFIXES:
            continue
        if prep.PART.search(path.name):
            continue                      # already a part, leave it alone
        if str(path) in busy:
            continue                      # on air or holding chunks
        # the draw identifies a video by the id at the end of its name, so a file
        # without one cannot be cut into parts that stay grouped
        if not prep.UNUSABLE_ID.search(path.name):
            continue
        seconds = duration(path)
        if seconds < SLICE_SECONDS * SLICE_MIN_RATIO:
            continue
        out.append((path, seconds))
    return out


def cut(path, seconds, apply):
    """Cut one file into parts. Returns the number of parts, or 0."""
    # Even parts, not fifty minutes and whatever is left. Cutting 2.56 h at a
    # flat fifty gave three full parts and a stub of three and a half minutes,
    # which is a rotation entry nobody wants and a needless handover. Dividing
    # the video by the number of parts it needs gives four of thirty eight
    # minutes instead, all under the ceiling and none of them a stub.
    wanted = math.ceil(seconds / SLICE_SECONDS)
    chaque = math.ceil(seconds / wanted)
    size = path.stat().st_size
    free = shutil.disk_usage(common.LIBRARY_DIR).free
    if free - size < SLICE_HEADROOM_BYTES:
        print(f"  {path.name[:52]}: pas la place ({free / 1024 ** 3:.1f}G libres, "
              f"il en faut {(size + SLICE_HEADROOM_BYTES) / 1024 ** 3:.1f}G)")
        return 0
    if not apply:
        print(f"  couperait {path.name[:52]} ({seconds / 3600:.2f} h) "
              f"en {wanted} parties de {chaque // 60} min")
        return 0

    work = pathlib.Path(tempfile.mkdtemp(dir=str(common.LIBRARY_DIR), prefix=".decoupe-"))
    try:
        done = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-c", "copy", "-f", "segment", "-segment_time", str(chaque),
             "-segment_format", "matroska", "-reset_timestamps", "1",
             str(work / "p%03d.mkv")],
            capture_output=True, text=True, timeout=4 * 3600)
        pieces = sorted(work.glob("p*.mkv"))
        if done.returncode != 0 or len(pieces) < 2:
            print(f"  {path.name[:52]}: echec "
                  f"({(done.stderr or '').strip().splitlines()[-1:]})")
            return 0

        # measured against the original before anything is deleted: a cut that
        # lost a chunk of the stream must not be allowed to replace it
        total = sum(duration(p) for p in pieces)
        if total < seconds * 0.98:
            print(f"  {path.name[:52]}: les parties ne totalisent que "
                  f"{total / 3600:.2f} h sur {seconds / 3600:.2f} h, on garde l'original")
            return 0

        made = []
        for n, piece in enumerate(pieces, 1):
            final = common.LIBRARY_DIR / f"{path.stem}.p{n:02d}of{len(pieces):02d}{path.suffix}"
            piece.replace(final)
            made.append(final)

        # every part has to be something prep can actually copy out again, or the
        # cut has quietly turned one playable file into several unplayable ones
        bad = [f for f in made if not prep.remux_verdict(f)]
        if bad:
            print(f"  {path.name[:52]}: {len(bad)} partie(s) non copiables, "
                  "on defait et on garde l'original")
            for f in made:
                f.unlink(missing_ok=True)
            return 0

        path.unlink()
        print(f"  {path.name[:52]} ({seconds / 3600:.2f} h) -> {len(made)} parties")
        return len(made)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv):
    apply = "--apply" in argv
    if common.LIBRARY_DIR is None or not common.LIBRARY_DIR.is_dir():
        print("pas de bibliotheque, rien a faire")
        return 0
    found = candidates()
    if not found:
        print("aucun fichier assez long a couper")
        return 0
    print(f"{len(found)} fichier(s) a couper en tranches de "
          f"{SLICE_SECONDS // 60} min")
    parts = 0
    for path, seconds in found:
        parts += cut(path, seconds, apply)
    print(f"{parts} partie(s) produite(s)"
          + ("" if apply else " -- essai a blanc, --apply pour agir"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
