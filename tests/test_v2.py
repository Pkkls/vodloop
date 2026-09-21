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
print("supply: the head of the list holds every country, not one of them")
check("a country is read off the title, cities included",
      (chan.country_of("Day 22, IRL Bursa, Turkey"), chan.country_of("IRL Osaka - Nontent"),
       chan.country_of("Day 37, IRL Cusco, Peru")) == ("turkey", "japan", "peru"),
      (chan.country_of("Day 22, IRL Bursa, Turkey"), chan.country_of("IRL Osaka - Nontent")))
check("control: a title that names nowhere says so plainly",
      chan.country_of("nanatty Kick VOD") == "" and chan.country_of("") == "")
rows = ([("t%d" % i, 3600) for i in range(40)]          # turkey, the crowd
        + [("j%d" % i, 3600) for i in range(20)]        # japan
        + [("p%d" % i, 3600) for i in range(3)])        # peru, almost nothing
titles = dict([("t%d" % i, "IRL Bursa, Turkey") for i in range(40)]
              + [("j%d" % i, "IRL Osaka") for i in range(20)]
              + [("p%d" % i, "IRL Cusco, Peru") for i in range(3)])
mixed = supply.mix_countries(rows, titles)
head = [chan.country_of(titles[v]) for v, _ in mixed[:9]]
check("the head alternates instead of running one country dry",
      set(head[:3]) == {"turkey", "japan", "peru"} and head[:3] != head[3:6] or
      sorted(head[:3]) == ["japan", "peru", "turkey"], head)
check("every country reaches the part of the list the board reads",
      {chan.country_of(titles[v]) for v, _ in mixed[:12]} == {"turkey", "japan", "peru"},
      [chan.country_of(titles[v]) for v, _ in mixed[:12]])
check("control: nothing is lost or duplicated in the reorder",
      sorted(mixed) == sorted(rows) and len(mixed) == len(rows), len(mixed))
just = ["turkey", "turkey", "turkey"]
after = supply.mix_countries(rows, titles, just)
check("a country still fresh on the wire goes last in the round, not away",
      chan.country_of(titles[after[0][0]]) != "turkey"
      and "turkey" in {chan.country_of(titles[v]) for v, _ in after[:4]},
      [chan.country_of(titles[v]) for v, _ in after[:4]])


check("a thick channel may only be asked for short videos",
      supply.fetch_ceiling(1e6) == int((10 * G - supply.AUDIO_ALLOWANCE) / 1e6),
      supply.fetch_ceiling(1e6))
check("and the room the board leaves for the sound is left here too",
      supply.fetch_ceiling(1e6) < int(10 * G / 1e6), supply.fetch_ceiling(1e6))
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
        check("with every hour spent just now, nothing is drawn rather than repeated",
              cut.next_source() == (None, None))
        # the same disk, but everything on it aired longer ago than the window
        old = int(time.time()) - cut.REPEAT_AFTER - 86400
        rows = [(old, chan.video_id(source.name), u) for u in
                range(cut.units_in(chan.duration(chan.AIRED / source.name)))]
        cut.UNITS.write_text("".join(
            chr(9).join(str(x) for x in row) + chr(10) for row in rows))
        cut.HOURS.unlink(missing_ok=True)
        cut.PARTS.unlink(missing_ok=True)
        again, origin2 = cut.next_source()
        check("but once it is older than the window it comes back round",
              again is not None and again.name == source.name, (again, origin2))
        if again is not None:
            again.replace(chan.AIRED / again.name)
        cut.UNITS.unlink(missing_ok=True)
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
    span = cut.per_slice()
    check("the ledger counts in chunks, twelve to an aired hour",
          (cut.unit_seconds(), span, cut.units_in(18000)) == (300, 12, 60),
          (cut.unit_seconds(), span, cut.units_in(18000)))
    starts = {cut.window(a, 18000)[0] for _ in range(300)}
    check("the block is drawn from anywhere in the file, not from the start",
          starts == {0.0, 3600.0, 7200.0, 10800.0, 14400.0}, sorted(starts))
    check("and it is a whole hour wherever it lands",
          {cut.window(a, 18000)[1] for _ in range(50)} == {3600.0})
    cut.set_part("a.mkv", {0, 1, 2, 4})
    check("a block already on the wire is never drawn again",
          {cut.window(a, 18000)[2] for _ in range(50)} == {3 * span},
          {cut.window(a, 18000)[2] for _ in range(50)})
    cut.set_part("a.mkv", {0, 1, 2, 3})
    check("the last block runs to the end of the file, stub included",
          cut.window(a, 18120) == (14400.0, 18120 - 14400.0, 4 * span),
          cut.window(a, 18120))
    cut.set_part("a.mkv", {0, 1, 2, 3, 4})
    check("a file with every minute spent offers no window at all",
          cut.window(a, 18000) is None, cut.window(a, 18000))
    check("but a resume gets back the block it was cutting, spent or not",
          cut.window(a, 18000, 2 * span) == (7200.0, 3600.0, 2 * span),
          cut.window(a, 18000, 2 * span))
    print("  -- and the minutes a skip never showed come back")
    cut.PARTS.unlink(missing_ok=True)
    cut.HOURS.unlink(missing_ok=True)
    cut.UNITS.unlink(missing_ok=True)
    # an hour was drawn at 2 h and skipped ten minutes in: two chunks went out
    cut.record_units("a.mkv", [24, 25])
    free = set(range(cut.units_in(18000))) - cut.played("a.mkv")
    check("only what was sent is spent, not the whole hour it came from",
          len(free) == 58 and 26 in free and 24 not in free, sorted(free)[:4])
    seen = {cut.window(a, 18000)[:2] for _ in range(300)}
    check("the minutes nobody saw are drawn again, starting where it stopped",
          (7800.0, 3600.0) in seen, sorted(seen))
    check("control: and never the two minutes that did go out",
          all(start >= 7800.0 or start + length <= 7200.0 for start, length in seen),
          sorted(seen))
    cut.UNITS.unlink(missing_ok=True)
    cut.set_part("a.mkv", 10800.0)
    check("the cursor the old format held reads as the chunks it had played",
          cut.played("a.mkv") == set(range(3 * span)), len(cut.played("a.mkv")))
    cut.PARTS.unlink(missing_ok=True)
finally:
    chan.PART_SECONDS = real_part

def send_everything():
    """Play the feeder: spend every chunk waiting, the way feed.note_on_air
    does as it puts each one on the wire."""
    book = cut.ledger()
    for name in sorted(q.name for q in chan.CHUNKS.glob("*.ts")):
        row = chan.read_json(cut.CHUNKMAP, {}).get(name)
        if row and len(row) >= 4:
            cut.record_units(row[0], [int(row[3])], book)
            book = cut.ledger()
        (chan.CHUNKS / name).unlink(missing_ok=True)


if shutil.which("ffmpeg"):
    print("cut: a sliced file goes back in the queue until it is spent")
    real_tail = cut.TAIL_SECONDS
    try:
        chan.PART_SECONDS, cut.TAIL_SECONDS = 5, 4
        for stale in chan.media(chan.AIRED):
            stale.unlink()
        (chan.STATE / "aired.tsv").unlink(missing_ok=True)
        make("3-Longue_video-partvideo01.mkv", 30)
        cut.UNITS.unlink(missing_ok=True)
        source, origin = cut.next_source()
        cut.run_job(source, origin, 0)
        check("nothing is spent until the feeder sends it",
              cut.played(source.name) == set(), cut.played(source.name))
        send_everything()
        back = chan.QUEUE / source.name
        check("after its first hour it is back in the queue", back.exists())
        check("with that hour written down and no other",
              len(cut.played(source.name)) == 1, cut.played(source.name))
        check("and it has not been recorded as aired",
              not (chan.STATE / "aired.tsv").exists())
        source, origin = cut.next_source()
        cut.run_job(source, origin, 0)
        send_everything()
        cut.next_source()   # the sweep is where a fully spent file retires now
        check("the last hour retires it", (chan.AIRED / back.name).exists()
              and "partvideo01" in (chan.STATE / "aired.tsv").read_text())
        check("control: the ledger keeps every hour, so none comes back",
              len(cut.played(back.name)) == cut.slices_in(chan.duration(chan.AIRED / back.name)),
              cut.played(back.name))
    finally:
        chan.PART_SECONDS, cut.TAIL_SECONDS = real_part, real_tail


if shutil.which("ffmpeg"):
    print("cut: the primer really cuts, and a skip is served from it")
    real_part2 = chan.PART_SECONDS
    try:
        chan.PART_SECONDS = 5
        cut.clear_ready()
        cut.HOURS.unlink(missing_ok=True)
        cut.PARTS.unlink(missing_ok=True)
        cut.PICK.unlink(missing_ok=True)
        cut.PRIMED.unlink(missing_ok=True)
        for stale in (list(chan.media(chan.QUEUE)) + list(chan.media(chan.CURRENT))
                      + list(chan.media(chan.AIRED))):
            stale.unlink()
        for stale in chan.CHUNKS.glob("*.ts"):
            stale.unlink()
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc2=s=854x480:r=30:d=12", "-f", "lavfi",
                        "-i", "sine=f=440:sample_rate=44100:d=12", "-c:v", "libx264",
                        "-g", "60", "-c:a", "aac", "-ac", "2", "-y",
                        str(chan.QUEUE / "5-A_tenir_pret-primevideo1.mkv")], check=True)
        cut.prime()
        held = cut.ready_set()
        check("the primer cuts a real head and writes down what it is",
              held is not None and held["source"] == "5-A_tenir_pret-primevideo1.mkv"
              and held["chunks"], held)
        check("and the hour it holds is not written to the ledger, it is not on air",
              cut.played("5-A_tenir_pret-primevideo1.mkv") == set(),
              cut.played("5-A_tenir_pret-primevideo1.mkv"))
        before = dict(held)
        # the only file on the disk is the one on air: the primer must still
        # hold an hour of it rather than leave the skip to the standby clip
        cut.clear_ready()
        chan.write_json(chan.STATE / "onair.json",
                        {"source": "5-A_tenir_pret-primevideo1.mkv", "number": 0,
                         "seconds": 12.0, "at": 0})
        cut.prime()
        check("with only the file on air left, it is still held rather than nothing",
              cut.ready_set() is not None, cut.ready_set())
        (chan.STATE / "onair.json").unlink(missing_ok=True)
        cut.clear_ready()
        cut.prime()
        held = cut.ready_set()
        before = dict(held)
        cut.prime()
        check("control: priming again keeps the one already held",
              cut.ready_set() == before)
        check("serving it fills the wire in one move",
              cut.serve_ready() and len(list(chan.CHUNKS.glob("*.ts"))) == len(before["chunks"]))
        check("and the loop is told to carry on with that same hour",
              chan.read_json(cut.PICK, {}).get("name") == before["source"]
              and chan.read_json(cut.PRIMED, {})["number"] == before["number"],
              (chan.read_json(cut.PICK, {}), chan.read_json(cut.PRIMED, {})))
    finally:
        chan.PART_SECONDS = real_part2
        cut.clear_ready()
        cut.PICK.unlink(missing_ok=True)
        cut.PRIMED.unlink(missing_ok=True)
        for stale in chan.CHUNKS.glob("*.ts"):
            stale.unlink()
        for stale in (list(chan.media(chan.QUEUE)) + list(chan.media(chan.CURRENT))
                      + list(chan.media(chan.AIRED))):
            stale.unlink()


BOTGUARD = True
print("bot: nobody can vote the reserve down to nothing")
import bot as _b  # noqa: E402
# the shelf is what every guard reads: the hours in reserve and the recordings
# they belong to both come out of it, so it is the one thing a test moves
ON_AIR = "1789-Un_Titre-dQw4w9WgXcQ.mkv"
OTHER = pathlib.Path("1790-Autre_Titre-aB3dEfGhIjK.mkv")
SAME = pathlib.Path("1789-Un_Titre-dQw4w9WgXcQ.mkv")
_was_shelf, _was_playing, _was_part = _b.shelf, _b.playing, chan.PART_SECONDS
try:
    chan.PART_SECONDS = 3600

    def at(minute, reserve):
        _b.shelf = lambda r=reserve: [(OTHER, r)]
        _b.playing = lambda m=minute: {"title": "t", "name": "t.mkv", "hour": 1,
                                       "hours": 4, "vid": "x", "started": 0,
                                       "elapsed": m * 60}
        return _b.playing()

    live = at(10, 12)
    check("a skip is priced on what it destroys, not counted as one",
          _b.skip_cost(live) == 3000.0 and _b.skip_cost(at(55, 12)) == 300.0,
          (_b.skip_cost(at(10, 12)), _b.skip_cost(at(55, 12))))
    fresh = {"skips": [], "users": {}}
    check("with a fat reserve and a late hour, a skip goes through",
          _b.skip_blocked(fresh, 10_000, at(55, 12)) is None,
          _b.skip_blocked(fresh, 10_000, at(55, 12)))
    early = _b.skip_blocked(fresh, 10_000, at(10, 5))
    check("a thin shelf refuses an early one, which would waste fifty minutes",
          early is not None and "too early" in early, early)
    thin = _b.skip_blocked(fresh, 10_000, at(55, 3.02))
    check("near the floor even a five minute skip is refused",
          thin is not None and "little left" in thin, thin)
    check("control: an hour more of shelf and the same skip goes through",
          _b.skip_blocked(fresh, 10_000, at(55, 4)) is None,
          _b.skip_blocked(fresh, 10_000, at(55, 4)))
    check("the allowance follows the shelf instead of being a fixed number",
          (_b.waste_allowance(3 * 3600), _b.waste_allowance(4 * 3600),
           _b.waste_allowance(12 * 3600)) == (0.0, 600.0, 3600.0),
          (_b.waste_allowance(3 * 3600), _b.waste_allowance(4 * 3600),
           _b.waste_allowance(12 * 3600)))
    check("a deep shelf still lets a viewer move on early in the hour",
          _b.skip_blocked(fresh, 10_000, at(10, 12)) is None,
          _b.skip_blocked(fresh, 10_000, at(10, 12)))

    print("  -- the attack: one viewer voting as fast as the rules allow")
    state = {"skips": [], "users": {}}
    clock, burned, allowed = 0.0, 0.0, 0
    for _ in range(400):
        clock += 60
        live = at(11, max(0.0, 12 - burned / 3600))
        if _b.skip_blocked(state, clock, live, "troll") is None:
            _b.do_skip(state, clock, "vote", live, "troll")
            burned += _b.skip_cost(live)
            allowed += 1
        state["skips"] = _b.skips_since(state, clock - 7200)
    check("six hours of one viewer spamming cannot empty the reserve",
          0 < allowed and burned < (12 - 3) * 3600,
          "%.1f h detruites en %d skips" % (burned / 3600, allowed))
    check("and the reserve never goes under the floor it promised",
          12 * 3600 - burned >= _b.SKIP_FLOOR, (12 * 3600 - burned) / 3600)

    print("  -- and one viewer cannot take every turn")
    state = {"skips": [], "users": {}}
    _b.do_skip(state, 10_000, "vote", at(55, 12), "troll")
    check("the one who just carried a skip is asked to wait",
          "someone else" in (_b.skip_blocked(state, 12_000, at(55, 12), "troll") or ""),
          _b.skip_blocked(state, 12_000, at(55, 12), "troll"))
    check("control: somebody else is not made to wait for them",
          _b.skip_blocked(state, 12_000, at(55, 12), "quelquun") is None,
          _b.skip_blocked(state, 12_000, at(55, 12), "quelquun"))
    check("control: a state written before costs were recorded still counts",
          len(_b.skips_since({"skips": [9_000.0, 1.0]}, 5_000)) == 1)
finally:
    _b.shelf, _b.playing, chan.PART_SECONDS = _was_shelf, _was_playing, _was_part


print("bot: reading the pipeline")
import bot  # noqa: E402

check("a file name becomes something a viewer can read",
      bot.pretty("1789784406-260120_nanatty_-_Day_2_IRL_Santiago-JEELzhY-PGQ.mkv")
      == "260120 nanatty - Day 2 IRL Santiago",
      bot.pretty("1789784406-260120_nanatty_-_Day_2_IRL_Santiago-JEELzhY-PGQ.mkv"))
check("control: a name with neither prefix nor id survives it",
      bot.pretty("clip.mkv") == "clip")
chan.write_json(bot.feed.ONAIR, {"source": "1789-Un_Titre-dQw4w9WgXcQ.mkv",
                                 "number": 24, "seconds": 18000,
                                 "at": time.time() - 900})
was = chan.PART_SECONDS
chan.PART_SECONDS = 3600
live = bot.playing()
chan.PART_SECONDS = was
check("what is on the wire is read from the cutter's own job",
      live["hour"] == 3 and live["hours"] == 5 and 890 < live["elapsed"] < 960, live)
check("and its source is named", live["vid"] == "dQw4w9WgXcQ")

print("bot: the guards on skipping")
real_shelf = bot.shelf
try:
    bot.shelf = lambda: []
    check("no vote opens when there is nothing unseen to move on to",
          bot.skip_blocked({"skips": []}, 1000000, None) is not None)
    bot.shelf = lambda: [(OTHER, 5.0)]
    check("control: with material in hand the hour itself is the only gate",
          bot.skip_blocked({"skips": []}, 1000000, None) is None)
    fresh = {"name": ON_AIR, "elapsed": 60, "hour": 1, "hours": 5}
    check("an hour cannot be voted off in its first minutes",
          "vote in" in (bot.skip_blocked({"skips": []}, 1000000, fresh) or ""))
    settled = {"name": ON_AIR, "elapsed": bot.SKIP_MIN_AIRED + 1, "hour": 1, "hours": 5}
    check("control: once it has run long enough it can",
          bot.skip_blocked({"skips": []}, 1000000, settled) is None)
    bot.shelf = lambda: [(SAME, 5.0)]
    check("a skip that can only land on the same stream is refused, not run",
          "only stream left" in (bot.skip_blocked({"skips": []}, 1000000, settled) or ""),
          bot.skip_blocked({"skips": []}, 1000000, settled))
    bot.shelf = lambda: [(SAME, 5.0), (OTHER, 1.0)]
    check("control: one other stream on the shelf and it goes through",
          bot.skip_blocked({"skips": []}, 1000000, settled) is None)
    just = {"skips": [1000000 - 60]}
    check("a skip locks the next one for the cooldown",
          "just skipped" in (bot.skip_blocked(just, 1000000, settled) or ""))
    many = {"skips": [1000000 - 100 * n for n in range(1, bot.SKIP_MAX_PER_HOUR + 1)]}
    check("and an hour holds only so many of them",
          "that is the limit" in (bot.skip_blocked(many, 1000000, settled) or ""),
          bot.skip_blocked(many, 1000000, settled))
    old = {"skips": [1000000 - 7000]}
    check("control: skips older than the hour do not count",
          bot.skip_blocked(old, 1000000, settled) is None)
    check("nothing a viewer is told names a file, an hour count or a floor",
          all(not any(word in (message or "") for word in ("reserve", "shelf", "h back"))
              for message in (bot.skip_blocked({"skips": []}, 1000000, settled),
                              bot.cmd_aide(),
                              bot.cmd_liste({}, 1000000, {"user_id": "u"}, []))))
finally:
    bot.shelf = real_shelf

print("bot: one voice per account, and the skip only at the threshold")
real_threshold, real_shelf = bot.threshold, bot.shelf
try:
    bot.threshold = lambda: 3
    bot.shelf = lambda: [(OTHER, 5.0)]
    bot.SKIP.unlink(missing_ok=True)
    data = {"skips": [], "vote": None, "users": {}, "seen": []}
    settled = {"name": ON_AIR, "elapsed": bot.SKIP_MIN_AIRED + 1, "hour": 1, "hours": 5}
    real_playing = bot.playing
    bot.playing = lambda: settled
    now = 2000000
    first = bot.cmd_vote(data, now, {"user_id": "u1", "privileged": False}, [])
    check("the first voice opens the vote and says what it needs",
          "3 votes to skip" in (first or "") and "!skip" in (first or ""), first)
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
    bot.threshold, bot.shelf = real_threshold, real_shelf
    bot.SKIP.unlink(missing_ok=True)
    bot.feed.ONAIR.unlink(missing_ok=True)

print("bot: the list is the catalogue, and picking what is not here fetches it")
real_shelf, real_cand = bot.shelf, supply.candidates
real_threshold, real_waiting = bot.threshold, bot.waiting_for
chan.STATE.mkdir(parents=True, exist_ok=True)
supply.REQUESTS.unlink(missing_ok=True)
try:
    on_disk = pathlib.Path("1789819373-Deja_La-dQw4w9WgXcQ.mkv")
    bot.shelf = lambda: [(on_disk, 4)]
    supply.candidates = lambda: [(f"id{n:09d}", 3600, f"Stream number {n}") for n in range(40)]
    bot.threshold, bot.waiting_for = lambda: 1, lambda seconds: "ready in ~30 min"
    viewer = {"user_id": "u1", "name": "u1", "privileged": False}
    listed = bot.cmd_liste({}, 1000, viewer, [])
    check("the list counts everything the board can bring, not only the disk",
          listed.startswith("41 videos:"), listed)
    supply.candidates = lambda: []
    check("control: with nothing fetchable the list is the disk again",
          bot.cmd_liste({}, 1000, viewer, []).startswith("1 videos:"))
    supply.candidates = lambda: [(f"id{n:09d}", 3600, f"Stream number {n}") for n in range(40)]
    check("one page stays inside a single chat line", len(listed) <= 500, len(listed))
    page2 = bot.cmd_liste({}, 1000, viewer, ["2"])
    check("the numbers run on across the pages rather than restarting",
          page2.split(": ")[1].startswith(f"{bot.LIST_PAGE + 1} "), page2)
    check("control: a page past the end lands on the last one, never empty",
          bot.cmd_liste({}, 1000, viewer, ["99"]).split(": ")[1].split(" ")[0].isdigit())

    data = {}
    answer = bot.cmd_pick(data, 1000, viewer, ["7"])
    check("picking something the channel does not hold puts it on the board",
          supply.REQUESTS.read_text().strip() == "id000000005", answer)
    check("and the viewer is told it is coming, with a wait", "downloading" in answer, answer)
    check("control: the same viewer cannot hold two of the board's slots",
          "one coming already" in bot.cmd_pick(data, 1010, viewer, ["8"]) and
          supply.REQUESTS.read_text().count("\n") == 1)
    other = {"user_id": "u2", "name": "u2", "privileged": False}
    bot.cmd_pick(data, 1020, other, ["9"])
    bot.cmd_pick(data, 1030, {"user_id": "u3", "name": "u3", "privileged": False}, ["10"])
    full = bot.cmd_pick(data, 1040, {"user_id": "u4", "name": "u4", "privileged": False}, ["11"])
    check("the board's queue is capped, and the refusal appends nothing",
          "downloading already" in full and supply.REQUESTS.read_text().count("\n") == 3, full)

    bot.PICK.unlink(missing_ok=True)
    before = supply.REQUESTS.read_text()
    picked = bot.cmd_pick({}, 1050, other, ["1"])
    check("control: a number that is on the disk plays, it does not fetch",
          bot.PICK.exists() and supply.REQUESTS.read_text() == before, picked)
finally:
    bot.shelf, supply.candidates = real_shelf, real_cand
    bot.threshold, bot.waiting_for = real_threshold, real_waiting
    supply.REQUESTS.unlink(missing_ok=True)
    bot.PICK.unlink(missing_ok=True)

print("bot: thirty minutes forward takes a slice out of the hour, not the hour")
real_playing_j, real_ahead = bot.playing, cut.ahead_seconds
try:
    chan.CHUNKS.mkdir(parents=True, exist_ok=True)
    for stale in chan.CHUNKS.glob("*.ts"):
        stale.unlink()
    cut.JUMP.unlink(missing_ok=True)
    cut.FLUSH.unlink(missing_ok=True)
    bot.playing = lambda: {"title": "t", "name": "t.mkv", "hour": 1, "hours": 2,
                           "vid": "x", "started": 0, "elapsed": 900}
    cut.ahead_seconds = lambda: bot.JUMP_SECONDS - chan.CHUNK_SECONDS
    ok, said = bot.reward_jump({}, 1000, "v", "")
    check("with less cut ahead than the jump it is refused and refunded",
          ok is False and said and not cut.JUMP.exists(), said)
    cut.ahead_seconds = lambda: 3600
    ok, said = bot.reward_jump({}, 1000, "v", "")
    check("with the hour in hand it is taken and the cutter is told",
          ok is True and cut.JUMP.read_text() == str(bot.JUMP_SECONDS), said)
    for n in range(12):
        (chan.CHUNKS / f"{n:05d}.ts").write_bytes(b"x")
    goes = int(bot.JUMP_SECONDS // chan.CHUNK_SECONDS)
    dropped = cut.do_jump()
    left = sorted(path.name for path in chan.CHUNKS.glob("*.ts"))
    check("the cutter drops exactly the minutes paid for, oldest first",
          dropped == bot.JUMP_SECONDS and len(left) == 12 - goes
          and left[0] == f"{goes:05d}.ts", (dropped, left))
    check("and flushes what is going out, so the jump is seen now and not in five minutes",
          cut.FLUSH.exists())
    cut.FLUSH.unlink(missing_ok=True)
    check("control: nothing asked drops nothing and flushes nothing",
          cut.do_jump() == 0 and not cut.FLUSH.exists()
          and len(list(chan.CHUNKS.glob("*.ts"))) == 12 - goes)
    bot.playing = lambda: None
    ok, said = bot.reward_jump({}, 1000, "v", "")
    check("control: with nothing on air there is nothing to jump into",
          ok is False and "nothing on air" in said, said)
finally:
    bot.playing, cut.ahead_seconds = real_playing_j, real_ahead
    cut.JUMP.unlink(missing_ok=True)
    cut.FLUSH.unlink(missing_ok=True)
    for stale in chan.CHUNKS.glob("*.ts"):
        stale.unlink()

print("bot: every answer is said in four languages and still fits a chat line")
try:
    check("every phrase carries exactly four languages",
          all(len(v) == 4 for v in bot.SAID.values()),
          [k for k, v in bot.SAID.items() if len(v) != 4])
    filled = {k: bot.four(k, n=3, min=15, need=3, max=527, place="peru")
              for k in bot.SAID}
    longest = max(filled.items(), key=lambda row: len(row[1]))
    check("and none of them is longer than 200 characters on its own",
          len(longest[1]) <= 200, (longest[0], len(longest[1])))
    # the two longest an answer can pair: a refusal plus the points coming back
    worst = max(len(f"@somebodywithalongname {line} · {filled['points_back']}")
                for key, line in filled.items())
    check("a refusal and its refund together stay inside Kick's 500",
          worst <= 500, worst)
    check("control: a phrase with a slot left unfilled is caught, not sent",
          "{" not in "".join(filled.values()),
          [k for k, v in filled.items() if "{" in v])
finally:
    pass

print("bot: !fetch shows the board's queue, whose it is and what is left")
real_shelf, real_eta, real_secs = bot.shelf, bot.fetch_eta, bot.catalog_seconds
try:
    bot.shelf = lambda: []
    bot.fetch_eta, bot.catalog_seconds = lambda seconds: 20, lambda vid: 3600
    empty = bot.cmd_fetch({}, 1000, {"user_id": "u", "name": "u"}, [])
    check("with nothing asked it says so and points at how to ask",
          empty.startswith("nothing asked for right now") and "!pick" in empty, empty)
    data = {"asked": {f"id{n}": {"who": f"u{n}", "title": f"Stream {n}", "state": "waiting"}
                      for n in range(5)}}
    data["asked"]["id9"] = {"who": "u9", "title": "Deja la", "state": "here"}
    line = bot.cmd_fetch(data, 1000, {"user_id": "u", "name": "u"}, [])
    check("it counts what is coming and names who asked",
          line.startswith("5 downloading · descargando") and "@u0 ~20 min" in line, line)
    check("it does not read out more than three of them",
          "2 more asked" in line and line.count(" @u") == 3, line)
    check("one already landed is said apart, not counted as coming",
          "1 landed, waiting its turn" in line, line)
    check("control: an answer stays inside a chat line", len(line) <= 500, len(line))
finally:
    bot.shelf, bot.fetch_eta, bot.catalog_seconds = real_shelf, real_eta, real_secs

print("bot: a vote that sends the board shopping puts a short on the wire")
real_conf = dict(chan.CONF)
try:
    chan.SHORTS.mkdir(parents=True, exist_ok=True)
    for stale in chan.SHORTS.glob("*.ts"):
        stale.unlink()
    feed.SHORT.unlink(missing_ok=True)
    check("with nothing prepared, no marker is written at all",
          bot.play_short() is False and not feed.SHORT.exists())
    check("control: and the feeder plays no short either", feed.next_short() is None)
    first, second = chan.SHORTS / "un.ts", chan.SHORTS / "deux.ts"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    os.utime(first, (1000, 1000))
    os.utime(second, (2000, 2000))
    check("with shorts ready, a vote asks the feeder for one",
          bot.play_short() is True and feed.SHORT.exists())
    taken = feed.next_short()
    check("the feeder takes the oldest and spends the marker",
          taken == first and not feed.SHORT.exists(), taken)
    check("control: with the marker spent it plays no second one",
          feed.next_short() is None)
    os.utime(first, (3000, 3000))  # what the feeder does after sending it
    bot.play_short()
    check("the next one asked for is the other short, not the same again",
          feed.next_short() == second)
    check("control: nothing is deleted, both are kept for their next turn",
          len(list(chan.SHORTS.glob("*.ts"))) == 2)
    for stale in chan.SHORTS.glob("*.ts"):
        stale.unlink()
finally:
    chan.CONF.clear()
    chan.CONF.update(real_conf)
    feed.SHORT.unlink(missing_ok=True)

print("bot: telegram carries the chat out and one room's answers back")
said_tg, real_say_tg = [], bot.say
real_chat, real_token = bot.TG_CHAT, bot.TG_TOKEN
try:
    bot.say = lambda text, reply_to=None: said_tg.append(text) or True
    bot.TG_CHAT, bot.TG_TOKEN = "42", "jeton"
    bot.tg_reply({"chat": {"id": 42}, "text": "bonsoir"})
    check("a line typed on telegram goes out on the kick chat",
          said_tg == ["bonsoir"], said_tg)
    bot.tg_reply({"chat": {"id": 99}, "text": "je passais par la"})
    check("control: another telegram room cannot speak through the channel",
          said_tg == ["bonsoir"], said_tg)
    bot.tg_reply({"chat": {"id": 42}, "text": "   "})
    check("control: an empty line says nothing", said_tg == ["bonsoir"], said_tg)
    bot._relay.clear()
    chan.on_log = lambda line: bot.relay(f"[log] {line}")
    chan.log("recompense jump par v: honoree")
    check("what the bot writes down goes out with the chat",
          bot._relay == ["[log] recompense jump par v: honoree"], bot._relay)
    bot._relay.clear()
    chan.on_log = lambda line: chan.log("et me revoila")
    chan.log("une ligne")
    check("control: a hook that logs goes round once, not for ever", True)
    chan.on_log = lambda line: 1 / 0
    chan.log("une autre")
    check("control: a hook that throws does not take the bot down with it", True)
    chan.on_log = None
    bot._relay.clear()
    chan.log("plus personne n ecoute")
    check("control: with no hook set nothing is queued at all", bot._relay == [], bot._relay)
    bot.relay("viewer: salut")
    check("the chat waits in one batch instead of a message per line",
          bot._relay == ["viewer: salut"], bot._relay)
    bot.TG_TOKEN = ""
    bot.relay("viewer: personne n'ecoute")
    check("control: with no telegram configured nothing is queued at all",
          bot._relay == ["viewer: salut"], bot._relay)
finally:
    bot.say, bot.TG_CHAT, bot.TG_TOKEN = real_say_tg, real_chat, real_token
    bot._relay.clear()

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
                     "number": 12, "seconds": 14400, "at": time.time()})
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


print("bot: an ETA that counts the board sitting out its own hourly cap")
for stale in chan.media(chan.QUEUE):
    stale.unlink()
check("with nothing delivered lately the board owes nothing",
      bot.board_busy_minutes() == 0, bot.board_busy_minutes())
small = chan.QUEUE / ("%d-Petit-aaaaaaaaaaa.mkv" % int(time.time() - 600))
small.write_bytes(b"x" * 1024)
check("control: a delivery under the cap does not hold it either",
      bot.board_busy_minutes() == 0, bot.board_busy_minutes())
big = chan.QUEUE / ("%d-Gros-bbbbbbbbbbb.mkv" % int(time.time() - 600))
big.touch()
# sparse: only st_size is read, and a real two gigabytes here killed the box
os.truncate(big, int(bot.BOARD_HOUR_MB) * 1000 * 1000 + 1)
waited = bot.board_busy_minutes()
check("a delivery over the cap holds the board for the rest of the hour",
      48 <= waited <= 50, waited)
check("and the wait lands in front of the estimate, not beside it",
      bot.fetch_eta(3600) >= waited + bot.BOARD_SLOT_MIN, bot.fetch_eta(3600))
small.unlink()
big.unlink()


print("cut: Kick is off where it is switched off")
real_nokick = chan.NO_KICK
try:
    chan.NO_KICK = True
    for stale in (list(chan.media(chan.QUEUE)) + list(chan.media(chan.CURRENT))
                  + list(chan.media(chan.AIRED))):
        stale.unlink()
    cut.HOURS.unlink(missing_ok=True)
    cut.PARTS.unlink(missing_ok=True)
    (chan.QUEUE / "9-Un_morceau_Kick-k2efa33c602.mkv").write_bytes(b"x")
    (chan.QUEUE / "9-Une_video_YouTube-yOutUbe1234.mkv").write_bytes(b"x")
    # chan.duration keys its cache on name and size, so the stubs are one byte
    durations = {"9-Un_morceau_Kick-k2efa33c602.mkv:1": 7200.0,
                 "9-Une_video_YouTube-yOutUbe1234.mkv:1": 7200.0}
    was = chan.PART_SECONDS
    chan.PART_SECONDS = 3600
    picks = {cut.draw(cut.ledger(), durations, "")[0].name for _ in range(30)}
    check("a Kick part is never drawn on a channel with Kick switched off",
          picks == {"9-Une_video_YouTube-yOutUbe1234.mkv"}, picks)
    (chan.QUEUE / "9-Une_video_YouTube-yOutUbe1234.mkv").unlink()
    check("with nothing else left it is still not drawn, it is not drawn at all",
          cut.draw(cut.ledger(), durations, "") == (None, None),
          cut.draw(cut.ledger(), durations, ""))
    chan.NO_KICK = False
    check("control: with Kick on, the same part is drawn again",
          cut.draw(cut.ledger(), durations, "")[0].name
          == "9-Un_morceau_Kick-k2efa33c602.mkv",
          cut.draw(cut.ledger(), durations, ""))
    chan.PART_SECONDS = was
finally:
    chan.NO_KICK = real_nokick
    for stale in (list(chan.media(chan.QUEUE)) + list(chan.media(chan.CURRENT))
                  + list(chan.media(chan.AIRED))):
        stale.unlink()
    cut.HOURS.unlink(missing_ok=True)


print("cut: a skip asked while the cutter is idle is still honoured")
cut.clear_ready()
cut.SKIP.unlink(missing_ok=True)
cut.FLUSH.unlink(missing_ok=True)
for stale in chan.CHUNKS.glob("*.ts"):
    stale.unlink()
(chan.CHUNKS / "0000008001.ts").write_bytes(b"x")
cut.do_skip("essai", "quelque_chose.mkv")
check("it drops what was waiting even with no job running",
      not list(chan.CHUNKS.glob("*.ts")), list(chan.CHUNKS.glob("*.ts")))
check("and tells the feeder to cut the chunk in flight short", cut.FLUSH.exists())
cut.FLUSH.unlink(missing_ok=True)


print("cut: an hour held ready, so a skip has something to send at once")
cut.clear_ready()
cut.PRIMED.unlink(missing_ok=True)
cut.PICK.unlink(missing_ok=True)
for stale in chan.media(chan.CURRENT):
    stale.unlink()
check("with nothing held, there is nothing to serve",
      cut.ready_set() is None and cut.serve_ready() is False)
cut.READY.mkdir(parents=True, exist_ok=True)
chan.write_json(cut.READY_JOB, {"source": "7-Tenue-preteAAAAAAA.mkv", "number": 2,
                                "seconds": 15654.0, "chunks": ["0000009001.ts"]})
check("control: a note whose chunks are gone is not trusted",
      cut.ready_set() is None, cut.ready_set())
(cut.READY / "0000009001.ts").write_bytes(b"x")
check("with its chunks on disk it is usable", cut.ready_set() is not None)
for stale in chan.CHUNKS.glob("*.ts"):
    stale.unlink()
check("serving it puts the chunks on the wire and says what comes next",
      cut.serve_ready()
      and [q.name for q in chan.CHUNKS.glob("*.ts")] == ["0000009001.ts"]
      and chan.read_json(cut.PRIMED, {}) == {"source": "7-Tenue-preteAAAAAAA.mkv",
                                             "number": 2, "done": 1}
      and chan.read_json(cut.PICK, {}).get("name") == "7-Tenue-preteAAAAAAA.mkv",
      (chan.read_json(cut.PRIMED, {}), chan.read_json(cut.PICK, {})))
check("and the chunk carries the hour it came from, for the title",
      chan.read_json(cut.CHUNKMAP, {}).get("0000009001.ts")
      == ["7-Tenue-preteAAAAAAA.mkv", 2, 15654.0, 2],
      chan.read_json(cut.CHUNKMAP, {}))
check("control: nothing is left held afterwards", cut.ready_set() is None)
cut.record_hour("7-Tenue-preteAAAAAAA.mkv", 5)
(cut.READY / "0000009002.ts").write_bytes(b"x")
chan.write_json(cut.READY_JOB, {"source": "7-Tenue-preteAAAAAAA.mkv", "number": 5,
                                "seconds": 15654.0, "chunks": ["0000009002.ts"]})
check("an hour that went out the normal way while it waited is dropped, not replayed",
      cut.serve_ready() is False and cut.ready_set() is None
      and not list(chan.CHUNKS.glob("0000009002.ts")))
for stale in chan.CHUNKS.glob("*.ts"):
    stale.unlink()
cut.PRIMED.unlink(missing_ok=True)
cut.PICK.unlink(missing_ok=True)
cut.HOURS.unlink(missing_ok=True)


print("cut: a minute the wire has passed is never queued behind it")
was_part = chan.PART_SECONDS
try:
    chan.PART_SECONDS = 3600
    cut.UNITS.unlink(missing_ok=True)
    cut.HOURS.unlink(missing_ok=True)
    cut.record_units("8-Deja_vu-ddddddddddd.mkv", [30, 31])
    check("the ledger knows those two minutes have gone out",
          cut.played("8-Deja_vu-ddddddddddd.mkv") == {30, 31})
    check("so a resumed job that reaches them must drop them, not queue them",
          all(u in cut.played("8-Deja_vu-ddddddddddd.mkv") for u in (30, 31))
          and 32 not in cut.played("8-Deja_vu-ddddddddddd.mkv"))
    cut.UNITS.unlink(missing_ok=True)
finally:
    chan.PART_SECONDS = was_part


print("cut: a restart resumes the hour it was cutting, not another one")
for stale in chan.media(chan.CURRENT):
    stale.unlink()
(chan.CURRENT / "9-Reprise-rESUmE12345.mkv").write_bytes(b"x")
chan.write_json(cut.JOB, {"source": "9-Reprise-rESUmE12345.mkv", "origin": "queue",
                          "done": 11, "number": 3, "seconds": 15654.0})
resumed = cut.recover()
check("the job hands back the hour it had drawn, beside the chunks it had made",
      resumed is not None and resumed[2:] == (11, 3), resumed)
chan.write_json(cut.JOB, {"source": "9-Reprise-rESUmE12345.mkv", "origin": "queue", "done": 4})
check("control: a job written before the hour was kept resumes without one",
      cut.recover()[3] is None, cut.recover())
cut.JOB.unlink(missing_ok=True)
for stale in chan.media(chan.CURRENT):
    stale.unlink()


print("cut: a minute is spent for good, whatever happens to the file")
was = chan.PART_SECONDS
chan.PART_SECONDS = 3600
span = cut.per_slice()
cut.HOURS.unlink(missing_ok=True)
cut.UNITS.unlink(missing_ok=True)
cut.PARTS.unlink(missing_ok=True)
cut.record_units("1789819373-Un_Titre-dQw4w9WgXcQ.mkv", [26])
check("a chunk on the wire is written down",
      cut.played("1789819373-Un_Titre-dQw4w9WgXcQ.mkv") == {26})
check("the same file fetched again under a new name remembers it",
      cut.played("1789999999-Un_Titre_Autre_Nom-dQw4w9WgXcQ.mkv") == {26},
      cut.played("1789999999-Un_Titre_Autre_Nom-dQw4w9WgXcQ.mkv"))
cut.record_units("1789819373-Un_Titre-dQw4w9WgXcQ.mkv", [26])
check("control: written twice it is still one chunk",
      cut.played("1789819373-Un_Titre-dQw4w9WgXcQ.mkv") == {26})
cut.UNITS.unlink(missing_ok=True)
cut.record_hour("1789819373-Un_Titre-dQw4w9WgXcQ.mkv", 2)
check("a whole block spent at once covers its twelve chunks",
      cut.played("1789819373-Un_Titre-dQw4w9WgXcQ.mkv") == set(range(24, 36)),
      sorted(cut.played("1789819373-Un_Titre-dQw4w9WgXcQ.mkv")))
check("the hour recorded before the change still reads as its twelve chunks",
      cut.unaired(pathlib.Path("1789819373-Un_Titre-dQw4w9WgXcQ.mkv"), cut.ledger(),
                  {"1789819373-Un_Titre-dQw4w9WgXcQ.mkv:0": 10800})
      == set(range(2 * span)),
      "les chunks des heures 0 et 1 restent, ceux de la 2 sont pris")
check("control: and a chunk held in the cutter's buffer is not offered either",
      cut.unaired(pathlib.Path("1789819373-Un_Titre-dQw4w9WgXcQ.mkv"), cut.ledger(),
                  {"1789819373-Un_Titre-dQw4w9WgXcQ.mkv:0": 10800},
                  {"dQw4w9WgXcQ": {0, 1}}) == set(range(2, 2 * span)),
      "les deux premiers chunks sont deja coupes, pas encore envoyes")
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
real_settle, real_unseen, real_playing = bot.kickapi.settle_redemption, bot.shelf, bot.playing
try:
    bot.kickapi.settle_redemption = lambda rid, ok: settled.append((rid, ok)) or True
    bot.shelf = lambda: [(OTHER, 6)]
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
    print("  -- the chat is told what changed")
    said = []
    real_say = bot.say
    try:
        bot.say = lambda text, reply_to=None: said.append(text) or True
        state = {"said_playing": "", "asked": {}}
        bot.playing = lambda: {"title": "t", "name": "1789819373-2026-09-15_SOLO_in_Thailand-k0156a4c001.mkv",
                               "hour": 2, "hours": 4, "vid": "k0156a4c001",
                               "started": 0, "elapsed": 60}
        bot.announce(state)
        check("a new hour is announced once", len(said) == 1 and "now playing" in said[0], said)
        bot.announce(state)
        check("and not announced again while it is still on", len(said) == 1, said)
        bot.playing = lambda: None
        bot.announce(state)
        check("an empty shelf is said out loud too",
              len(said) == 2 and "nothing new to play" in said[1], said)
    finally:
        bot.say = real_say
        bot.playing = lambda: {"title": "t", "name": "t.mkv", "hour": 1, "hours": 4,
                               "vid": "x", "started": 0,
                               "elapsed": bot.SKIP_MIN_AIRED + 60}

    print("  -- what a viewer is told about the wait")
    chan.write_json(chan.STATE / "want.json", {"rate_bps": 250000})
    was_part = chan.PART_SECONDS
    chan.PART_SECONDS = 3600
    two_hours = bot.fetch_eta(7200)
    four_hours = bot.fetch_eta(14400)
    check("a longer video is quoted a longer wait", four_hours > two_hours,
          (two_hours, four_hours))
    check("and the estimate is rounded, not pretended to the minute",
          two_hours % 5 == 0 and four_hours % 5 == 0, (two_hours, four_hours))
    check("the slot allowance is in it even for something tiny",
          bot.fetch_eta(1) >= bot.BOARD_SLOT_MIN, bot.fetch_eta(1))
    line = bot.waiting_for(7200)
    check("a viewer is told both when it arrives and when it airs",
          line.startswith("~") and "on air ~" in line and "yay" in line, line)
    chan.PART_SECONDS = was_part

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
              redeem("Request a stream", "https://youtu.be/dQw4w9WgXcQ") is None
              and "dQw4w9WgXcQ" in supply.REQUESTS.read_text())
        check("control: a link to anything else is refunded, not fetched blind",
              redeem("Request a stream", "https://youtu.be/AAAAAAAAAAA") == ("r1", False))
        check("control: nonsense is refunded",
              redeem("Request a stream", "coucou") == ("r1", False))
    finally:
        supply.catalog_titles = real_titles
        supply.REQUESTS.unlink(missing_ok=True)

    print("  -- a paid request is followed to its end, or the points come back")
    real_titles2, real_shelf3, real_say3 = supply.catalog_titles, bot.shelf, bot.say
    real_excluded = supply.excluded
    said2, landed = [], chan.QUEUE / "1789819373-Day_7_IRL_Cappadocia-dQw4w9WgXcQ.mkv"
    try:
        bot.say = lambda text, reply_to=None: said2.append(text) or True
        supply.catalog_titles = lambda: {"dQw4w9WgXcQ": "Day 7 IRL Cappadocia Turkey"}
        supply.excluded = lambda now: set()
        supply.REQUESTS.unlink(missing_ok=True)
        bot.PICK.unlink(missing_ok=True)
        bot.shelf = lambda: []
        fresh = bot.load()
        fresh["asked"] = {}
        bot.save(fresh)
        check("a fetch the board still owes takes no points yet",
              redeem("Request a stream", "https://youtu.be/dQw4w9WgXcQ") is None)
        state = bot.load()
        check("and the redemption is held so it can be settled either way",
              (state.get("asked") or {}).get("dQw4w9WgXcQ", {}).get("redemption") == "r1",
              state.get("asked"))
        settled.clear(); said2.clear()
        bot.announce_arrivals(state)
        check("while it is still coming, nothing is settled and nothing is said",
              not settled and not said2, (settled, said2))
        landed.write_bytes(b"x")
        bot.shelf = lambda: [(landed, 3)]
        bot.announce_arrivals(state)
        check("when it lands the points are taken and the chat hears it",
              settled == [("r1", True)] and any("landed" in t for t in said2)
              and bot.PICK.exists(), (settled, said2))
        settled.clear(); said2.clear()
        bot.playing = lambda: {"title": "t", "name": landed.name, "hour": 1, "hours": 1,
                               "vid": "dQw4w9WgXcQ", "started": 0, "elapsed": 60}
        bot.announce_arrivals(state)
        check("and the chat hears again when it actually goes out",
              any("is on now" in t for t in said2) and not state["asked"],
              (said2, state.get("asked")))
        bot.playing = lambda: None
        late = {"asked": {"AAAAAAAAAAA": {"who": "v", "title": "Jamais venue",
                                          "at": time.time() - bot.REQUEST_DEADLINE - 1,
                                          "state": "waiting", "redemption": "r9"}}}
        settled.clear(); said2.clear()
        bot.announce_arrivals(late)
        check("one the board never brought back gives the points back",
              settled == [("r9", False)] and any("points back" in t for t in said2)
              and not late["asked"], (settled, said2))
        supply.excluded = lambda now: {"BBBBBBBBBBB"}
        barred = {"asked": {"BBBBBBBBBBB": {"who": "v", "title": "Refusee",
                                            "at": time.time(), "state": "waiting",
                                            "redemption": "r8"}}}
        settled.clear(); said2.clear()
        bot.announce_arrivals(barred)
        check("one the supply refuses refunds at once, not six hours later",
              settled == [("r8", False)] and any("points back" in t for t in said2)
              and not barred["asked"], (settled, said2))
        full = {"asked": {"V%09d" % i: {"who": "v", "title": "t", "at": time.time(),
                                        "state": "waiting", "redemption": None}
                          for i in range(bot.REQUEST_MAX_PENDING)}}
        ok, why = bot.queue_request(full, time.time(), "v", "CCCCCCCCCCC", "Encore une")
        check("the queue of paid fetches is capped, and the refusal refunds",
              ok is False and "downloading already" in (why or ""), (ok, why))
        check("control: under the cap it goes through",
              bot.queue_request({"asked": {}}, time.time(), "v", "DDDDDDDDDDD", "t")[0])
        twice = {"asked": {"EEEEEEEEEEE": {"who": "first", "title": "Deja demandee",
                                           "at": time.time(), "state": "waiting",
                                           "redemption": "r7"}}}
        ok, why = bot.queue_request(twice, time.time(), "second", "EEEEEEEEEEE", "Deja demandee")
        check("asking for one already on its way refunds rather than strands the first",
              ok is False and "already coming" in (why or "")
              and twice["asked"]["EEEEEEEEEEE"]["redemption"] == "r7", (ok, why, twice))
    finally:
        supply.catalog_titles, bot.shelf, bot.say = real_titles2, real_shelf3, real_say3
        supply.excluded = real_excluded
        supply.REQUESTS.unlink(missing_ok=True)
        landed.unlink(missing_ok=True)
        bot.PICK.unlink(missing_ok=True)
        bot.playing = lambda: {"title": "t", "name": "t.mkv", "hour": 1, "hours": 4,
                               "vid": "x", "started": 0,
                               "elapsed": bot.SKIP_MIN_AIRED + 60}
        state = bot.load()
        state["asked"] = {}
        bot.save(state)

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
        # kil, 2026-09-21: "peru" came back refunded because the one video the
        # loop reached first was already on its way to somebody else
        real_titles3, real_excluded3, real_queue = (supply.catalog_titles,
                                                    supply.excluded, bot.queue_request)
        try:
            bot.shelf = lambda: []
            supply.excluded = lambda now: set()
            supply.catalog_titles = lambda: {"AAAAAAAAAAA": "Day 1 IRL Lima, Peru",
                                             "BBBBBBBBBBB": "Day 2 IRL Cusco, Peru"}
            taken = []
            bot.queue_request = lambda data, now, who, vid, title: (taken.append(vid), (True, None))[1]
            asked = {"asked": {"AAAAAAAAAAA": {"who": "other", "title": "Day 1",
                                               "state": "waiting", "redemption": None}}}
            ok, said = bot.reward_place(asked, 1000, "v", "peru")
            check("a place steps over what is already coming and takes the next one",
                  ok is True and taken == ["BBBBBBBBBBB"], (ok, said, taken))
            asked["asked"]["BBBBBBBBBBB"] = {"who": "other", "title": "Day 2",
                                             "state": "waiting", "redemption": None}
            ok, said = bot.reward_place(asked, 1000, "v", "peru")
            check("control: with every one of them coming already it says so, not "
                  "that nothing was filmed there",
                  ok is False and "already coming" in said, said)
        finally:
            supply.catalog_titles, supply.excluded = real_titles3, real_excluded3
            bot.queue_request = real_queue
            bot.shelf = lambda: [(here, 2)]
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
    bot.shelf, bot.playing = real_unseen, real_playing
    bot.SKIP.unlink(missing_ok=True)

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
