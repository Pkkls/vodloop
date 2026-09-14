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


def served(text):
    real = quality.urllib.request.urlopen
    quality.urllib.request.urlopen = lambda *_a, **_k: _Answer(text)
    try:
        return quality.ladder("https://example.invalid/master.m3u8")
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

print("a label Kick froze at the start of the session")
# Measured 2026-09-12 21:17: session opened at 1080p60, the chunked rung still
# labelled 1920x1080 at 60 while its segments decoded to 640x360, and then to
# 1280x720 once the next file aired.
STALE = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=3422999,RESOLUTION=1280x720,FRAME-RATE=60.000,VIDEO="720p60"
https://example.invalid/720p60.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1427999,RESOLUTION=852x480,FRAME-RATE=30.000,VIDEO="480p30"
https://example.invalid/480p30.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1365409,RESOLUTION=1920x1080,FRAME-RATE=60.000,VIDEO="chunked"
https://example.invalid/chunked.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=630000,RESOLUTION=640x360,FRAME-RATE=30.000,VIDEO="360p30"
https://example.invalid/360p30.m3u8
"""


def served_pushed(text, pushed):
    real = quality.urllib.request.urlopen
    quality.urllib.request.urlopen = lambda *_a, **_k: _Answer(text)
    try:
        return quality.ladder("https://example.invalid/master.m3u8", pushed)
    finally:
        quality.urllib.request.urlopen = real


def rung_fault(row):
    return any("RUNG" in f for f in quality.faults({"chunk": "x.ts", "duration": 0, **row}))


# Pushing 720p, Kick's own 720p60 has the same pixels and costs a generation,
# not a picture: it must not be the rung counted against the source. The 480p30
# one is smaller, and measured at 21:30 with the 720p file on air the frozen
# source rung advertised 1 321 933 against its 1 427 999, so that one is real.
at_720 = served_pushed(STALE, (1280, 720))
check("pushing 720p, the same-size 720p60 rung no longer counts against it",
      at_720.get("rung_best_smaller_bps") == 1427999, str(at_720.get("rung_best_smaller_bps")))
check("and the 480p30 rung outbidding it is still reported", rung_fault(at_720))
check("temoin: against the frozen label, the 720p60 rung was the one counted",
      served_pushed(STALE, None).get("rung_best_smaller_bps") == 3422999)
check("temoin: a real 1080p push counts the 720p60 rung",
      served_pushed(STALE, (1920, 1080)).get("rung_best_smaller_bps") == 3422999)
check("an unreadable chunk falls back to the label",
      served_pushed(STALE, (None, None)).get("rung_best_smaller_bps") == 3422999)
# built, not measured: the case the change exists for, a 720p source that beats
# every smaller rung and is only outbid by Kick's encode at its own size
LEADS_SMALLER = STALE.replace("BANDWIDTH=1365409", "BANDWIDTH=2400000")
check("a 720p source outbid only at its own size is not called an upscale",
      not rung_fault(served_pushed(LEADS_SMALLER, (1280, 720))))
check("temoin: against the frozen label that same ladder raised the alarm",
      rung_fault(served_pushed(LEADS_SMALLER, None)))

print("a session opened below 1080p")
# Measured 2026-09-12 22:31 on a session opened at 720p60: no chunked rung.
NO_PASSTHROUGH = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=3422999,RESOLUTION=1280x720,FRAME-RATE=60.000,VIDEO="720p60"
https://example.invalid/720p60.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1427999,RESOLUTION=852x480,FRAME-RATE=30.000,VIDEO="480p30"
https://example.invalid/480p30.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=630000,RESOLUTION=640x360,FRAME-RATE=30.000,VIDEO="360p30"
https://example.invalid/360p30.m3u8
"""
same = served_pushed(NO_PASSTHROUGH, (1280, 720))
check("720p on air in a 720p session is not a fault", not quality.faults(
    {"chunk": "x.ts", "duration": 0, **same}), str(quality.faults({"chunk": "x.ts", "duration": 0, **same})))
check("and the top it is served at is recorded",
      (same.get("rung_width"), same.get("rung_height")) == (1280, 720), str(same))
bigger = served_pushed(NO_PASSTHROUGH, (1920, 1080))
check("1080p on air in that session is reported, every viewer is downscaled",
      any("ladder" in f for f in quality.faults({"chunk": "x.ts", "duration": 0, **bigger})))
check("temoin: with nothing known about the chunk, nothing is claimed",
      not served_pushed(NO_PASSTHROUGH, None).get("ladder_error"))

print("the height the channel asked for")
# The downloader falls through to a lower rendition when the wanted one is too
# large for the fetching board. The fallback works, so nothing calls it an error,
# and four of ten library files were 720p before anyone noticed. This says so.
essai = {"rung_width": 1280, "rung_height": 720, "rung_fps": 60,
         "rung_bps": 2277000, "rung_count": 5, "rung_best_smaller_bps": None}
avant = quality.WANTED_HEIGHT
try:
    quality.WANTED_HEIGHT = 1080
    dit = quality.faults({"chunk": "x.ts", "duration": 0, **essai})
    check("720p on the wire when 1080p was asked for is a fault",
          any("720p" in f and "1080p" in f for f in dit), str(dit))

    # the control, twice: the same sample at the wanted height is silent, and so
    # is any sample when the channel has stated no preference. Without these the
    # check above would pass on a function that complains about everything.
    bon = dict(essai, rung_width=1920, rung_height=1080)
    check("temoin: the same sample at 1080p says nothing",
          not quality.faults({"chunk": "x.ts", "duration": 0, **bon}),
          str(quality.faults({"chunk": "x.ts", "duration": 0, **bon})))
    quality.WANTED_HEIGHT = 0
    check("temoin: with no height asked for it stays quiet",
          not quality.faults({"chunk": "x.ts", "duration": 0, **essai}),
          str(quality.faults({"chunk": "x.ts", "duration": 0, **essai})))
finally:
    quality.WANTED_HEIGHT = avant

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
