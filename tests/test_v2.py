#!/usr/bin/env python3
"""vodloop v2. Run: python3 tests/test_v2.py

The pure rules first, each with the case that must fail beside it, then one
real ffmpeg pass through the cutter: a playable file goes queue -> chunks ->
aired, a 50 fps one is refused and never reaches the wire.
"""
import os
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile

root = pathlib.Path(tempfile.mkdtemp(prefix="v2-")) / "chaine"
root.mkdir()
(root / "channel.env").write_text("MAXH=720\nBUDGET_GB=28\nWINDOW_HOURS=16\nMAX_FILE_GB=10\n"
                                  "FLOOR_GB=5\nMIN_SECONDS=3600\nMAX_SECONDS=43200\n")
os.environ["CHAN_ROOT"] = str(root)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "v2" / "oracle"))

import chan  # noqa: E402
import cut  # noqa: E402
import feed  # noqa: E402
import supply  # noqa: E402

failures = []
G = chan.GIB


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + str(detail) if detail else ''}")
    if not ok:
        failures.append(label)


print("names and shapes")
check("the id is read under a queue prefix", chan.video_id("1789-Titre_long-dQw4w9WgXcQ.mkv") == "dQw4w9WgXcQ")
check("an id may start with a dash", chan.video_id("1789-Kick_VOD--_urJXVT-Mk.mkv") == "-_urJXVT-Mk")
check("control: a name without an id has none", chan.video_id("filler.ts") is None)
good = {"vcodec": "h264", "width": 1280, "height": 720, "fps": 30.0, "acodec": "aac",
        "rate": 44100, "channels": 2, "seconds": 7200}
check("720p30 h264 aac is copyable", chan.shape_problem(good) is None)
check("50 fps is not", chan.shape_problem(dict(good, fps=50.0)) is not None)
check("360p is not", chan.shape_problem(dict(good, width=640, height=360)) is not None)
check("vp9 is not", chan.shape_problem(dict(good, vcodec="vp9")) is not None)
check("aac 44.1 stereo is copied", chan.audio_copies(good))
check("control: 48 kHz is transcoded", not chan.audio_copies(dict(good, rate=48000)))
parsed = chan.parse_probe({"streams": [
    {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720, "r_frame_rate": "30000/1001"},
    {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 2}],
    "format": {"duration": "12.5"}})
check("ffprobe json is read", parsed["fps"] == 29.97 and parsed["seconds"] == 12.5, parsed)

print("supply: the one rule for disk")
aired = [(f"a{n}", 3 * G, 100 + n) for n in range(5)]  # a0 aired longest ago
need, offer, evict = supply.plan(16 * G, 16 * 3600, 1 * G, aired, free=20 * G)
check("a full window asks for nothing and evicts nothing", (need, offer, evict) == (0, 0, []))
# the 2026-09-17 state that deadlocked v1: 23 GB held, all of it aired, room wanted
need, offer, evict = supply.plan(0, 0, 2 * G, aired + [("a5", 6 * G, 106)], free=17 * G)
check("with the queue empty it asks for a full window", need == 16 * 3600, need)
check("and offers the largest file", offer == 10 * G, offer / G)
check("making room from the oldest aired files only", evict == ["a0", "a1"], evict)
need, offer, evict = supply.plan(20 * G, 10 * 3600, 1 * G, aired, free=40 * G)
check("the offer shrinks to what the share leaves", offer == 6 * G, offer / G)
check("never by evicting a waiting file", all(e.startswith("a") for e in evict))
need, offer, evict = supply.plan(0, 0, 0, [], free=8 * G)
check("the disk floor caps the offer", offer == 3 * G, offer / G)
need, offer, evict = supply.plan(27 * G, 3600, 1 * G, aired, free=10 * G)
check("control: a queue past the share gets no offer", offer == 0 and evict == [], (offer, evict))

print("supply: catalogue")
listing = ("dQw4w9WgXcQ\t7200\tDay 1 IRL\nshortvideo1\t600\tclip\nNAvideo0001\tNA\tlive\n"
           "longvideo01\t50000\ttoo long\nmatchvideo1\t7200\tOther stream\n")
kept = supply.parse_listing(listing, [])
check("only the band is kept", [v for v, _ in kept] == ["dQw4w9WgXcQ", "matchvideo1"], kept)
kept = supply.parse_listing(listing, ["irl"])
check("title words narrow it", [v for v, _ in kept] == ["dQw4w9WgXcQ"], kept)
check("sources carry their words",
      supply.parse_sources("# note\nhttps://a/videos\nhttps://b/search | Nana, IRL\n")
      == [("https://a/videos", []), ("https://b/search", ["nana", "irl"])])

print("cut: what airs next")
files = [(f"f{n}", 1000 + n) for n in range(6)]


class First:
    @staticmethod
    def choice(seq):
        return seq[-1]


check("a replay comes from the least recently aired half",
      cut.pick_replay(files, First) == "f2", cut.pick_replay(files, First))
check("any of that half can be drawn",
      {cut.pick_replay(files, random.Random(s)) for s in range(40)} == {"f0", "f1", "f2"})
check("nothing aired, nothing to replay", cut.pick_replay([]) is None)

print("feed: a session reopens only upward")
check("1080p in a 720p session", feed.exceeds((1920, 1080, 30), (1280, 720, 30)))
check("60 fps in a 30 fps session", feed.exceeds((1280, 720, 60), (1280, 720, 30)))
check("control: 720p30 in a 1080p60 session", not feed.exceeds((1280, 720, 30), (1920, 1080, 60)))

if shutil.which("ffmpeg"):
    print("cut: one real pass")
    for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED, chan.CHUNKS, chan.WORK, chan.STATE):
        folder.mkdir(parents=True, exist_ok=True)

    def make(name, rate):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"testsrc2=s=854x480:r={rate}:d=12", "-f", "lavfi",
                        "-i", "sine=f=440:sample_rate=44100:d=12", "-c:v", "libx264",
                        "-g", str(2 * rate), "-c:a", "aac", "-ac", "2", "-y",
                        str(chan.QUEUE / name)], check=True)

    make("1-Bonne_video-goodvideo01.mkv", 30)
    source, origin = cut.next_source()
    check("the oldest queued file is taken", origin == "queue" and source.parent == chan.CURRENT, source)
    cut.run_job(source, origin, 0)
    chunks = sorted(chan.CHUNKS.glob("*.ts"))
    check("it is cut into chunks", len(chunks) == 1, chunks)
    check("then it is aired", (chan.AIRED / source.name).exists())
    check("and remembered", "goodvideo01" in (chan.STATE / "aired.tsv").read_text())
    check("the chunk is h264 at the source's shape",
          chan.probe(chunks[0])["height"] == 480 if chunks else False)
    make("2-Mauvaise_video-badvideo001.mkv", 50)
    source, origin = cut.next_source()
    cut.run_job(source, origin, 0)
    check("a 50 fps file is refused", not source.exists()
          and "badvideo001" in (chan.STATE / "rejected.tsv").read_text())
    check("and adds no chunk", len(list(chan.CHUNKS.glob("*.ts"))) == 1)
    source, origin = cut.next_source()
    check("with the queue empty, the aired file comes back", origin == "aired", origin)
else:
    print("  (ffmpeg absent: real pass skipped)")

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
