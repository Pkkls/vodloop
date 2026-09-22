#!/usr/bin/env python3
"""Lower the wire to the channel's ceiling, the moment it can be done safely.

    python3 ceiling.py            say whether it can happen, and what blocks it
    python3 ceiling.py --apply    do it when nothing blocks. From cron.

Kick fixes a session's ladder on its first picture, so the height the channel
serves is decided when the session opens and held until it closes. Changing
MAXH changes what is fetched within minutes and changes nothing on the wire: the
session keeps serving what it opened on, for days.

Making it take effect is three steps, and only the third is delicate. The clip
is rebuilt at the new ceiling, the session is reopened on it, and the pusher
restart that reopening costs is about six seconds off air.

The delicate part is the order. A file taller than the new ceiling that still
holds unaired time would reopen the session every ten minutes, for ever: the
chunk exceeds the session, the session reopens on the clip, the same chunk
exceeds it again. So the flip waits until nothing above the ceiling is left to
show. That wait is what this exists for, because it is measured in hours and
nobody watches a disk for hours.
"""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402
import cut  # noqa: E402
import feed  # noqa: E402

SIZES = {1080: "1920x1080", 720: "1280x720", 480: "854x480"}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def taller_than_ceiling():
    """(files, units) still holding unaired time above MAXH lines."""
    book, held = cut.ledger(), cut.reserved()
    durations = chan.read_json(chan.STATE / "durations.json", {})
    files, units = [], 0
    for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED):
        for path in chan.media(folder):
            free = len(cut.unaired(path, book, durations, held))
            if not free:
                continue
            info = chan.probe(path)
            if info and info["height"] > chan.MAXH:
                files.append(path.name)
                units += free
    return files, units


def build_clip(size):
    """The standby clip at the new ceiling, the same way install.sh builds it."""
    tmp = chan.FILLER.with_suffix(".ts.new")
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-t", "20",
         "-i", f"color=c=0x101014:s={size}:r=60,drawtext=fontfile={FONT}:"
               f"text='vod loading...':fontcolor=white@0.82:fontsize=h/16:"
               f"x=(w-text_w)/2:y=(h-text_h)/2",
         "-f", "lavfi", "-t", "20", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
         "-g", "120", "-keyint_min", "120", "-sc_threshold", "0",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
         "-f", "mpegts", "-y", str(tmp)], capture_output=True)
    if done.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(chan.FILLER)
    return True


def stale_shorts(probed, ceiling):
    """Built shorts that no longer fit the frame the channel serves.

    A short is padded into the channel's own frame rather than copied, so one
    built for the old ceiling can only ever be skipped: feed.py refuses
    anything above the session. Twenty-one of them were in that state on
    2026-09-22, which would have made the reward that plays one do nothing at
    all. Dropping them puts their ids back in front of the board, and the
    rotation refills by the ordinary path. (name, height) pairs in, pure.
    """
    return [name for name, height in probed if height != ceiling]


def state():
    """(clip height, blocking files, blocking units), all read and none assumed."""
    info = chan.probe(chan.FILLER)
    files, units = taller_than_ceiling()
    return (info["height"] if info else 0), files, units


def main(argv):
    apply = "--apply" in argv
    height, files, units = state()
    size = SIZES.get(chan.MAXH)
    if height == chan.MAXH:
        print(f"deja au plafond: clip {height} lignes, MAXH {chan.MAXH}")
        return 0
    if size is None:
        print(f"MAXH={chan.MAXH} sans taille connue, rien fait")
        return 1
    if units:
        print(f"bloque: {units} unites ({units * chan.CHUNK_SECONDS / 3600:.1f} h) "
              f"au-dessus de {chan.MAXH} lignes, dans {len(files)} fichier(s)")
        for name in files:
            print(f"  {name[:64]}")
        return 0
    print(f"rien au-dessus de {chan.MAXH} lignes: le fil peut passer de "
          f"{height} a {chan.MAXH}")
    if not apply:
        return 0
    if not build_clip(size):
        chan.log("bascule annulee: le clip n a pas ete construit")
        return 1
    # the session is reopened by restarting the one ffmpeg that holds it, which
    # is the only moment in this system where that is the right thing to do
    subprocess.run(["sudo", "-n", "systemctl", "restart", chan.unit("push")])
    feed.SESSION.unlink(missing_ok=True)
    chan.log(f"fil bascule en {size} a 60 i/s, session rouverte")
    gone = stale_shorts([(p.name, (chan.probe(p) or {}).get("height", 0))
                         for p in sorted(chan.SHORTS.glob("*.ts"))], chan.MAXH)
    for name in gone:
        (chan.SHORTS / name).unlink(missing_ok=True)
    if gone:
        chan.log(f"{len(gone)} short(s) au format d avant retires, la carte les refera")
    chan.telegram(f"le fil passe en {size}, session rouverte, environ 6 s hors ligne")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
