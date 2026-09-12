#!/usr/bin/env python3
"""What Kick advertises. Run: python3 tests/test_quality_ladder.py

The channel can be live, the chunks can be perfect copies of their source, and
the picture can still arrive as an upscaled bitmap, because a player does not
take the rung that was pushed, it takes the fattest rung the ladder offers. On
2026-09-08 the source rung advertised 3 070 272 while Kick's own 720p60
advertised 3 422 999, so every player picked an encode of the source and blew it
back up to 1080. Nothing in the pipeline was wrong and nothing reported it.

The two playlists below are that real failure and the real recovery that
followed it, so the check has a case it must catch and a case it must not.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import quality  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


# Measured on the live channel while the library was still 50 fps. The source
# rung is not first, and it is not the fattest: both of those are the point.
BROKEN = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=3422999,RESOLUTION=1280x720,FRAME-RATE=60.000,VIDEO="720p60"
https://example.invalid/720p60.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3070272,RESOLUTION=1920x1080,FRAME-RATE=50.000,VIDEO="chunked"
https://example.invalid/chunked.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=628000,RESOLUTION=640x360,FRAME-RATE=30.000,VIDEO="360p30"
https://example.invalid/360p30.m3u8
"""

# The same ladder once the source went to 60 fps: the source leads it.
HEALTHY = BROKEN.replace("BANDWIDTH=3070272,RESOLUTION=1920x1080,FRAME-RATE=50.000",
                         "BANDWIDTH=4295657,RESOLUTION=1920x1080,FRAME-RATE=60.000")


class _Answer:
    def __init__(self, text):
        self.status = 200
        self._text = text

    def read(self, _n=None):
        return self._text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def served(text, airing=None):
    real = quality.urllib.request.urlopen
    quality.urllib.request.urlopen = lambda *_a, **_k: _Answer(text)
    try:
        return quality.ladder("https://example.invalid/master.m3u8", airing=airing)
    finally:
        quality.urllib.request.urlopen = real


print("reading the ladder")
broken = served(BROKEN)
check("the source rung is found by its VIDEO group, not by position",
      (broken.get("rung_width"), broken.get("rung_height")) == (1920, 1080),
      str(broken))
check("its frame rate is read as a number",
      broken.get("rung_fps") == 50.0, str(broken.get("rung_fps")))
check("every rung is counted", broken.get("rung_count") == 3,
      str(broken.get("rung_count")))
check("the fattest SMALLER rung is what it is measured against",
      broken.get("rung_best_smaller_bps") == 3422999,
      str(broken.get("rung_best_smaller_bps")))

# Kick also publishes an encode at the source's own resolution. Outbid by that
# one a viewer loses a generation and not a pixel, and for a 690 kbps 720p30
# source, which is what YouTube serves for the long IRL VODs in this pool, it
# would be true on every sample of every one of its eleven hours.
SAME_SIZE = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=3422999,RESOLUTION=1280x720,FRAME-RATE=60.000,VIDEO="720p60"
https://example.invalid/720p60.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=760000,RESOLUTION=1280x720,FRAME-RATE=30.000,VIDEO="chunked"
https://example.invalid/chunked.m3u8
"""
thin_720 = served(SAME_SIZE)
check("a rung at the source's own size does not count as outbidding it",
      thin_720.get("rung_best_smaller_bps") is None, str(thin_720))
check("and so it is not reported",
      not any("RUNG" in f for f in quality.faults(
          {"chunk": "x.ts", "duration": 0, **thin_720})))

print("the label Kick freezes at the start of the session")
# Observed on the live channel 2026-09-12. Kick reads the resolution out of the
# sequence header when the RTMP session opens and advertises it for the whole
# live, so the source rung is still announced 1920x1080 hours after the mixed
# library started sending 720p through it. The playlist below is the SAME_SIZE
# situation above -- a 720p source and Kick's 720p60 encode of it -- wearing the
# label of the 1080p file the session happened to open on.
FROZEN = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=3422999,RESOLUTION=1280x720,FRAME-RATE=60.000,VIDEO="720p60"
https://example.invalid/720p60.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=760000,RESOLUTION=1920x1080,FRAME-RATE=60.000,VIDEO="chunked"
https://example.invalid/chunked.m3u8
"""
stale = served(FROZEN, airing=(1280, 720))
check("measured against the wire, a rung at the source's own size is no fault",
      stale.get("rung_best_smaller_bps") is None, str(stale))
check("and nothing is reported",
      not any("RUNG" in f for f in quality.faults(
          {"chunk": "x.ts", "duration": 0, **stale})))
check("both sizes are kept, so the gap can be read back",
      (stale.get("rung_width"), stale.get("air_width")) == (1920, 1280), str(stale))
check("and the gap is named for the detail block",
      quality.frozen_label(stale))

# the control, and the reason airing exists at all: taken from Kick's label the
# same channel reads as a 1080p source outbid by a 720p rung, which is a
# standing alert every six hours for the eleven hours one of these VODs lasts.
believed = served(FROZEN)
check("taken from the label alone it is a false alarm",
      any("RUNG" in f for f in quality.faults(
          {"chunk": "x.ts", "duration": 0, **believed})), str(believed))
# and the other control: when the wire agrees with the label there is nothing to
# tell apart, and a genuinely outbid 1080p source is still reported
truly_1080 = served(BROKEN, airing=(1920, 1080))
check("a real 1080p source outbid by a 720p rung is still reported",
      any("RUNG" in f for f in quality.faults(
          {"chunk": "x.ts", "duration": 0, **truly_1080})), str(truly_1080))
check("agreement between the two is not called a frozen label",
      not quality.frozen_label(truly_1080))
check("nothing measured on the wire is not called one either",
      not quality.frozen_label(believed))

print("what counts as on the wire")
import tempfile  # noqa: E402
air_root = pathlib.Path(tempfile.mkdtemp(prefix="quality-air-"))
quality.common.ROOT = air_root
quality.common.SEGMENTS = air_root / "segments"
quality.common.SEGMENTS.mkdir()
check("with nothing ready and no clip there is nothing to measure",
      quality.airing_file() is None)
(air_root / "filler.ts").write_text("clip")
check("with nothing ready the standby clip is what is going out",
      quality.airing_file() == air_root / "filler.ts", str(quality.airing_file()))
for name in ("00007_00003.ts", "00007_00004.ts", "00009_00000.ts"):
    (quality.common.SEGMENTS / name).write_text("chunk")
# the feeder takes the head of the list and deletes it once it has been sent, so
# the head is on the wire. newest_chunk() is what prep last wrote, which is up to
# AHEAD_LIMIT_SECONDS ahead of the viewer and answers a different question.
check("the head of the playback order is what is on the wire",
      quality.airing_file().name == "00007_00003.ts",
      quality.airing_file().name)

print("the fault it exists for")
found = quality.faults({"chunk": "x.ts", "duration": 0, **broken})
check("a source outbid by one of Kick's encodes is reported",
      any("RUNG" in f for f in found), str(found))
# the alert de-dupes on the text, so a fault that stands for hours has to read
# the same on every sample or it becomes a message every five minutes
worse = dict(broken, rung_bps=3000000, rung_best_smaller_bps=3422999)
check("the message does not move with the numbers",
      [f for f in found if "RUNG" in f]
      == [f for f in quality.faults({"chunk": "x.ts", "duration": 0, **worse})
          if "RUNG" in f])

print("the control: the same ladder once the source led it")
healthy = served(HEALTHY)
check("the healthy source rung is the fattest",
      healthy["rung_bps"] > healthy["rung_best_smaller_bps"],
      f"{healthy['rung_bps']} vs {healthy['rung_best_smaller_bps']}")
quiet = quality.faults({"chunk": "x.ts", "duration": 0, **healthy})
check("and it says nothing", not any("RUNG" in f for f in quiet), str(quiet))

print("the floors, against a source that is simply thin")
# Measured 2026-09-08: an 11 h 15 IRL stream is served by YouTube at 690 kbps in
# 720p30, so the catastrophe floor of 1 Mbps sits above a real, untouched
# arrival. Every byte of it is a copy, which is the whole point of the pipeline,
# and reporting it as a loss is what the two previous floors here did.
thin = {"chunk": "x.ts", "duration": 300, "video_bps": 690_000,
        "abitrate": 129_000, "source": "s.mp4",
        "copied_video": True, "copied_audio": True,
        "video_ratio": 1.0, "audio_ratio": 1.0}
check("a thin chunk that matched its source is content, not a fault",
      quality.faults(thin) == [], str(quality.faults(thin)))

# the control: the same numbers, but nothing says they were copied. Then there
# is no source to explain them and the net has to stay up.
loose = dict(thin, copied_video=False, copied_audio=False)
check("the same numbers with no copy behind them are still reported",
      any("video a" in f for f in quality.faults(loose)),
      str(quality.faults(loose)))

no_source = {"chunk": "x.ts", "duration": 300, "video_bps": 690_000}
check("with no source at all the floor is what is left",
      any("video a" in f for f in quality.faults(no_source)),
      str(quality.faults(no_source)))

print("when it cannot see")
check("a playlist with no chunked rung is named, not silently passed",
      served("#EXTM3U\n").get("ladder_error") is not None)
check("no playback url at all is not a fault, it is nothing to measure",
      quality.ladder(None) == {})

unreadable = quality.faults({"chunk": "x.ts", "duration": 0,
                             "ladder_error": "playlist illisible"})
check("a ladder that could not be read is a fault, not a silence",
      any("ladder" in f for f in unreadable), str(unreadable))

print("how loudly it says it")
# A source too thin to lead the ladder stays too thin for as long as it is on
# air, up to eleven hours in this pool. At the ordinary cooldown that is a
# message every half hour about something nobody can act on.
rung_only = [f for f in quality.faults({"chunk": "x.ts", "duration": 0, **broken})
             if "RUNG" in f]
check("the standing fault is the whole of what would be sent",
      rung_only == quality.faults({"chunk": "x.ts", "duration": 0, **broken}),
      str(rung_only))
check("and it is marked so the alert can slow it down",
      all(f.startswith(quality.STANDING_PREFIX) for f in rung_only))
check("a standing fault waits far longer than an ordinary one",
      quality.STANDING_COOLDOWN_SECONDS > quality.ALERT_COOLDOWN_SECONDS,
      f"{quality.STANDING_COOLDOWN_SECONDS} > {quality.ALERT_COOLDOWN_SECONDS}")
# the control: a breakage alongside it is a new situation and must not inherit
# the slow cadence
mixed = quality.faults({"chunk": "x.ts", "duration": 0, "chunk_error": "illisible",
                        **broken})
check("mixed with a real breakage it is not a standing fault any more",
      not all(f.startswith(quality.STANDING_PREFIX) for f in mixed), str(mixed))

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
