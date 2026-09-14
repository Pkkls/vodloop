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

    def __init__(self, packets, muxer_code=0):
        self.packets, self.muxer_code = packets, muxer_code

    def run(self, args, **kw):
        if args[0] == "ffmpeg":
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

    print("a probe that measured nothing says so, and nothing more")
    # None, not False, and the difference is the whole point. remux_verdict
    # writes down what it is told, so a probe that lost to a busy box and said
    # False got that remembered as a permanent refusal: 22 conformant files went
    # invisible on 2026-09-06 and the rotation fell from 14h to 4.5h while the
    # files sat there, perfect, on the disk.
    prep.subprocess = Probe("")
    check("no packets read is not a verdict",
          prep.remux_is_safe(pathlib.Path("x.mp4")) is None)
    prep.subprocess = Probe("   \n")
    check("nor is whitespace", prep.remux_is_safe(pathlib.Path("x.mp4")) is None)

    print("the remux itself failing")
    prep.subprocess = Probe(CLEAN, muxer_code=1)
    check("ffmpeg exiting non-zero is not a verdict either",
          prep.remux_is_safe(pathlib.Path("x.mp4")) is None)
    # The control for all three. A probe that DID run and DID see the defect has
    # to answer False, or the checks above would pass just as well on a function
    # that had stopped answering at all.
    prep.subprocess = Probe(DIRTY)
    check("a probe that ran and saw the defect still says no",
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
        # ffmpeg cannot open it at all, so there is no measurement to report
        check("a file that is not video yields no verdict",
              prep.remux_is_safe(junk) is None)
        check("a path that does not exist yields no verdict",
              prep.remux_is_safe(pathlib.Path(tmp) / "absent.mp4") is None)

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

# What the probe refused for months was not a broken file, it was an SEI unit
# the transport stream muxer hands out as a packet of its own with no timestamp,
# one behind every keyframe. Dropping SEI is what made those files copyable, so
# the copy paths and the probe all have to carry the same filter: a probe that
# measured a different command than the job runs is worth nothing.
print("the filter that makes the copy playable")
check("it drops SEI, and only SEI",
      prep.common.DROP_SEI == ["-bsf:v", "filter_units=remove_types=6"],
      str(prep.common.DROP_SEI))
check("the probe remuxes the way the job copies",
      '"-an"] + common.DROP_SEI + [' in source)
check("the video-copy path carries it",
      prep.common.REMUX[-2:] == prep.common.DROP_SEI, str(prep.common.REMUX[-2:]))
check("so does the path that copies sound too",
      '["-c:v", "copy", "-c:a", "copy"] + common.DROP_SEI' in source)

# The pair that matters, against real ffmpeg. x264 writes SEI of its own, so a
# filtered elementary stream MUST come out smaller: that is the control proving
# the filter fires at all, without which "pixels unchanged" would also pass on a
# filter that did nothing whatsoever.
with tempfile.TemporaryDirectory() as tmp:
    tmp = pathlib.Path(tmp)
    clip = tmp / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-t", "3", "-i", "testsrc=s=320x180:r=25",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-y", str(clip)],
        check=True, capture_output=True)

    # MPEG-TS, because that is what prep writes. A raw h264 elementary stream
    # comes out of this filter undecodable ("non-existing PPS 0 referenced"),
    # which says nothing about the job and everything about the container: TS
    # carries the parameter sets in band ahead of every keyframe.
    def chunk(name, extra):
        out = tmp / name
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(clip),
             "-an", "-c:v", "copy"] + extra + ["-f", "mpegts", "-y", str(out)],
            check=True, capture_output=True)
        return out

    def pixels(path):
        # framemd5 goes to a file, not to a pipe: ffmpeg refuses the pipe here
        # on Windows and the test has to run wherever the repo is checked out
        sink = path.with_suffix(".framemd5")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-an",
             "-f", "framemd5", "-y", str(sink)],
            check=True, capture_output=True)
        return [l for l in sink.read_text().splitlines()
                if l and not l.startswith("#")]

    plain = chunk("plain.ts", [])
    filtered = chunk("filtered.ts", list(prep.common.DROP_SEI))
    check("the filter really takes bytes out",
          filtered.stat().st_size < plain.stat().st_size,
          f"{plain.stat().st_size} -> {filtered.stat().st_size}")
    before, after = pixels(plain), pixels(filtered)
    check("and every decoded frame survives it untouched",
          before == after and len(before) > 0, f"{len(before)} frames")

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
