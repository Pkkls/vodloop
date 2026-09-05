#!/usr/bin/env python3
"""Source rotation. Run: python3 tests/test_collector.py

The library is fed from several playlists and a channel of a few thousand
videos. Taking them in order would put one playlist on air for days, then the
next, which on a rerun channel reads as a far smaller library than it is. The
sources are therefore taken in turn.

The check that matters is not "it returned some ids". It is that ids from
different sources interleave, and the control below runs the same function over
a single source to show the interleaving comes from the rotation and not from
the order the ids happened to be in.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import collector  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def ids(prefix, n):
    # 11 characters, the shape yt-dlp yields
    return [f"{prefix}{i:010d}" for i in range(n)]


A, B, C = ids("a", 5), ids("b", 5), ids("c", 5)

real_sources = collector.sources
real_pool = collector.pool
try:
    collector.sources = lambda: ["src-a", "src-b", "src-c"]
    collector.pool = lambda refresh=True: {
        "src-a": {"ids": A}, "src-b": {"ids": B}, "src-c": {"ids": C}}

    print("rotation")
    got = collector.pick(6, set())
    check("takes from every source, not just the first",
          len({v[0] for v in got}) == 3, str([v[0] for v in got]))
    check("alternates one at a time", [v[0] for v in got] == list("abcabc"),
          "".join(v[0] for v in got))

    print("a single source, for comparison")
    # the control: same function, one source. If this also came out interleaved
    # the check above would be measuring the fixture, not the rotation.
    collector.sources = lambda: ["src-a"]
    collector.pool = lambda refresh=True: {"src-a": {"ids": A}}
    solo = collector.pick(4, set())
    check("with one source it simply takes them in order", solo == A[:4], str(solo))

    print("what is already known is skipped")
    collector.sources = lambda: ["src-a", "src-b", "src-c"]
    collector.pool = lambda refresh=True: {
        "src-a": {"ids": A}, "src-b": {"ids": B}, "src-c": {"ids": C}}
    known = set(A[:3]) | set(B)
    got = collector.pick(4, known)
    check("nothing already held is offered again",
          not (set(got) & known), str(got))
    check("an exhausted source does not stall the others",
          len(got) == 4, str(got))

    print("running out")
    got = collector.pick(99, set(A) | set(B) | set(C))
    check("everything known yields nothing rather than looping", got == [], str(got))

    print("reading ids back off filenames")
    real_lib, collector.LIBRARY = collector.LIBRARY, pathlib.Path("/nonexistent")
    try:
        check("a missing library is empty, not an error",
              collector.library_ids() == set())
    finally:
        collector.LIBRARY = real_lib
    check("the yt-dlp naming convention is what is parsed",
          collector.VIDEO_ID.search("Some_Title-abcDEF12345.mp4").group(1)
          == "abcDEF12345")
    check("a name without an id is not mistaken for one",
          collector.VIDEO_ID.search("no_id_here.mp4") is None)
finally:
    collector.sources = real_sources
    collector.pool = real_pool

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
