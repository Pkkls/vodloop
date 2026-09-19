#!/usr/bin/env python3
"""vodloop v2. Run: python3 tests/test_v2.py

The pure rules first, each with the case that must fail beside it, then one
real ffmpeg pass through the cutter: a playable file goes queue -> chunks ->
aired, a 50 fps one is refused and never reaches the wire.
"""
import os
import pathlib
import shutil
import subprocess
import time
import sys
import tempfile

root = pathlib.Path(tempfile.mkdtemp(prefix="v2-")) / "chaine"
root.mkdir()
(root / "channel.env").write_text("MAXH=720\nBUDGET_GB=28\nWINDOW_HOURS=16\nMAX_FILE_GB=10\n"
                                  "FLOOR_GB=5\nMIN_SECONDS=3600\nMAX_SECONDS=43200\n")
os.environ["CHAN_ROOT"] = str(root)
here = pathlib.Path(__file__).resolve().parents[1]
# the repo keeps the code in v2/oracle, the server installs it in bin/
sys.path[:0] = [str(here / "v2" / "oracle"), str(here / "bin")]

import chan  # noqa: E402
import cut  # noqa: E402
import feed  # noqa: E402
import kick  # noqa: E402
import kickapi  # noqa: E402
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
check("only the band is kept", [r[0] for r in kept] == ["dQw4w9WgXcQ", "matchvideo1"], kept)
check("and the title comes with it, so a place can be looked up later",
      kept[0][2] == "Day 1 IRL", kept[0])
kept = supply.parse_listing(listing, ["irl"])
check("title words narrow it", [r[0] for r in kept] == ["dQw4w9WgXcQ"], kept)
supply.CATALOG.parent.mkdir(parents=True, exist_ok=True)
supply.CATALOG.write_text(
    "dQw4w9WgXcQ\t7200\t0\t0\tDay 7 IRL Cappadocia Turkey\n"
    "older000001\t7200\t0\t1\tDay 2 IRL Cusco Peru\n")
check("the catalogue answers on a place now",
      [v for v, t in supply.catalog_titles().items() if "turkey" in t.lower()]
      == ["dQw4w9WgXcQ"], supply.catalog_titles())
check("sources carry their words",
      supply.parse_sources("# note\nhttps://a/videos\nhttps://b/search | Nana, IRL\n")
      == [("https://a/videos", []), ("https://b/search", ["nana", "irl"])])

print("supply: how long a video the board may be asked for")
real_duration, real_size = chan.duration, chan.size_of
try:
    lengths = {"a": 3600.0, "b": 7200.0}
    sizes = {"a": 800 * 1024 ** 2, "b": 1600 * 1024 ** 2}
    chan.duration = lambda p, cache=None: lengths[str(p)]
    chan.size_of = lambda p: sizes[str(p)]
    rate = supply.measured_rate(["a", "b"], {})
    check("the rate is measured on what landed", round(rate / 1e6, 2) == 0.23, rate)
    chan.duration = lambda p, cache=None: 30.0
    check("control: too little measured falls back",
          supply.measured_rate(["a"], {}) == 250_000)
finally:
    chan.duration, chan.size_of = real_duration, real_size
check("a thick channel may only be asked for short videos",
      supply.fetch_ceiling(1e6) == int(10 * G / 1e6), supply.fetch_ceiling(1e6))
check("control: a thin one is bounded by the band, not by the card",
      supply.fetch_ceiling(233016) == chan.MAX_SECONDS)
supply.CATALOG.parent.mkdir(parents=True, exist_ok=True)
supply.CATALOG.write_text("dQw4w9WgXcQ\t7200\t0\t3\nbroken00001\tNA\t0\t4\nolder000001\t7200\n")
check("the catalogue comes back with numbers, not text",
      supply.read_catalog() == [("dQw4w9WgXcQ", 7200, 0, 3), ("older000001", 7200, 0, 1)],
      supply.read_catalog())

print("supply: what the board is not offered again")
(chan.STATE).mkdir(parents=True, exist_ok=True)
now = 1789000000
rows = {"aired.tsv": [(now - 3600, "aired000001"), (now - 40 * 86400, "olddvideo01")],
        "rejected.tsv": [(now - 86400, "refused0001")],
        "failed.tsv": [(now - 86400, "flaky000001")]}
def write_ledgers(rows):
    for name, entries in rows.items():
        (chan.STATE / name).write_text(
            "".join("%d\t%s\tnote\n" % (at, vid) for at, vid in entries))


write_ledgers(rows)
skip = supply.excluded(now)
check("aired within the refetch window stays out", "aired000001" in skip)
check("aired long ago comes back", "olddvideo01" not in skip, sorted(skip))
check("a shape the wire refuses never comes back", "refused0001" in skip)
check("one ffmpeg failure is forgiven", "flaky000001" not in skip)
write_ledgers({"failed.tsv": [(now - 86400, "flaky000001"), (now, "flaky000001")]})
check("control: twice is not", "flaky000001" in supply.excluded(now))
for name in ("aired.tsv", "rejected.tsv", "failed.tsv"):
    (chan.STATE / name).unlink()

print("supply: the recent material of every source comes first")
rows = [("oldA0000001", 3600, 0, 2), ("newA0000001", 3600, 0, 0),
        ("midA0000001", 3600, 0, 1), ("newB0000001", 3600, 1, 0),
        ("oldB0000001", 3600, 1, 1)]
order = [vid for vid, _ in supply.newest_first(rows)]
check("sources are taken in turn, newest of each first",
      order == ["newA0000001", "newB0000001", "midA0000001", "oldB0000001", "oldA0000001"],
      order)
check("control: nothing is lost on the way", len(order) == len(rows))

print("kick: reading a channel's own VODs")
master = ("#EXTM3U\n"
          "#EXT-X-STREAM-INF:BANDWIDTH=7624377,RESOLUTION=1920x1080,FRAME-RATE=30.000\n"
          "1080p/playlist.m3u8\n"
          "#EXT-X-STREAM-INF:BANDWIDTH=2439511,RESOLUTION=1280x720,FRAME-RATE=30.000\n"
          "720p30/playlist.m3u8\n"
          "#EXT-X-STREAM-INF:BANDWIDTH=630000,RESOLUTION=640x360,FRAME-RATE=30.000\n"
          "360p30/playlist.m3u8\n")
check("the tallest rung under the ceiling is taken",
      kick.parse_master(master, 720) == ("720p30/playlist.m3u8", 2439511, 720),
      kick.parse_master(master, 720))
check("control: a taller ceiling takes the source rung",
      kick.parse_master(master, 1080)[2] == 1080)
check("control: nothing at all under 480 lines", kick.parse_master(master, 360) is None)

print("the floor is the channel's, not a constant")
real_minh = chan.MINH
try:
    chan.MINH = 720
    check("a 720p floor refuses a 480p file",
          chan.shape_problem(dict(good, width=854, height=480)) is not None)
    check("control: and still takes 720p", chan.shape_problem(good) is None)
    check("the Kick rung obeys the same floor", kick.parse_master(master, 1080, 720)[2] == 1080)
    check("control: a recording Kick only serves at 360 is not fetched at all",
          kick.parse_master("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=630000,RESOLUTION=640x360\n"
                            "360p30/playlist.m3u8\n", 1080, 720) is None)
finally:
    chan.MINH = real_minh
check("control: the default floor still takes 480p", chan.shape_problem(
    dict(good, width=854, height=480)) is None)
media = "#EXTM3U\n#EXTINF:10.000,\n0.ts\n#EXTINF:10.000,\n1.ts\n#EXTINF:4.000,\n2.ts\n"
check("segments come back in order with their length",
      kick.parse_media(media) == [("0.ts", 10.0), ("1.ts", 10.0), ("2.ts", 4.0)])
check("a part takes the segments that start inside it",
      kick.segments_for(kick.parse_media(media), 10, 20) == ["1.ts"])
check("control: the whole recording takes them all",
      kick.segments_for(kick.parse_media(media), 0, 30) == ["0.ts", "1.ts", "2.ts"])
check("a short recording is one part", kick.slice_parts(3600, 7200) == [(0, 3600)])
cuts = kick.slice_parts(36000, 7200)
check("a ten hour one is cut in equal parts that cover it",
      len(cuts) == 5 and cuts[0][0] == 0 and cuts[-1][1] == 36000, cuts)
check("no gap between two parts", all(a[1] == b[0] for a, b in zip(cuts, cuts[1:])), cuts)
ids = {kick.part_id("uuid-abc", n) for n in range(1, 6)}
check("every part has its own id, of the right shape",
      len(ids) == 5 and all(len(i) == 11 and chan.VIDEO_ID.search("x-" + i + ".mkv") for i in ids),
      sorted(ids))
check("control: the same part keeps its id",
      kick.part_id("uuid-abc", 2) == kick.part_id("uuid-abc", 2))
check("a title becomes a file name", kick.safe_title("Day 7: Jaipur / India!") == "Day_7_Jaipur_India")

print("kick: which part is fetched next")
import kickfetch  # noqa: E402

supply.CATALOG.write_text("kaaaaaaaa01\t7200\t0\t0\nyoutubevid1\t7200\t1\t0\n"
                          "kaaaaaaaa02\t7200\t0\t1\n")
kick.write_table([("kaaaaaaaa01", "https://cdn/1.m3u8", 0, 7200, "Jour_1", 6000000),
                  ("kaaaaaaaa02", "https://cdn/1.m3u8", 7200, 14400, "Jour_1", 6000000)])
chosen = kickfetch.pick(now=1789000000)
check("the newest Kick part comes first, YouTube is left to the board",
      chosen and chosen[0] == "kaaaaaaaa01", chosen)
check("the part carries the bitrate it will really weigh at",
      chosen and chosen[6] == 6000000, chosen)
write_ledgers({"aired.tsv": [(1789000000 - 60, "kaaaaaaaa01")]})
chosen = kickfetch.pick(now=1789000000)
check("a part already aired is never fetched again",
      chosen and chosen[0] == "kaaaaaaaa02", chosen)
write_ledgers({"aired.tsv": [(1789000000 - 60, "kaaaaaaaa01"),
                             (1789000000 - 30, "kaaaaaaaa02")]})
check("control: with both aired there is nothing to take",
      kickfetch.pick(now=1789000000) is None)
(chan.STATE / "aired.tsv").unlink()
kick.TABLE.unlink()

print("kick: this line fills its share and leaves the rest to the board")
real_media, real_duration = chan.media, chan.duration
try:
    held = [pathlib.Path("1789-Stream-kaaaaaaaa01.mkv"), pathlib.Path("1789-Yt-youtubevid1.mkv")]
    chan.media = lambda folder: held if pathlib.Path(folder) == chan.QUEUE else []
    chan.duration = lambda p, cache=None: 7200.0
    table = {"kaaaaaaaa01": ()}
    check("only Kick material counts against the Kick share",
          kickfetch.kick_hours_queued(table) == 7200.0, kickfetch.kick_hours_queued(table))
    was = chan.PART_SECONDS
    chan.PART_SECONDS = 3600
    cut.set_part("1789-Stream-kaaaaaaaa01.mkv", {0})
    check("an hour of it already aired does not count twice",
          kickfetch.kick_hours_queued(table) == 3600.0, kickfetch.kick_hours_queued(table))
    chan.PART_SECONDS = was
    cut.PARTS.unlink(missing_ok=True)
    check("control: a channel with no Kick file leaves the whole window free",
          kickfetch.kick_hours_queued({}) == 0.0)
finally:
    chan.media, chan.duration = real_media, real_duration

print("feed: a session reopens only upward")
check("1080p in a 720p session", feed.exceeds((1920, 1080, 30), (1280, 720, 30)))
check("60 fps in a 30 fps session", feed.exceeds((1280, 720, 60), (1280, 720, 30)))
check("control: 720p30 in a 1080p60 session", not feed.exceeds((1280, 720, 30), (1920, 1080, 60)))

print("feed: a session is opened on the standby clip, so nothing can end it")
real_profile = feed.profile_of
try:
    feed.profile_of = lambda path: (1280, 720, 60.0)
    feed.SESSION.unlink(missing_ok=True)
    check("a new pusher fixes the ladder on the clip", feed.open_session(4242))
    check("and the session carries the clip's shape",
          chan.read_json(feed.SESSION, {})["profile"] == [1280, 720, 60.0],
          chan.read_json(feed.SESSION, {}))
    check("control: the same pusher does not reopen it", not feed.open_session(4242))
    check("a new one does", feed.open_session(4243))
    # the cut this whole change exists to stop: 30 fps clip, 60 fps chunk
    feed.profile_of = lambda path: (1280, 720, 30.0)
    feed.open_session(4244)
    feed.profile_of = lambda path: (1280, 720, 60.0)
    check("control: a session opened below the material would be ended",
          feed.exceeds((1280, 720, 60.0),
                       tuple(chan.read_json(feed.SESSION, {})["profile"])))
finally:
    feed.profile_of = real_profile
    feed.SESSION.unlink(missing_ok=True)

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
    check("control: what aired is kept in reserve, not thrown away",
          len(chan.media(chan.AIRED)) == 1, chan.media(chan.AIRED))
    check("unsliced, an empty queue draws nothing rather than replay",
          cut.next_source() == (None, None), cut.next_source())

    print("cut: the three tiers, and no hour twice")
    was = chan.PART_SECONDS
    try:
        chan.PART_SECONDS = 5
        cut.HOURS.unlink(missing_ok=True)
        cut.PARTS.unlink(missing_ok=True)
        spare = chan.media(chan.AIRED)[0]
        source, origin = cut.next_source()
        check("with an empty queue the reserve is drawn, not the standby clip",
              source is not None and origin == "reserve", (source, origin))
        check("and it is the file that still holds unseen hours", source.name == spare.name)
        check("what the board brought back is preferred to the Kick line",
              cut.from_board(pathlib.Path("1789-Titre-dQw4w9WgXcQ.mkv"))
              and not cut.from_board(pathlib.Path("1789-Titre-kb97ce32702.mkv")))
        # every hour of it on the wire: the reserve has nothing unseen left
        for n in range(cut.slices_in(chan.duration(source))):
            cut.record_hour(source.name, n)
        source.replace(chan.AIRED / source.name)
        check("control: a spent file offers no unaired hour",
              cut.unaired(chan.AIRED / source.name, cut.ledger(), {}) == set())
        check("with every hour spent, nothing is drawn rather than repeated",
              cut.next_source() == (None, None), cut.next_source())
        for leftover in chan.media(chan.AIRED):
            leftover.unlink()
        check("control: and an empty disk answers the same way",
              cut.next_source() == (None, None))
    finally:
        chan.PART_SECONDS = was
        cut.HOURS.unlink(missing_ok=True)
else:
    print("  (ffmpeg absent: real pass skipped)")

print("cut: an hour at a time, taken from anywhere in the file")
real_part = chan.PART_SECONDS
a = pathlib.Path("a.mkv")
try:
    chan.PART_SECONDS = 0
    check("unsliced, a file airs whole", cut.window(a, 18000) == (0.0, 18000, 0))
    chan.PART_SECONDS = 3600
    cut.PARTS.unlink(missing_ok=True)
    check("a five hour file holds five hours", cut.slices_in(18000) == 5)
    check("and a stub is folded into the last, not counted as a sixth",
          cut.slices_in(18120) == 5, cut.slices_in(18120))
    check("control: a file shorter than a slice still holds one", cut.slices_in(600) == 1)
    draws = {cut.window(a, 18000)[2] for _ in range(200)}
    check("the hour is drawn from anywhere in the file, not from the start",
          draws == {0, 1, 2, 3, 4}, sorted(draws))
    starts = {cut.window(a, 18000)[0] for _ in range(200)}
    check("and a draw lands where its hour begins",
          starts == {0.0, 3600.0, 7200.0, 10800.0, 14400.0}, sorted(starts))
    cut.set_part("a.mkv", {0, 1, 2, 4})
    check("an hour already on the wire is never drawn again",
          {cut.window(a, 18000)[2] for _ in range(50)} == {3})
    cut.set_part("a.mkv", {0, 1, 2, 3})
    check("the last hour runs to the end of the file, stub included",
          cut.window(a, 18120) == (14400.0, 18120 - 14400.0, 4), cut.window(a, 18120))
    cut.set_part("a.mkv", 10800.0)
    check("the cursor the old format held reads as the hours it had played",
          cut.played("a.mkv") == {0, 1, 2}, cut.played("a.mkv"))
    cut.PARTS.unlink(missing_ok=True)
finally:
    chan.PART_SECONDS = real_part

if shutil.which("ffmpeg"):
    print("cut: a sliced file goes back in the queue until it is spent")
    real_tail = cut.TAIL_SECONDS
    try:
        chan.PART_SECONDS, cut.TAIL_SECONDS = 5, 4
        for stale in chan.media(chan.AIRED):
            stale.unlink()
        (chan.STATE / "aired.tsv").unlink(missing_ok=True)
        make("3-Longue_video-partvideo01.mkv", 30)
        source, origin = cut.next_source()
        cut.run_job(source, origin, 0)
        back = chan.QUEUE / source.name
        check("after its first hour it is back in the queue", back.exists())
        check("with that hour written down and no other",
              len(cut.played(source.name)) == 1, cut.played(source.name))
        check("and it has not been recorded as aired",
              not (chan.STATE / "aired.tsv").exists())
        source, origin = cut.next_source()
        cut.run_job(source, origin, 0)
        check("the last hour retires it", (chan.AIRED / back.name).exists()
              and "partvideo01" in (chan.STATE / "aired.tsv").read_text())
        check("control: the ledger keeps every hour, so none comes back",
              len(cut.played(back.name)) == cut.slices_in(chan.duration(chan.AIRED / back.name)),
              cut.played(back.name))
    finally:
        chan.PART_SECONDS, cut.TAIL_SECONDS = real_part, real_tail


print("bot: reading the pipeline")
import bot  # noqa: E402

check("a file name becomes something a viewer can read",
      bot.pretty("1789784406-260120_nanatty_-_Day_2_IRL_Santiago-JEELzhY-PGQ.mkv")
      == "260120 nanatty - Day 2 IRL Santiago",
      bot.pretty("1789784406-260120_nanatty_-_Day_2_IRL_Santiago-JEELzhY-PGQ.mkv"))
check("control: a name with neither prefix nor id survives it",
      bot.pretty("clip.mkv") == "clip")
chan.write_json(bot.feed.ONAIR, {"source": "1789-Un_Titre-dQw4w9WgXcQ.mkv",
                                 "number": 2, "seconds": 18000, "at": time.time() - 900})
was = chan.PART_SECONDS
chan.PART_SECONDS = 3600
live = bot.playing()
chan.PART_SECONDS = was
check("what is on the wire is read from the cutter's own job",
      live["hour"] == 3 and live["hours"] == 5 and 890 < live["elapsed"] < 960, live)
check("and its source is named", live["vid"] == "dQw4w9WgXcQ")

print("bot: the guards on skipping")
real_unseen = bot.unseen_hours
try:
    bot.unseen_hours = lambda: 0
    check("no vote opens when there is nothing unseen to move on to",
          bot.skip_blocked({"skips": []}, 1000000, None) is not None)
    bot.unseen_hours = lambda: 5
    check("control: with material in hand the hour itself is the only gate",
          bot.skip_blocked({"skips": []}, 1000000, None) is None)
    fresh = {"elapsed": 60, "hour": 1, "hours": 5}
    check("an hour cannot be voted off in its first minutes",
          "votable in" in (bot.skip_blocked({"skips": []}, 1000000, fresh) or ""))
    settled = {"elapsed": bot.SKIP_MIN_AIRED + 1, "hour": 1, "hours": 5}
    check("control: once it has run long enough it can",
          bot.skip_blocked({"skips": []}, 1000000, settled) is None)
    just = {"skips": [1000000 - 60]}
    check("a skip locks the next one for the cooldown",
          "next vote possible" in (bot.skip_blocked(just, 1000000, settled) or ""))
    many = {"skips": [1000000 - 100 * n for n in range(1, bot.SKIP_MAX_PER_HOUR + 1)]}
    check("and an hour holds only so many of them",
          "that is the limit" in (bot.skip_blocked(many, 1000000, settled) or ""),
          bot.skip_blocked(many, 1000000, settled))
    old = {"skips": [1000000 - 7000]}
    check("control: skips older than the hour do not count",
          bot.skip_blocked(old, 1000000, settled) is None)
finally:
    bot.unseen_hours = real_unseen

print("bot: one voice per account, and the skip only at the threshold")
real_threshold, real_viewers = bot.threshold, bot.unseen_hours
try:
    bot.threshold = lambda: 3
    bot.unseen_hours = lambda: 5
    bot.SKIP.unlink(missing_ok=True)
    data = {"skips": [], "vote": None, "users": {}, "seen": []}
    settled = {"elapsed": bot.SKIP_MIN_AIRED + 1, "hour": 1, "hours": 5}
    real_playing = bot.playing
    bot.playing = lambda: settled
    now = 2000000
    first = bot.cmd_vote(data, now, {"user_id": "u1", "privileged": False}, [])
    check("the first voice opens the vote and says what it needs",
          "3 votes needed" in (first or ""), first)
    again = bot.cmd_vote(data, now + 1, {"user_id": "u1", "privileged": False}, [])
    check("the same account cannot vote twice", again is None and len(data["vote"]["voters"]) == 1)
    bot.cmd_vote(data, now + 2, {"user_id": "u2", "privileged": False}, [])
    check("a second voice counts but does not carry it",
          len(data["vote"]["voters"]) == 2 and not bot.SKIP.exists())
    done = bot.cmd_vote(data, now + 3, {"user_id": "u3", "privileged": False}, [])
    check("the third carries it, and the cutter is told", bot.SKIP.exists(), done)
    check("and the vote is closed behind it", data["vote"] is None)
    check("control: the skip is written down so the next one is on cooldown",
          len(data["skips"]) == 1)
    bot.SKIP.unlink(missing_ok=True)
    plain = bot.cmd_force(data, now + 4, {"user_id": "u4", "privileged": False, "name": "x"}, [])
    check("a viewer cannot force", plain is None and not bot.SKIP.exists())
    bot.playing = real_playing
finally:
    bot.threshold, bot.unseen_hours = real_threshold, real_viewers
    bot.SKIP.unlink(missing_ok=True)
    bot.feed.ONAIR.unlink(missing_ok=True)

print("bot: a webhook is read only once it is proven to be Kick's")
check("a POST with no signature at all is refused",
      not kickapi.verify({"Kick-Event-Message-Id": "1"}, b"{}"))
check("control: a signature over the wrong body is refused too",
      not kickapi.verify({"Kick-Event-Message-Id": "1",
                          "Kick-Event-Message-Timestamp": "2026-09-19T12:00:00Z",
                          "Kick-Event-Signature": "Zm9v"}, b"{}"))


print("bot: the title a viewer can read, with the handle always last")
real_conf = dict(chan.CONF)
try:
    chan.CONF["TITLE_SUFFIX"] = "@nanatty"
    check("the date an upload carries is written the way it is read",
          bot.said_date("260120 nanatty Day 2") == ("20 Jan 2026", "nanatty Day 2"),
          bot.said_date("260120 nanatty Day 2"))
    check("the other prefix too",
          bot.said_date("2026-09-15 SOLO in Thailand")[0] == "15 Sep 2026")
    check("control: a number that is not a date is left alone",
          bot.said_date("999999 something") == ("", "999999 something"),
          bot.said_date("999999 something"))
    check("the channel's own name is not said twice",
          bot.clean_title("1789819373-2026-09-15_nanatty_SOLO_in_Thailand-k0156a4c001.mkv", "nanatty247")
          == "SOLO in Thailand (15 Sep 2026)",
          bot.clean_title("1789819373-2026-09-15_nanatty_SOLO_in_Thailand-k0156a4c001.mkv", "nanatty247"))
    check("an uploader's reference goes, the episode number stays",
          bot.clean_title("1789819373-260120_nanatty_-_Day_2_IRL_Santiago_c223-JEELzhY-PGQ.mkv", "nanatty247")
          == "Day 2 IRL Santiago (20 Jan 2026)",
          bot.clean_title("1789819373-260120_nanatty_-_Day_2_IRL_Santiago_c223-JEELzhY-PGQ.mkv", "nanatty247"))
    check("control: a name with nothing left still says something",
          bot.clean_title("1789819373-250512_nanatty_Kick_VOD-VWYhzs0WkQQ.mkv", "nanatty247")
          == "12 May 2025",
          bot.clean_title("1789819373-250512_nanatty_Kick_VOD-VWYhzs0WkQQ.mkv", "nanatty247"))
    was_part = chan.PART_SECONDS
    chan.PART_SECONDS = 3600
    chan.write_json(bot.feed.ONAIR,
                    {"source": "1789819373-2026-09-15_SOLO_in_Thailand-k0156a4c001.mkv",
                     "number": 1, "seconds": 14400, "at": time.time()})
    title = bot.wanted_title()
    check("the handle ends the title", title.endswith("@nanatty"), title)
    check("and the hour is in it", "hour 2/4" in title, title)
    chan.write_json(bot.feed.ONAIR, {"source": "1789819373-260120_" + "tres_long_" * 20 + "fin-JEELzhY-PGQ.mkv",
                                     "number": 0, "seconds": 14400, "at": time.time()})
    long_title = bot.wanted_title()
    check("a name too long is cut, never the handle",
          long_title.endswith("@nanatty") and len(long_title) <= 140, len(long_title))
    chan.CONF.pop("TITLE_SUFFIX")
    check("control: a channel with no handle configured carries none",
          not (bot.wanted_title() or "").endswith("@nanatty"))
    chan.PART_SECONDS = was_part
    bot.feed.ONAIR.unlink(missing_ok=True)
finally:
    chan.CONF.clear()
    chan.CONF.update(real_conf)


print("cut: two turns in a row from the same stream is a loop, whatever the hours say")
check("a Kick part belongs to its recording",
      cut.recording_of(pathlib.Path("1789-T-kb97ce32702.mkv")) == "kb97ce327",
      cut.recording_of(pathlib.Path("1789-T-kb97ce32702.mkv")))
check("and its neighbour belongs to the same one",
      cut.recording_of(pathlib.Path("1789-T-kb97ce32704.mkv")) == "kb97ce327")
check("control: a YouTube video is its own recording",
      cut.recording_of(pathlib.Path("1789-T-dQw4w9WgXcQ.mkv")) == "dQw4w9WgXcQ")
check("the last thing on the wire is read from the ledger",
      cut.last_recording({"kb97ce32702": {0: 100}, "dQw4w9WgXcQ": {0: 50}}) == "kb97ce327",
      cut.last_recording({"kb97ce32702": {0: 100}, "dQw4w9WgXcQ": {0: 50}}))
check("control: with a YouTube id last, that id is what must not repeat",
      cut.last_recording({"kb97ce32702": {0: 50}, "dQw4w9WgXcQ": {0: 100}}) == "dQw4w9WgXcQ")


print("bot: a vote asks for no more voices than there are people")
real_viewers = bot.kickapi.viewers
try:
    table = {}
    for n in (0, 1, 2, 3, 4, 12, 20, 40):
        bot.kickapi.viewers = lambda slug, n=n: n
        table[n] = bot.threshold()
    check("a lone viewer carries the vote alone", table[1] == 1, table)
    check("and so does a viewer the API cannot count", table[0] == 1, table)
    check("two viewers need both", table[2] == 2, table)
    check("the floor holds once the room can meet it", table[3] == 3 and table[12] == 3, table)
    check("above it the share takes over", table[20] == 5 and table[40] == 10, table)
    check("control: never more voices than viewers",
          all(v <= max(1, k) for k, v in table.items()), table)
    check("control: and never zero", all(v >= 1 for v in table.values()), table)
finally:
    bot.kickapi.viewers = real_viewers


print("cut: an hour is spent for good, whatever happens to the file")
cut.HOURS.unlink(missing_ok=True)
cut.PARTS.unlink(missing_ok=True)
cut.record_hour("1789819373-Un_Titre-dQw4w9WgXcQ.mkv", 2)
check("an hour on the wire is written down",
      cut.played("1789819373-Un_Titre-dQw4w9WgXcQ.mkv") == {2})
check("the same file fetched again under a new name remembers it",
      cut.played("1789999999-Un_Titre_Autre_Nom-dQw4w9WgXcQ.mkv") == {2},
      cut.played("1789999999-Un_Titre_Autre_Nom-dQw4w9WgXcQ.mkv"))
cut.record_hour("1789819373-Un_Titre-dQw4w9WgXcQ.mkv", 2)
check("control: written twice it is still one hour",
      cut.played("1789819373-Un_Titre-dQw4w9WgXcQ.mkv") == {2})
was = chan.PART_SECONDS
chan.PART_SECONDS = 3600
check("a file whose every hour is spent offers nothing",
      cut.unaired(pathlib.Path("1789819373-Un_Titre-dQw4w9WgXcQ.mkv"), cut.ledger(),
                  {"1789819373-Un_Titre-dQw4w9WgXcQ.mkv:0": 10800}) == {0, 1},
      "les heures 0 et 1 restent, la 2 est prise")
chan.PART_SECONDS = was
cut.HOURS.unlink(missing_ok=True)


print("bot: channel points do the thing or hand the points back")
check("no reward description would be cut in half by Kick's limit",
      bot.check_rewards(),
      [(r["title"], len(r["description"])) for r in bot.REWARDS])
check("the ones that need typing are the ones that ask a question",
      [r["input"] for r in bot.REWARDS]
      == [r["key"] in ("pick", "place", "request") for r in bot.REWARDS],
      [(r["key"], r["input"]) for r in bot.REWARDS])
settled = []
real_settle, real_unseen, real_playing = bot.kickapi.settle_redemption, bot.unseen_hours, bot.playing
try:
    bot.kickapi.settle_redemption = lambda rid, ok: settled.append((rid, ok)) or True
    bot.unseen_hours = lambda: 6
    bot.playing = lambda: {"title": "t", "name": "t.mkv", "hour": 1, "hours": 4,
                           "vid": "x", "started": 0, "elapsed": bot.SKIP_MIN_AIRED + 60}
    bot.SKIP.unlink(missing_ok=True)
    bot.STATE.unlink(missing_ok=True)

    def redeem(title, text="", status="pending", rid="r1"):
        settled.clear()
        bot.redeemed({"id": rid, "status": status, "user_input": text,
                      "reward": {"title": title},
                      "redeemer": {"user_id": 9, "username": "viewer"}})
        return settled[0] if settled else None

    check("a skip redemption moves the channel on and spends the points",
          redeem("Skip this hour") == ("r1", True) and bot.SKIP.exists())
    check("a second one right after is refused and refunded",
          redeem("Skip this hour", rid="r2") == ("r2", False))
    bot.SKIP.unlink(missing_ok=True)
    check("control: a reward nobody wired is left alone entirely",
          redeem("Some other reward") is None)
    check("control: a redemption already settled is not acted on twice",
          redeem("Skip this hour", status="accepted") is None)
    print("  -- a stream asked for by link")
    for shape in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                  "https://youtu.be/dQw4w9WgXcQ?t=42",
                  "youtube.com/live/dQw4w9WgXcQ",
                  "https://m.youtube.com/watch?app=desktop&v=dQw4w9WgXcQ",
                  "dQw4w9WgXcQ"):
        if bot.video_asked(shape) != "dQw4w9WgXcQ":
            check("every shape of link gives the id", False, shape)
            break
    else:
        check("every shape of link gives the id", True)
    check("control: something that is not a link gives nothing",
          bot.video_asked("vas y stp") is None and bot.video_asked("") is None)
    real_titles = supply.catalog_titles
    try:
        supply.catalog_titles = lambda: {"dQw4w9WgXcQ": "Day 7 IRL Cappadocia Turkey"}
        supply.REQUESTS.unlink(missing_ok=True)
        check("a link to something the channel follows is fetched",
              redeem("Request a stream", "https://youtu.be/dQw4w9WgXcQ") == ("r1", True)
              and "dQw4w9WgXcQ" in supply.REQUESTS.read_text())
        check("control: a link to anything else is refunded, not fetched blind",
              redeem("Request a stream", "https://youtu.be/AAAAAAAAAAA") == ("r1", False))
        check("control: nonsense is refunded",
              redeem("Request a stream", "coucou") == ("r1", False))
    finally:
        supply.catalog_titles = real_titles
        supply.REQUESTS.unlink(missing_ok=True)

    print("  -- keep going, and take me somewhere")
    real_shelf2 = bot.shelf
    try:
        here = pathlib.Path("1789819373-2026-09-15_SOLO_in_Thailand-k0156a4c001.mkv")
        bot.shelf = lambda: [(here, 2)]
        bot.PICK.unlink(missing_ok=True)
        check("a place that is on the shelf is honoured",
              redeem("Take me somewhere", "thailand") == ("r1", True) and bot.PICK.exists())
        bot.PICK.unlink(missing_ok=True)
        check("control: a place nobody filmed is refunded",
              redeem("Take me somewhere", "reykjavik") == ("r1", False)
              and not bot.PICK.exists())
        check("control: a place too short to mean anything is refunded",
              redeem("Take me somewhere", "a") == ("r1", False))
        check("a country reaches the cities filmed in it",
              "osaka" in bot.place_terms("japan") and "cappadocia" in bot.place_terms("turkey"))
        check("control: a city asked for stays that city",
              bot.place_terms("osaka") == ("osaka",))
        bot.playing = lambda: None
        check("keeping a nothing going is refunded",
              redeem("Keep this one going") == ("r1", False))
    finally:
        bot.shelf = real_shelf2
        bot.PICK.unlink(missing_ok=True)
        bot.playing = lambda: {"title": "t", "name": "t.mkv", "hour": 1, "hours": 4,
                               "vid": "x", "started": 0,
                               "elapsed": bot.SKIP_MIN_AIRED + 60}

    real_shelf = bot.shelf
    try:
        bot.shelf = lambda: []
        check("a pick with nothing on the shelf is refunded",
              redeem("Pick what plays next", "1") == ("r1", False))
        bot.shelf = lambda: [(pathlib.Path("1789819373-Un_Titre-dQw4w9WgXcQ.mkv"), 2)]
        bot.PICK.unlink(missing_ok=True)
        check("a valid pick is honoured and the cutter is told",
              redeem("Pick what plays next", "1") == ("r1", True) and bot.PICK.exists())
        check("control: a pick that is not a number is refunded",
              redeem("Pick what plays next", "banana") == ("r1", False))
    finally:
        bot.shelf = real_shelf
        bot.PICK.unlink(missing_ok=True)
finally:
    bot.kickapi.settle_redemption = real_settle
    bot.unseen_hours, bot.playing = real_unseen, real_playing
    bot.SKIP.unlink(missing_ok=True)

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
