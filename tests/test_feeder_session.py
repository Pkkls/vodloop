#!/usr/bin/env python3
"""Opening an RTMP session on the right picture.
Run: python3 tests/test_feeder_session.py

Kick reads the resolution out of the sequence header when a session opens and
advertises it for the whole live, so the first picture the pusher carries names
the channel until that pusher dies. Left to chance that is whatever chunk sat at
the head of the queue, and most of this library is 720p.

pusher.sh writes one line per exec and the feeder opens a session it has not
greeted with filler.ts, which is WIDTH x HEIGHT at FPS by construction. What is
checked here is the decision and nothing else: whether a session is new, and
what goes out because of it. No ffmpeg is involved, which is the point -- the
wire is measured on the live channel and reasoned about here.
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import common  # noqa: E402

root = pathlib.Path(tempfile.mkdtemp(prefix="feeder-session-"))
common.STATE = root / "state"
common.STATE.mkdir()

import feeder  # noqa: E402

feeder.common.STATE = common.STATE

passed = 0
failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"ok   {label}")
    else:
        failed += 1
        print(f"RATE {label} {detail}")


print("which sessions get greeted")
check("a session that differs from the last greeted one is greeted",
      feeder.needs_greeting("1757600000 4321", "1757500000 1234"))
check("the same session is not greeted twice",
      not feeder.needs_greeting("1757600000 4321", "1757600000 4321"))
# The first run after this shipped meets a pusher whose header was read hours
# ago. Greeting it would be twenty seconds of grey for a label already decided.
check("a session with no predecessor on record is left alone",
      not feeder.needs_greeting("1757600000 4321", None))
# An old pusher, or a state directory it could not write, says nothing. Silence
# is not a new session: it is no answer, and it changes nothing.
check("a pusher that says nothing is not a new session",
      not feeder.needs_greeting(None, "1757500000 1234"))
check("and neither is silence on both sides",
      not feeder.needs_greeting(None, None))

print("reading and recording it")
check("with no file written there is no session", feeder.push_session() is None)
(common.STATE / feeder.SESSION).write_text("1757600000 4321\n")
check("the line pusher.sh writes is read back whole",
      feeder.push_session() == "1757600000 4321", feeder.push_session())
feeder.mark_greeted("1757600000 4321")
check("what was greeted survives a feeder restart",
      feeder._read(feeder.GREETED) == "1757600000 4321")
check("so the session it just greeted is not greeted again",
      not feeder.needs_greeting(feeder.push_session(),
                                feeder._read(feeder.GREETED)))
(common.STATE / feeder.SESSION).write_text("1757600100 4399\n")
check("and the next restart of the pusher is",
      feeder.needs_greeting(feeder.push_session(),
                            feeder._read(feeder.GREETED)))

print("what goes out because of it")
filler = pathlib.Path("/clip/filler.ts")
chunks = [pathlib.Path(f"/seg/{n:05d}.ts") for n in range(3)]

source, playing = feeder.next_source(False, chunks, filler)
check("ordinarily the head of the playback order goes out",
      (source, playing) == (chunks[0], chunks[0]), str((source, playing)))

source, playing = feeder.next_source(False, [], filler)
check("a dry queue falls back to the standby clip",
      source == filler and playing is None, str((source, playing)))
check("and the clip is not consumed by playing it", playing is None)

source, playing = feeder.next_source(True, chunks, filler)
check("a greeting sends the clip even with a full queue behind it",
      source == filler, str(source))
# the one that would have deleted filler.ts: the old loop consumed "the source"
# whenever segments existed, and here the source is the clip
check("and the greeting consumes nothing, so the clip is there next time",
      playing is None, str(playing))
check("the chunk that was not sent is still at the head",
      chunks[0] == pathlib.Path("/seg/00000.ts"))

print()
print(f"{passed}/{passed + failed} passent")
sys.exit(0 if failed == 0 else 1)
