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
    # all three above MIN_USEFUL_SECONDS, so this measures the ordering alone
    collector.pool = lambda: {
        "src-a": {"ids": A, "dur": {v: 3600 for v in A}},
        "src-b": {"ids": B, "dur": {v: 1500 for v in B}},
        "src-c": {"ids": C, "dur": {v: 43000 for v in C}}}
    urgent = collector.pick(3, set(), shortest=True)
    check("the shortest videos are taken first",
          [v[0] for v in urgent] == list("bbb"), str(urgent))

    print("but not videos too short to pay for the trip")
    # A fetch costs the board about ten minutes whatever it returns, so a clip
    # adds nothing and spends the same. Left out, this asked for exactly the
    # worst videos in the pool and got three of 17, 26 and 17 seconds: one
    # minute of air for three complete cycles, on 2026-09-09.
    collector.pool = lambda: {
        "src-a": {"ids": A, "dur": {v: 3600 for v in A}},
        "src-b": {"ids": B, "dur": {v: 20 for v in B}},
        "src-c": {"ids": C, "dur": {v: 1500 for v in C}}}
    picked = collector.pick(3, set(), shortest=True)
    check("a 20 second clip is refused even though it is the shortest",
          not any(v[0] == "b" for v in picked), str(picked))
    check("and the shortest one above the floor is taken",
          [v[0] for v in picked] == list("ccc"), str(picked))

    # the control: raise the clips above the floor and they win again, so the
    # check above is measuring the floor and not some other exclusion
    collector.pool = lambda: {
        "src-a": {"ids": A, "dur": {v: 3600 for v in A}},
        "src-b": {"ids": B, "dur": {v: collector.MIN_USEFUL_SECONDS for v in B}},
        "src-c": {"ids": C, "dur": {v: 1500 for v in C}}}
    check("temoin: juste au-dessus du plancher, ils repassent devant",
          [v[0] for v in collector.pick(3, set(), shortest=True)] == list("bbb"))

    # and when nothing at all clears the floor, a short video beats no video
    collector.pool = lambda: {
        "src-a": {"ids": A, "dur": {v: 30 for v in A}},
        "src-b": {"ids": B, "dur": {v: 20 for v in B}},
        "src-c": {"ids": C, "dur": {v: 40 for v in C}}}
    check("un pool entierement sous le plancher donne quand meme quelque chose",
          len(collector.pick(3, set(), shortest=True)) == 3)

    collector.pool = lambda: {
        "src-a": {"ids": A, "dur": {v: 3600 for v in A}},
        "src-b": {"ids": B, "dur": {v: 600 for v in B}},
        "src-c": {"ids": C, "dur": {v: 43000 for v in C}}}

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

    print("when it is not starving, it goes for variety")
    # The disk holds about the same number of hours whatever is on it, so what a
    # duration band changes is how many DISTINCT videos those hours are. On the
    # real pool of 4570: four hours and over gives five videos on the disk,
    # twenty to sixty minutes gives fifty five. Nearly half the pool is four
    # hours or more, so taking it as it comes builds the poorest rotation there
    # is. Below the band a fetch is confetti; above it, one fetch fills the disk
    # with a single video.
    LONG = ids("x", 5)
    SHORT = ids("y", 5)
    BAND = ids("z", 5)
    collector.sources = lambda: [{"url": u, "match": []} for u in ("src-a",)]
    collector.pool = lambda: {"src-a": {
        "ids": LONG + SHORT + BAND,
        "dur": dict([(v, 40000) for v in LONG]
                    + [(v, 30) for v in SHORT]
                    + [(v, 1800) for v in BAND])}}
    calm = collector.pick(5, set())
    check("les subathons de 11 h sont ecartes",
          not any(v[0] == "x" for v in calm), str(calm))
    check("les clips de 30 s aussi", not any(v[0] == "y" for v in calm), str(calm))
    check("il ne reste que la bande utile",
          [v[0] for v in calm] == list("zzzzz"), str(calm))

    # the control: with nothing in the band the source keeps its whole list, so
    # narrowing can never silence a source altogether
    collector.pool = lambda: {"src-a": {
        "ids": LONG + SHORT,
        "dur": dict([(v, 40000) for v in LONG] + [(v, 30) for v in SHORT])}}
    check("temoin: une source sans rien dans la bande sert quand meme",
          len(collector.pick(4, set())) == 4)

    # and the urgent branch is untouched by the band: it has its own rule
    collector.pool = lambda: {"src-a": {
        "ids": LONG + BAND,
        "dur": dict([(v, 40000) for v in LONG] + [(v, 1800) for v in BAND])}}
    check("temoin: en urgence c'est toujours le plus court utile",
          [v[0] for v in collector.pick(2, set(), shortest=True)] == list("zz"))

    collector.sources = lambda: [{"url": u, "match": []}
                                 for u in ("src-a", "src-b", "src-c")]

    print("the runway it reads")
    # prep takes its library from VODLOOP_LIBRARY, which the unit sets and this
    # module's crontab line does not. Unwired, runway_seconds() counts only the
    # chunks already cut: 1500 s against a true 3097 s when this was measured,
    # so every run read as urgent and the unhurried branch was unreachable.
    check("prep is pointed at the same library this module uses",
          collector.prep.LIBRARY == collector.LIBRARY,
          f"{collector.prep.LIBRARY} vs {collector.LIBRARY}")

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
