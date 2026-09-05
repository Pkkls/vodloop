#!/usr/bin/env python3
"""The cheap path has to be earned, not assumed. Run:

    python3 tests/test_prep_remux_safety.py

matches_target asks whether a library file already carries the right picture, and
when it does prep copies the video instead of re-encoding it. That is the whole
reason the channel keeps up with itself, so it is worth keeping.

What it does not ask is whether copying that particular file comes out playable.
On 2026-09-05 three library files answered yes to matches_target and produced
chunks holding fifty video packets with no PTS at all. MPEG-TS carries those
without complaint, so prep logged a clean job and the chunks sat in the queue
looking fine. The flv muxer at the far end refuses the first one with "Invalid
argument", the pusher exits 1, systemd restarts it onto the same chunk, and it
exits again. 1298 consecutive restarts, the channel black throughout, and
nothing anywhere reporting a fault.

So the property under test is that a file can be the right shape and still be
refused the cheap path. The checks below pair every verdict with its opposite:
without the clean control next to the N/A one, this file would still pass on a
function that had simply stopped saying yes to anything.
"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import prep  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    # Skipping would let this file report success while measuring nothing, which
    # is the exact shape of the failure it was written for.
    print("FAIL  ffmpeg/ffprobe introuvables, le test ne peut rien mesurer")
    sys.exit(1)


class Probe:
    """Stands in for the two commands remux_is_safe runs.

    The defect cannot be produced on demand: it lives in how a particular
    encoder wrote a particular file, and generating one here would be inventing
    a fixture rather than reproducing anything. What can be pinned down is the
    verdict, so the real ffprobe output from the reproduction on 2026-09-05 is
    replayed and the reading of it is what gets checked.
    """
    SubprocessError = subprocess.SubprocessError
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, packets, muxer_code=0, write=True):
        self.packets, self.muxer_code, self.write = packets, muxer_code, write

    def run(self, args, **kw):
        if args[0] == "ffmpeg":
            if self.write:
                pathlib.Path(args[-1]).write_bytes(b"\x47" * 188)
            return subprocess.CompletedProcess(args, self.muxer_code, b"", b"")
        return subprocess.CompletedProcess(args, 0, self.packets, "")


print("what a probe of a bad file looks like")
CLEAN = "0.000000\n0.020000\n0.040000\n0.060000\n"
# one every six seconds, the density measured across all three bad files
DIRTY = "0.000000\n0.020000\nN/A\n0.060000\n"

real_sub = prep.subprocess
try:
    prep.subprocess = Probe(DIRTY)
    check("a packet with no timestamp refuses the cheap path",
          prep.remux_is_safe(pathlib.Path("x.mp4")) is False)

    # The control. Same function, same plumbing, one string changed. Without it
    # the check above would also pass on a remux_is_safe that never says yes,
    # which would re-encode the whole library and lose to the clock forever.
    prep.subprocess = Probe(CLEAN)
    check("timestamps all present keeps it",
          prep.remux_is_safe(pathlib.Path("x.mp4")) is True)

    print("a probe that measured nothing does not get to vote")
    prep.subprocess = Probe("")
    check("no packets read is not a clean bill of health",
          prep.remux_is_safe(pathlib.Path("x.mp4")) is False)
    prep.subprocess = Probe("   \n")
    check("nor is whitespace",
          prep.remux_is_safe(pathlib.Path("x.mp4")) is False)

    print("the remux itself failing")
    prep.subprocess = Probe(CLEAN, muxer_code=1)
    check("ffmpeg exiting non-zero is not safe",
          prep.remux_is_safe(pathlib.Path("x.mp4")) is False)
    prep.subprocess = Probe(CLEAN, write=False)
    check("no file written is not safe",
          prep.remux_is_safe(pathlib.Path("x.mp4")) is False)

    prep.subprocess = real_sub
    print("against real ffmpeg, no stubs")
    with tempfile.TemporaryDirectory() as tmp:
        good = pathlib.Path(tmp) / "good.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-t", "3", "-i", "color=c=0x101014:s=320x180:r=25",
             "-c:v", "libx264", "-preset", "ultrafast", "-y", str(good)],
            check=True, capture_output=True)
        check("a real, well formed file is allowed the cheap path",
              prep.remux_is_safe(good) is True)

        # the other real case: nothing ffmpeg can open at all. Paired with the
        # check above, this shows the plumbing distinguishes files rather than
        # returning one answer whatever it is handed.
        junk = pathlib.Path(tmp) / "junk.mp4"
        junk.write_bytes(b"this is not a video" * 400)
        check("a file that is not video is refused it",
              prep.remux_is_safe(junk) is False)
        check("a path that does not exist is refused it",
              prep.remux_is_safe(pathlib.Path(tmp) / "absent.mp4") is False)

        check("the probe leaves nothing behind",
              not list(pathlib.Path(tempfile.gettempdir()).glob("remuxprobe_*.ts")))
finally:
    prep.subprocess = real_sub

print("how prep uses the answer")
source = (ROOT / "bin" / "prep.py").read_text(encoding="utf-8")
check("the cheap path is gated on the shape AND the copy coming out playable",
      re.search(r"if matches_target\(source\) and remux_is_safe\(source\):", source)
      is not None)
check("and a refused file falls back to the full encode, not to nothing",
      re.search(r"encode = list\(common\.ENCODE\)", source) is not None)
check("the probe is short enough to run on every item",
      0 < prep.REMUX_PROBE_SECONDS <= 30, f"{prep.REMUX_PROBE_SECONDS}s")

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
