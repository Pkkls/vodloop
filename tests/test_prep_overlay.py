#!/usr/bin/env python3
"""Title overlay. Run: python3 tests/test_prep_overlay.py

Two claims are worth a test here.

The graph must stay free of the title. A VOD name comes from YouTube, so it is
attacker-influenced text, and the moment it is spliced into the filter string a
title carrying a quote or a comma rewrites the graph. The defence is that the
text only ever reaches ffmpeg through a file. A test that just looked for
"drawtext" would pass while that defence was gone, so the check below feeds a
hostile title and asserts none of it appears in the returned string.

The second is the "next" box, which must vanish rather than lie. When nothing
follows, a leftover file from the previous item is the failure mode: the overlay
would draw the last video's successor over this one.
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import common  # noqa: E402
import prep  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


HOSTILE = "':drawtext=text=pwned:x=0,%{gmtime}\\ and a comma, too"

with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    now = root / "title_00001.txt"
    nxt = root / "next_00001.txt"
    now.write_text(HOSTILE, encoding="utf-8")
    nxt.write_text("a suivre  " + HOSTILE, encoding="utf-8")

    # only the presence of the path is tested by the code, never its contents,
    # so a stand-in keeps these assertions meaningful off the target machine
    stand_in_font = root / "font.ttf"
    stand_in_font.write_bytes(b"not a real font")
    real_font, prep.FONT = prep.FONT, str(stand_in_font)

    print("overlay graph")
    try:
        both = prep.overlay_filter(now, nxt)
        one = prep.overlay_filter(now, root / "does_not_exist.txt")

        check("two boxes when something follows", both.count("drawtext=") == 2)
        check("one box when nothing follows", one.count("drawtext=") == 1)
        check("second box is smaller than the first",
              "fontsize=23" in both and "fontsize=17" in both)
        check("second box is dimmer than the first",
              "white@0.92" in both and "white@0.60" in both)
        check("upper box sits a line higher", "h-th-58" in both and "h-th-26" in both)
        check("single box sits on the baseline", "h-th-28" in one)
        check("expansion stays off, so %{...} in a title is drawn not evaluated",
              both.count("expansion=none") == 2)

        # the control: the hostile title is in the file the graph points at, so
        # a test that cannot see it here is measuring something real
        check("title file really holds the hostile text", HOSTILE in now.read_text(encoding="utf-8"))
        leaked = [frag for frag in ("pwned", "%{gmtime}", "comma, too") if frag in both]
        check("no fragment of the title reaches the graph", not leaked, str(leaked))

        print("caption baked at normalisation")
        captured = {}

        class _Stub:
            """Stands in for the subprocess module: records argv, encodes nothing."""
            SubprocessError = RuntimeError

            @staticmethod
            def run(argv, **_kw):
                captured["argv"] = list(argv)
                return type("R", (), {"returncode": 1, "stderr": "stub"})()

        def vf_of(argv):
            return argv[argv.index("-vf") + 1] if "-vf" in argv else ""

        source = root / "video.mp4"
        source.write_bytes(b"not a real video")
        real_sub, prep.subprocess = prep.subprocess, _Stub
        try:
            prep.normalise_in_place(source, now)
            burned = vf_of(captured.get("argv", []))
            check("normalisation burns the name into the library copy",
                  "drawtext=" in burned)
            check("exactly one box is baked, never the 'next' line",
                  burned.count("drawtext=") == 1)

            # the control: same call, no title file. If this still drew a box the
            # assertion above would be passing on something other than the title.
            captured.clear()
            prep.normalise_in_place(source, None)
            check("no title file, nothing is burned",
                  "drawtext=" not in vf_of(captured.get("argv", [])))
        finally:
            prep.subprocess = real_sub

        print("the ledger of what already carries its title")
        real_state3, common.STATE = common.STATE, root / "capstate"
        common.STATE.mkdir(parents=True, exist_ok=True)
        try:
            check("nothing is captioned before anything is recorded",
                  prep.captioned() == set(), str(prep.captioned()))
            prep.mark_captioned("already_done.mp4")
            check("a recorded file is remembered",
                  "already_done.mp4" in prep.captioned())
            check("an unrecorded one still wants a pass",
                  "never_touched.mp4" not in prep.captioned())

            # A remux cannot draw, so a conformant file is only ever captioned
            # during a normalisation. Marking one that was normalised without a
            # title file would strand it: conformant, uncaptioned, never redone.
            captured2 = {}

            class _S2:
                SubprocessError = RuntimeError

                @staticmethod
                def run(argv, **_kw):
                    captured2["argv"] = list(argv)
                    # a real ffmpeg leaves an output file behind, and
                    # normalise_in_place checks for it before believing the
                    # return code. A stub that skips this reports a failure the
                    # code does not have.
                    pathlib.Path(argv[-1]).write_bytes(b"encoded")
                    return type("R", (), {"returncode": 0, "stderr": ""})()

            src2 = root / "capsrc.mp4"
            src2.write_bytes(b"x")
            real_sub2, prep.subprocess = prep.subprocess, _S2
            try:
                prep.normalise_in_place(src2, now)
                check("normalising with a title records the file",
                      "capsrc.mp4" in prep.captioned(), str(sorted(prep.captioned())))

                # the control: same call, no title file. If this also recorded,
                # the flag would mean "normalised" rather than "captioned".
                src3 = root / "capsrc3.mp4"
                src3.write_bytes(b"x")
                prep.normalise_in_place(src3, None)
                check("normalising without a title records nothing",
                      "capsrc3.mp4" not in prep.captioned(),
                      str(sorted(prep.captioned())))
            finally:
                prep.subprocess = real_sub2
        finally:
            common.STATE = real_state3

        print("missing font")
        prep.FONT = str(root / "no-such-font.ttf")
        bare = prep.overlay_filter(now, nxt)
        check("falls back to the plain video filter", bare == common.VFILTER)
    finally:
        prep.FONT = real_font

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
