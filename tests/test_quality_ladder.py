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
check("the fattest rung that is not the source is what it is measured against",
      broken.get("rung_best_other_bps") == 3422999,
      str(broken.get("rung_best_other_bps")))

print("the fault it exists for")
found = quality.faults({"chunk": "x.ts", "duration": 0, **broken})
check("a source outbid by one of Kick's encodes is reported",
      any("RUNG" in f for f in found), str(found))
# the alert de-dupes on the text, so a fault that stands for hours has to read
# the same on every sample or it becomes a message every five minutes
worse = dict(broken, rung_bps=3000000, rung_best_other_bps=3422999)
check("the message does not move with the numbers",
      [f for f in found if "RUNG" in f]
      == [f for f in quality.faults({"chunk": "x.ts", "duration": 0, **worse})
          if "RUNG" in f])

print("the control: the same ladder once the source led it")
healthy = served(HEALTHY)
check("the healthy source rung is the fattest",
      healthy["rung_bps"] > healthy["rung_best_other_bps"],
      f"{healthy['rung_bps']} vs {healthy['rung_best_other_bps']}")
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

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
