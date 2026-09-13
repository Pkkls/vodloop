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
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import time

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

    print("one board, one inbox per channel")
    # A single total would have each channel read its neighbour's backlog as
    # its own: the busy one would hold the quiet one at want<=0 for ever while
    # the quiet one's inbox sat empty.
    two = {"queues": {"videos": 3, "videos-second": 6}, "at": 1000}
    real_load, collector.load = collector.load, lambda *_a, **_k: two
    real_lib, real_status = collector.LIBRARY, collector.STATUS_FILE
    try:
        collector.LIBRARY = pathlib.Path("/home/ubuntu/videos-second")
        check("il lit la file de SA bibliotheque",
              collector.board_queue(now=1060) == 6)
        # the control: the same report, the same call, the other channel. If
        # both came back 6 this would be measuring the fixture.
        collector.LIBRARY = pathlib.Path("/home/ubuntu/videos")
        check("temoin: l'autre chaine y lit la sienne, pas la meme",
              collector.board_queue(now=1060) == 3)
        collector.LIBRARY = pathlib.Path("/home/ubuntu/videos-absente")
        check("une bibliotheque absente du rapport n'attend rien",
              collector.board_queue(now=1060) == 0)
        # and a board that only knows how to publish a total is still read
        collector.load = lambda *_a, **_k: {"queue": 4, "at": 1000}
        check("temoin: un rapport sans detail reste lu comme avant",
              collector.board_queue(now=1060) == 4)
    finally:
        collector.load, collector.LIBRARY = real_load, real_lib
        collector.STATUS_FILE = real_status

    print("cutting a stream the board's card cannot hold whole")
    LONG = "L0000000000"
    collector.sources = lambda: [{"url": "src-a", "match": []}]
    collector.pool = lambda: {"src-a": {"ids": [LONG], "dur": {LONG: 21600}}}
    real_part, real_h = collector.PART_SECONDS, collector.MAX_HEIGHT
    try:
        collector.PART_SECONDS, collector.MAX_HEIGHT = 7200, 720
        lists, _, spans = collector.catalogue()
        check("un flux de 6 h devient trois morceaux",
              lists[0] == [f"{LONG}.p01of03", f"{LONG}.p02of03", f"{LONG}.p03of03"],
              str(lists[0]))
        # a gap is video nobody ever fetches, an overlap is video fetched twice
        check("les morceaux se suivent sans trou ni recouvrement",
              [spans[k] for k in lists[0]] == [(0, 7200), (7200, 14400), (14400, 21600)],
              str([spans[k] for k in lists[0]]))
        got = collector.pick(2, {f"{LONG}.p01of03"})
        check("un morceau deja tenu n'est pas repropose, les autres si",
              got == [f"{LONG}.p02of03", f"{LONG}.p03of03"], str(got))
        line = collector.line_for(f"{LONG}.p02of03", spans)
        check("la ligne porte la place et la hauteur",
              line == f"https://www.youtube.com/watch?v={LONG} "
                      "part=p02of03 range=7200-14400 h=720", line)
    finally:
        collector.PART_SECONDS, collector.MAX_HEIGHT = real_part, real_h

    # the control: unset, the same video stays whole and its line is the bare
    # URL cx247's board has always been handed
    lists, _, spans = collector.catalogue()
    check("temoin: sans decoupage la video reste entiere", lists[0] == [LONG],
          str(lists[0]))
    check("temoin: et sa ligne est l'URL nue",
          collector.line_for(LONG, spans) == f"https://www.youtube.com/watch?v={LONG}")

    print("what the inbox already holds is read back")
    tmp = pathlib.Path(tempfile.mkdtemp())
    inbox = tmp / "inbox.txt"
    inbox.write_text(f"https://www.youtube.com/watch?v={LONG} "
                     "part=p01of03 range=0-7200 h=720\n")
    real_inbox, collector.INBOX = collector.INBOX, inbox
    try:
        # without this a part in flight is invisible and gets queued again
        check("une ligne de morceau se relit comme ce morceau",
              f"{LONG}.p01of03" in collector.inbox_ids(), str(collector.inbox_ids()))
        inbox.write_text(f"https://www.youtube.com/watch?v={LONG}\n")
        check("temoin: une URL nue ne fabrique pas de morceau",
              collector.inbox_ids() == {LONG}, str(collector.inbox_ids()))
    finally:
        collector.INBOX = real_inbox

    print("the share of the disk, with two channels on one server")
    # Free space stops being a channel's own measure the moment a second one
    # writes to the same filesystem: the neighbour's arrivals would have this
    # one refuse for ever, and its own arrivals would have the neighbour do
    # the same. What bounds a channel is its share.
    # wide enough that the ledger below can hold sixteen entries and still
    # leave more candidates than a run can take: otherwise a short queue would
    # be explained by an empty pool rather than by the share
    POOL = ids("s", 40)
    collector.sources = lambda: [{"url": "src-a", "match": []}]
    collector.pool = lambda: {"src-a": {"ids": POOL, "dur": {v: 1800 for v in POOL}}}
    lib = tmp / "videos-second"
    lib.mkdir()
    GB = 1024 ** 3
    saved = (collector.LIBRARY, collector.INBOX, collector.HANDED_FILE,
             collector.UNUSABLE_FILE, collector.STATUS_FILE, collector.shutil,
             collector.common.BUDGET_BYTES, collector.common.bytes_used,
             collector.prep.runway_seconds)

    def run(free_gb, used_gb, handed=None, budget_gb=10.0):
        """One --apply-less pass, with the disk and the ledger dictated."""
        collector.HANDED_FILE.write_text(json.dumps(handed or {}))
        collector.common.BUDGET_BYTES = int(budget_gb * GB)
        collector.common.bytes_used = lambda *_a: int(used_gb * GB)
        collector.shutil = type("S", (), {"disk_usage": staticmethod(
            lambda _p: type("U", (), {"free": int(free_gb * GB)})())})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            collector.main([])
        return out.getvalue()

    try:
        collector.LIBRARY = lib
        collector.INBOX = tmp / "inbox-absent.txt"
        collector.HANDED_FILE = tmp / "handed.json"
        collector.UNUSABLE_FILE = tmp / "absent.json"
        collector.STATUS_FILE = tmp / "absent.json"
        collector.prep.runway_seconds = lambda: 12 * 3600
        # an empty library measures nothing, so DEFAULT_RATE applies: 250 ko/s,
        # about 450 Mo for the 30 min videos above
        check("la part pleine arrete tout", "rien ajoute" in run(20, 9.9),
              run(20, 9.9).strip().splitlines()[-1])
        # the control: the same disk, the same pool, a share with room in it
        check("temoin: la meme passe avec de la place sert la carte",
              run(20, 1.0).count("ajouterait") == collector.MAX_PER_RUN,
              str(run(20, 1.0).count("ajouterait")))
        # free space still counts, but only as the floor the janitor frees to,
        # which is the one line both channels leave alone
        check("un disque au plancher arrete tout malgre la part",
              "rien ajoute" in run(5.2, 1.0), run(5.2, 1.0).strip().splitlines()[-1])

        now = time.time()
        flying = {v: now for v in POOL[:16]}
        busy = run(20, 1.0, flying).count("ajouterait")
        check("ce qui est en vol est deja depense sur la part",
              busy < collector.MAX_PER_RUN, f"{busy} lignes au lieu de 8")

        # En vol et ne-pas-reproposer sont deux questions. Avec un refetch de 21
        # jours, une cle remise il y a huit heures ne vole plus: elle a atterri
        # ou elle a echoue. Mesure 2026-09-14: huit cles retirees de la file de
        # la carte, qui n'arriveraient donc jamais, etaient encore facturees a
        # la part trois semaines plus tard.
        # La carte dit combien elle tient, et c'est le seul compte honnete de ce
        # qui arrive encore. Mesure 2026-09-14: onze cles dans la fenetre de six
        # heures contre une carte qui en annoncait trois, donc dix-neuf Go
        # reserves sur une part de dix-huit, et la chaine refusait chaque passe
        # avec trois fichiers en bibliotheque.
        real_bq = collector.board_queue
        try:
            collector.board_queue = lambda now=None: 2
            capped = run(20, 1.0, {v: now for v in POOL[:16]}).count("ajouterait")
            collector.board_queue = lambda now=None: None
            uncapped = run(20, 1.0, {v: now for v in POOL[:16]}).count("ajouterait")
            check("seules les cles que la carte tient encore sont facturees",
                  capped > uncapped, f"{capped} lignes avec plafond, {uncapped} sans")
        finally:
            collector.board_queue = real_bq

        real_refetch = collector.REFETCH_SECONDS
        try:
            collector.REFETCH_SECONDS = 21 * 24 * 3600
            landed = {v: now - 8 * 3600 for v in POOL[:16]}
            freed = run(20, 1.0, landed).count("ajouterait")
            check("une cle remise il y a huit heures ne pese plus sur la part",
                  freed == collector.MAX_PER_RUN, f"{freed} lignes")
            # le temoin: les memes cles, remises a l'instant, pesent encore
            still = run(20, 1.0, {v: now for v in POOL[:16]}).count("ajouterait")
            check("temoin: les memes, remises a l'instant, pesent toujours",
                  still < collector.MAX_PER_RUN, f"{still} lignes")
        finally:
            collector.REFETCH_SECONDS = real_refetch
        # the control: the same ledger, the same count of entries, old enough
        # that nothing is in flight any more
        stale = {v: now - 8 * 24 * 3600 for v in POOL[:16]}
        check("temoin: les memes entrees, mais perimees, ne coutent rien",
              run(20, 1.0, stale).count("ajouterait") == collector.MAX_PER_RUN,
              str(run(20, 1.0, stale).count("ajouterait")))

        # and with no share at all the old free-space rule is what decides
        check("temoin: sans part, c'est le plancher de 8 Go qui tranche",
              "rien ajoute" in run(7, 0, budget_gb=0)
              and run(20, 0, budget_gb=0).count("ajouterait") == collector.MAX_PER_RUN)
    finally:
        (collector.LIBRARY, collector.INBOX, collector.HANDED_FILE,
         collector.UNUSABLE_FILE, collector.STATUS_FILE, collector.shutil,
         collector.common.BUDGET_BYTES, collector.common.bytes_used,
         collector.prep.runway_seconds) = saved

    print("a channel that only wants whole hours")
    # Measured 2026-09-13 on the second channel, left with the defaults: the two
    # videos the collector chose for it were 38 and 21 minutes. Both rules were
    # working as written. The urgent path asks for the SHORTEST candidate above
    # the twenty minute floor, and the variety band caps at sixty minutes, so a
    # channel built on whole days got exactly the opposite of what it is for.
    LONG = ids("h", 4)     # deux heures
    MID = ids("m", 4)      # quarante minutes
    TINY = ids("t", 4)     # six minutes
    collector.sources = lambda: [{"url": "src-a", "match": []}]
    collector.pool = lambda: {"src-a": {
        "ids": LONG + MID + TINY,
        "dur": dict([(v, 7200) for v in LONG] + [(v, 2400) for v in MID]
                    + [(v, 360) for v in TINY])}}
    real_min, real_rand = collector.MIN_SECONDS, collector.RANDOM_PICK
    try:
        collector.MIN_SECONDS = 3600
        calm = collector.pick(6, set())
        check("rien sous l'heure n'est propose",
              all(v[0] == "h" for v in calm), str(calm))
        check("et il en propose autant qu'il en existe",
              len(calm) == len(LONG), str(len(calm)))
        # en urgence le chemin est un autre, et il doit tenir le meme plancher
        urgent = collector.pick(4, set(), shortest=True)
        check("meme en urgence, rien sous l'heure",
              all(v[0] == "h" for v in urgent), str(urgent))

        # the control: same pool, same calls, floor removed. If these also came
        # back all-h the checks above would be measuring the fixture.
        collector.MIN_SECONDS = 0
        loose = collector.pick(6, set())
        check("temoin: sans plancher, les courtes reviennent",
              any(v[0] in "mt" for v in loose), str(loose))
        check("temoin: et en urgence c'est la plus courte utile qui gagne",
              collector.pick(1, set(), shortest=True)[0][0] == "m",
              str(collector.pick(1, set(), shortest=True)))

        # un plancher pose expres est un contrat: mieux vaut ne rien proposer
        # que de servir quarante minutes en esperant que personne ne regarde
        collector.pool = lambda: {"src-a": {
            "ids": MID + TINY,
            "dur": dict([(v, 2400) for v in MID] + [(v, 360) for v in TINY])}}
        collector.MIN_SECONDS = 3600
        check("un pool entierement sous le plancher ne rend rien",
              collector.pick(4, set(), shortest=True) == []
              and collector.pick(4, set()) == [])
        # the control: the old floor still falls back, because nobody asked for it
        collector.MIN_SECONDS = 0
        check("temoin: sans plancher explicite, le repli d'avant sert encore",
              len(collector.pick(2, set(), shortest=True)) == 2)

        print("and it wants them drawn, not read in order")
        BIG = ids("b", 40)
        collector.pool = lambda: {"src-a": {
            "ids": BIG, "dur": {v: 7200 for v in BIG}}}
        collector.MIN_SECONDS = 3600
        collector.RANDOM_PICK = False
        first = collector.pick(5, set())
        check("sans tirage, c'est toujours le haut de la liste",
              first == BIG[:5] and collector.pick(5, set()) == first, str(first))
        collector.RANDOM_PICK = True
        draws = [tuple(collector.pick(5, set())) for _ in range(6)]
        check("avec tirage, deux passes ne donnent pas la meme chose",
              len(set(draws)) > 1, f"{len(set(draws))} ordres differents sur 6")
        check("et il pioche ailleurs que dans les cinq premiers",
              any(k not in BIG[:5] for d in draws for k in d))
        # the draw must not cost a source its turn: two sources, both served
        collector.sources = lambda: [{"url": u, "match": []} for u in ("src-a", "src-b")]
        OTHER = ids("o", 40)
        collector.pool = lambda: {
            "src-a": {"ids": BIG, "dur": {v: 7200 for v in BIG}},
            "src-b": {"ids": OTHER, "dur": {v: 7200 for v in OTHER}}}
        mixed = collector.pick(6, set())
        check("le tirage ne prive aucune source de son tour",
              {v[0] for v in mixed} == {"b", "o"}, str(mixed))
    finally:
        collector.MIN_SECONDS, collector.RANDOM_PICK = real_min, real_rand
finally:
    collector.sources = real_sources
    collector.pool = real_pool

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
