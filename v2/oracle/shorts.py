#!/usr/bin/env python3
"""Turn a vertical short into something the wire can carry. By hand, or cron.

    python3 shorts.py                 what is ready, what it weighs
    python3 shorts.py --add FILE...   put each of these in the rotation

A short is filmed upright and the channel is not, so this is the one place the
picture has to be built rather than copied: scaled to fit, padded into the
channel's own frame, at the channel's own height and 60 i/s. Measured on Oracle
on 2026-09-21, a minute of it costs 58 s of a niced vCPU while the channel
streams, which is nothing like the 0.06x a full length re-encode costs and is
why this is allowed to exist at all.

Nothing here touches the wire. feed.py picks the oldest file in the directory
when bot.py asks for one, so a short prepared now goes out at the next junction
after the next vote, between two chunks and without cutting anything.
"""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402

# A short is a short. Anything longer is a video and belongs in the rotation,
# where it does not cost an encode.
MAX_SECONDS = int(chan.conf_num("SHORT_MAX_SECONDS", 90))
# Enough for a wait, few enough that the same one is not seen twice a day.
KEEP = int(chan.conf_num("SHORTS_KEPT", 20))


def ready():
    return sorted(chan.SHORTS.glob("*.ts"))


def prepare(source, into=None):
    """One file into the rotation, or None with a line saying why not."""
    info = chan.probe(source)
    if info is None:
        chan.log(f"short refuse, image illisible: {source.name}")
        return None
    seconds = chan.duration(source, {})
    if seconds > MAX_SECONDS:
        chan.log(f"short refuse, {seconds / 60:.1f} min pour un plafond "
                 f"de {MAX_SECONDS} s: {source.name}")
        return None
    size = {1080: "1920:1080", 480: "854:480"}.get(chan.MAXH, "1280:720")
    out = (into or chan.SHORTS) / (source.stem[:40] + ".ts")
    chan.SHORTS.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".ts.new")
    done = subprocess.run(
        ["nice", "-n", "19", "ffmpeg", "-hide_banner", "-loglevel", "error",
         "-i", str(source),
         # the same frame the standby clip is built at, so the session never
         # sees a picture it did not open on
         "-vf", f"scale={size}:force_original_aspect_ratio=decrease,"
                f"pad={size.replace(':', ':')}:(ow-iw)/2:(oh-ih)/2,fps=60",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
         "-pix_fmt", "yuv420p", "-g", "120", "-keyint_min", "120",
         "-sc_threshold", "0",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
         "-f", "mpegts", "-y", str(tmp)], capture_output=True)
    if done.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        chan.log(f"short refuse, ffmpeg: {source.name}")
        return None
    tmp.replace(out)
    chan.log(f"short pret: {out.name} ({seconds:.0f} s, "
             f"{out.stat().st_size / 2 ** 20:.1f} Mo)")
    return out


def retire():
    """Oldest out once there are more than KEEP: the rotation is by mtime."""
    files = sorted(chan.SHORTS.glob("*.ts"), key=lambda p: p.stat().st_mtime)
    for path in files[:max(0, len(files) - KEEP)]:
        path.unlink(missing_ok=True)
        chan.log(f"short retire: {path.name}")


def main(argv):
    chan.SHORTS.mkdir(parents=True, exist_ok=True)
    if "--add" in argv:
        for name in argv[argv.index("--add") + 1:]:
            prepare(pathlib.Path(name))
        retire()
    files = ready()
    total = sum(p.stat().st_size for p in files) / 2 ** 20
    print(f"{len(files)} shorts prets, {total:.1f} Mo, plafond {KEEP}")
    for path in files:
        print(f"  {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
