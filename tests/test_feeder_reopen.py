#!/usr/bin/env python3
"""When the feeder ends the RTMP session. Run: python3 tests/test_feeder_reopen.py

Kick fixes its ladder from the first picture of a session. Measured 2026-09-12:
opened at 1080p60 it served 720p and 360p files under a 1080p label, opened at
720p60 it had no passthrough at all and topped out at its own 720p encode. The
feeder reopens a session when a chunk would be served below itself, and only
then: a reopening costs viewers six seconds, and one asked on every chunk would
be an outage.
"""
import os
import pathlib
import sys
import tempfile

os.environ["VODLOOP_ROOT"] = tempfile.mkdtemp(prefix="feeder-reopen-")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import common  # noqa: E402
import feeder  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


P1080_60, P1080_30 = (1920, 1080, 60.0), (1920, 1080, 30.0)
P720_60, P720_30, P720_5994 = (1280, 720, 60.0), (1280, 720, 30.0), (1280, 720, 59.94)

print("what counts as going up")
check("1080p60 after a 720p60 opening", feeder.exceeds(P1080_60, P720_60))
check("1080p60 after a 1080p30 opening", feeder.exceeds(P1080_60, P1080_30))
check("temoin: 720p30 after a 1080p60 opening is not", not feeder.exceeds(P720_30, P1080_60))
check("temoin: 1080p30 after a 1080p60 opening is not", not feeder.exceeds(P1080_30, P1080_60))
check("59.94 after a 60 opening is the same rung", not feeder.exceeds(P720_5994, P720_60))

common.STATE.mkdir(parents=True, exist_ok=True)
clock = {"t": 1_000_000.0}
killed = []
world = {"pid": 4242, "profile": P720_60}
feeder.pusher_pid = lambda: world["pid"]
feeder.profile_of = lambda _p: world["profile"]
feeder.os.kill = lambda pid, sig: killed.append(pid)
now = lambda: clock["t"]  # noqa: E731
chunk = pathlib.Path("00516_00000.ts")

print("a session is recorded, then judged")
check("the first chunk of a pusher opens its record, nothing is ended",
      feeder.reopen_if_below(chunk, now=now) is False and killed == [])
check("and the record holds that pusher and that picture",
      feeder.read_session().get("pid") == 4242
      and tuple(feeder.read_session()["profile"]) == P720_60)

world["profile"] = P720_30
check("a lower chunk plays in the same session",
      feeder.reopen_if_below(chunk, now=now) is False and killed == [])

world["profile"] = P1080_60
check("a 1080p60 chunk in a 720p60 session ends it",
      feeder.reopen_if_below(chunk, now=now) is True and killed == [4242], str(killed))
check("and the record is cleared before the signal", feeder.read_session().get("pid") == 0)

print("the pusher systemd brings back")
world["pid"] = 5151
check("the chunk that asked is fed, not asked about again",
      feeder.reopen_if_below(chunk, now=now) is False and killed == [4242], str(killed))
check("and the new session is recorded at its picture",
      feeder.read_session().get("pid") == 5151
      and tuple(feeder.read_session()["profile"]) == P1080_60)
check("temoin: a later 720p chunk stays in it",
      (world.update(profile=P720_30) or feeder.reopen_if_below(chunk, now=now)) is False
      and killed == [4242])

print("never twice in a window")
feeder.write_session({"pid": 5151, "profile": list(P720_60), "reopened_at": clock["t"]})
world["profile"] = P1080_60
clock["t"] += 60
check("going up a minute after a reopening does nothing",
      feeder.reopen_if_below(chunk, now=now) is False and killed == [4242], str(killed))
clock["t"] += feeder.REOPEN_GAP_SECONDS
check("temoin: past the window it does",
      feeder.reopen_if_below(chunk, now=now) is True and killed == [4242, 5151], str(killed))

print("what never ends a session")
world.update(pid=6161, profile=P720_60)
feeder.reopen_if_below(chunk, now=now)
world["profile"] = P1080_60
check("the filler going up does not",
      feeder.reopen_if_below(pathlib.Path("filler.ts"), may_reopen=False, now=now) is False
      and killed == [4242, 5151])
world["pid"] = 0
check("no pusher running, nothing is signalled",
      feeder.reopen_if_below(chunk, now=now) is False and killed == [4242, 5151])
world.update(pid=6161, profile=None)
check("a chunk that cannot be probed, nothing is signalled",
      feeder.reopen_if_below(chunk, now=now) is False and killed == [4242, 5151])


def refuse(pid, sig):
    raise PermissionError("not allowed")


world.update(pid=7171, profile=P720_60)
clock["t"] += feeder.REOPEN_GAP_SECONDS
feeder.reopen_if_below(chunk, now=now)
feeder.os.kill = refuse
world["profile"] = P1080_60
check("a signal that fails reports nothing ended",
      feeder.reopen_if_below(chunk, now=now) is False)
check("and leaves the session as it was",
      feeder.read_session().get("pid") == 7171
      and tuple(feeder.read_session()["profile"]) == P720_60, str(feeder.read_session()))

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
