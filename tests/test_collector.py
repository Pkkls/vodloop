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
    # sources() normalises to dicts, so a stub has to hand back that shape
    collector.sources = lambda: [{"url": u, "match": []}
                                 for u in ("src-a", "src-b", "src-c")]
    collector.pool = lambda: {
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
    collector.sources = lambda: [{"url": "src-a", "match": []}]
    collector.pool = lambda: {"src-a": {"ids": A}}
    solo = collector.pick(4, set())
    check("with one source it simply takes them in order", solo == A[:4], str(solo))

    print("what is already known is skipped")
    # sources() normalises to dicts, so a stub has to hand back that shape
    collector.sources = lambda: [{"url": u, "match": []}
                                 for u in ("src-a", "src-b", "src-c")]
    collector.pool = lambda: {
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

    print("filtering a source by title")
    listing = ("aaaaaaaaaaa\t1800\tIce Poseidon in Tokyo\n"
               "bbbbbbbbbbb\t600\tSomeone else entirely\n"
               "ccccccccccc\t43000\tCX247 highlights\n"
               "ddddddddddd\tNA\tA cooking show\n")

    class _Stub:
        SubprocessError = RuntimeError

        @staticmethod
        def run(*_a, **_kw):
            return type("R", (), {"stdout": listing, "returncode": 0})()

    real_sub, collector.subprocess = collector.subprocess, _Stub
    try:
        kept, secs = collector.list_source(
            {"url": "u", "match": ["ice poseidon", "cx"]})
        check("only the matching titles are kept",
              kept == ["aaaaaaaaaaa", "ccccccccccc"], str(kept))
        check("the match is case insensitive",
              "ccccccccccc" in collector.list_source(
                  {"url": "u", "match": ["cx247"]})[0])

        # the control: the same listing with no filter must keep everything,
        # otherwise the check above could be passing on parsing that drops rows
        # for some reason of its own rather than on the filter
        everything, all_secs = collector.list_source({"url": "u", "match": []})
        check("without a filter the whole source is kept",
              len(everything) == 4, str(everything))

        print("durations come off the same listing")
        check("a duration is read alongside the id",
              secs.get("aaaaaaaaaaa") == 1800 and secs.get("ccccccccccc") == 43000,
              str(secs))
        # NA is what yt-dlp prints for a live or hidden entry. Stored as zero it
        # would sort ahead of every real video and be fetched first, which is the
        # opposite of what asking for the shortest one means
        check("an unreadable duration is left out, not stored as zero",
              "ddddddddddd" in everything and "ddddddddddd" not in all_secs,
              str(all_secs))
    finally:
        collector.subprocess = real_sub

    check("changing the words invalidates the cached list",
          collector.cache_key({"url": "u", "match": ["a"]})
          != collector.cache_key({"url": "u", "match": ["b"]}))
    check("an unfiltered source keys on its url alone",
          collector.cache_key({"url": "u", "match": []}) == "u")

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

    print("choosing by duration when the runway is short")
    # b is the shortest, then a, then c. In source order it is a, b, c: if the
    # picker were ignoring the durations it would hand back a first, so the
    # order below is what proves the duration is being read.
    collector.sources = lambda: [{"url": u, "match": []}
                                 for u in ("src-a", "src-b", "src-c")]
    collector.pool = lambda: {
        "src-a": {"ids": A, "dur": {v: 3600 for v in A}},
        "src-b": {"ids": B, "dur": {v: 600 for v in B}},
        "src-c": {"ids": C, "dur": {v: 43000 for v in C}}}
    urgent = collector.pick(3, set(), shortest=True)
    check("the shortest videos are taken first",
          [v[0] for v in urgent] == list("bbb"), str(urgent))

    # the control: the same pool, same call, without the flag. If this also came
    # out shortest first the check above would be measuring the fixture.
    calm = collector.pick(3, set())
    check("without the flag it still rotates the sources",
          [v[0] for v in calm] == list("abc"), str(calm))

    collector.pool = lambda: {
        "src-a": {"ids": A, "dur": {A[0]: 900}},
        "src-b": {"ids": B, "dur": {}}, "src-c": {"ids": C, "dur": {}}}
    partial = collector.pick(3, set(), shortest=True)
    check("an unmeasured video is not offered as a short one",
          partial == [A[0]], str(partial))

    collector.pool = lambda: {"src-a": {"ids": A}, "src-b": {"ids": B},
                              "src-c": {"ids": C}}
    blind = collector.pick(3, set(), shortest=True)
    check("with no duration anywhere it queues something rather than nothing",
          len(blind) == 3, str(blind))

    print("reading the board's own report")
    real_status, collector.STATUS_FILE = collector.STATUS_FILE, pathlib.Path("/nonexistent")
    try:
        check("no report at all is unknown, not zero",
              collector.board_queue() is None)
    finally:
        collector.STATUS_FILE = real_status

    fresh = {"queue": 7, "at": 1000}
    real_load, collector.load = collector.load, lambda *_a, **_k: fresh
    try:
        check("a fresh report is believed", collector.board_queue(now=1060) == 7)
        # the board publishes every five minutes. Past the staleness window the
        # file records the last time it could be reached, not what it holds now,
        # and a count read off it would be a guess presented as a measurement
        check("a stale report is unknown, not the number it last said",
              collector.board_queue(
                  now=1000 + collector.STATUS_STALE_SECONDS + 1) is None)
    finally:
        collector.load = real_load
finally:
    collector.sources = real_sources
    collector.pool = real_pool

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
