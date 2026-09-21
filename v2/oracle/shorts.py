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
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402

SOURCES = chan.ROOT / "shorts.txt"
WANTED = chan.STATE / "shorts.tsv"
YTDLP = shutil.which("yt-dlp") or str(pathlib.Path.home() / ".local/bin/yt-dlp")
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
    # named by the video id, which is what tells the board it already has this
    # one and stops it fetching the same short every hour
    out = (into or chan.SHORTS) / ((chan.video_id(source.name) or source.stem[:40]) + ".ts")
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


def collect():
    """Build every short the board has dropped, and take the source away.

    Called from supply.py, which already runs every ten minutes behind a lock,
    so an encode can never overlap another one however slow a file is.
    """
    made = []
    if not chan.SHORTS_IN.exists():
        return made
    for source in sorted(chan.SHORTS_IN.iterdir()):
        if not source.is_file():
            continue
        out = prepare(source)
        source.unlink(missing_ok=True)
        if out:
            made.append(out)
    if made:
        retire()
    return made


def wanted(now=None):
    """Write the ids the board should still fetch, most recent first.

    The channel's own shorts pages, minus what is already built and minus what
    is waiting to be built. Listing works from this address; only downloading is
    walled, which is why the board does that half.
    """
    have = {p.stem for p in chan.SHORTS.glob("*.ts")}
    have |= {chan.video_id(p.name) or p.stem for p in chan.SHORTS_IN.glob("*")
             if p.is_file()}
    rows, seen = [], set()
    for url in sources():
        for vid, secs, title in listing(url):
            if vid in have or vid in seen or secs > MAX_SECONDS:
                continue
            seen.add(vid)
            rows.append((vid, secs, title))
    rows = rows[:KEEP * 2]
    tmp = WANTED.with_suffix(".tmp")
    tmp.write_text("".join(f"{vid}\t{secs}\t{title}\n" for vid, secs, title in rows),
                   encoding="utf-8")
    tmp.replace(WANTED)
    chan.log(f"shorts: {len(ready())} prets, {len(rows)} a chercher")
    return rows


def sources():
    try:
        return [line.strip() for line in SOURCES.read_text().splitlines()
                if line.strip() and not line.startswith("#")]
    except OSError:
        return []


def listing(url):
    """(id, seconds, title) of one shorts page, newest first, or nothing."""
    try:
        out = subprocess.run([YTDLP, "--flat-playlist", "--no-warnings", "--print",
                              "%(id)s\t%(duration)s\t%(title)s", url],
                             capture_output=True, text=True, timeout=300).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or len(parts[0]) != 11:
            continue
        try:
            secs = int(float(parts[1]))
        except ValueError:
            secs = 0  # a shorts page often lists no duration, and a short is short
        rows.append((parts[0], secs, re.sub(r"\s+", " ", parts[2]).strip()[:80]))
    return rows


def main(argv):
    chan.SHORTS.mkdir(parents=True, exist_ok=True)
    if "--add" in argv:
        for name in argv[argv.index("--add") + 1:]:
            prepare(pathlib.Path(name))
        retire()
    if "--collect" in argv:
        collect()
    if "--list" in argv:
        wanted()
    files = ready()
    total = sum(p.stat().st_size for p in files) / 2 ** 20
    print(f"{len(files)} shorts prets, {total:.1f} Mo, plafond {KEEP}")
    for path in files:
        print(f"  {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
