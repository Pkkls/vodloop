#!/usr/bin/env python3
"""Taking a Kick VOD apart. Run: python3 tests/test_kickfetch.py

Nothing here talks to Kick. What is checked is the arithmetic between an API
listing and a file the rest of the tree can read: which rung is pulled, which
segments belong to which part, and whether the name that comes out is one the
janitor, prep and the chat bot parse the way this module meant them to.

The part that would be expensive to get wrong is the tiling. A gap is video
nobody ever fetches and an overlap is video fetched twice, and neither shows
up anywhere until a viewer notices the same hour twice in one evening.
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import collector  # noqa: E402
import common  # noqa: E402
import kickfetch  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=7624377,RESOLUTION=1920x1080,FRAME-RATE=30.000
1080p/playlist.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3400000,RESOLUTION=1280x720,FRAME-RATE=50.000
720p50/playlist.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2439511,RESOLUTION=1280x720,FRAME-RATE=30.000
720p30/playlist.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1494511,RESOLUTION=852x480,FRAME-RATE=30.000
480p30/playlist.m3u8
"""

# ten seconds each, the shape Kick serves, with a short last one
PLAYLIST = ("#EXTM3U\n#EXT-X-TARGETDURATION:13\n"
            + "".join(f"#EXTINF:10.000,\n{n}.ts\n" for n in range(50))
            + "#EXTINF:4.101,\n50.ts\n#EXT-X-ENDLIST\n")

BASE = "https://stream.kick.com/abc/ivs/v1/1/2/2026/9/12/6/35/X/media/hls"

LISTING = [
    {"is_live": False, "duration": 21600000, "source": BASE + "/master.m3u8",
     "session_title": "Thailand Day 3 w/ @someone", "start_time": "2026-09-12 06:36:02",
     "video": {"uuid": "147031b5-52cf-47ec-95a4-dfb370eb44f5"}},
    {"is_live": True, "duration": 3600000, "source": BASE + "/master.m3u8",
     "session_title": "on air right now", "start_time": "2026-09-13 10:00:00",
     "video": {"uuid": "22222222-2222-2222-2222-222222222222"}},
    {"is_live": False, "duration": 90000, "source": BASE + "/master.m3u8",
     "session_title": "a ninety second clip", "start_time": "2026-09-11 10:00:00",
     "video": {"uuid": "33333333-3333-3333-3333-333333333333"}},
    {"is_live": False, "duration": 0, "source": BASE + "/master.m3u8",
     "session_title": "nothing behind it yet", "start_time": "2026-09-10 10:00:00",
     "video": {"uuid": "44444444-4444-4444-4444-444444444444"}},
]

real_get = kickfetch.get


def served(url, timeout=120):
    if url.endswith("/videos"):
        return json.dumps(LISTING).encode()
    if url.endswith("master.m3u8"):
        return MASTER.encode()
    if url.endswith("playlist.m3u8"):
        return PLAYLIST.encode()
    return None


try:
    kickfetch.get = served

    print("the key a Kick uuid is filed under")
    key = kickfetch.key_of("147031b5-52cf-47ec-95a4-dfb370eb44f5")
    check("eleven characters, like every id this tree reads", len(key) == 11, key)
    check("and it is read back by the pattern the others use",
          collector.VIDEO_ID.search(f"Some title-{key}.mkv").group(1) == key)
    check("the same uuid always gives the same key",
          key == kickfetch.key_of("147031b5-52cf-47ec-95a4-dfb370eb44f5"))
    check("two uuids do not share one key",
          key != kickfetch.key_of("147031b5-52cf-47ec-95a4-dfb370eb44f6"))

    print("what is worth listing")
    vids = kickfetch.videos("whoever")
    check("a stream still on air is left alone", len(vids) == 1, str(len(vids)))
    check("and it is the finished one that is kept",
          vids[0]["key"] == key and vids[0]["secs"] == 21600, str(vids[0]))
    check("the API's milliseconds become seconds", vids[0]["secs"] == 21600)
    check("the date is the day, not the timestamp", vids[0]["date"] == "2026-09-12")
    # the control: a listing of nothing but that same VOD, with is_live cleared
    # on the others, has to give four. Otherwise the count above could be the
    # parser dropping rows for a reason of its own.
    real_min, kickfetch.MIN_SECONDS = kickfetch.MIN_SECONDS, 1
    try:
        loose = [dict(item, is_live=False) for item in LISTING]
        kickfetch.get = lambda url, timeout=120: (
            json.dumps(loose).encode() if url.endswith("/videos") else served(url))
        check("temoin: sans le direct ni le plancher, trois des quatre passent",
              len(kickfetch.videos("whoever")) == 3
              and sum(1 for i in loose if i["duration"] == 0) == 1,
              "la quatrieme a une duree nulle, rien ne peut la faire passer")
    finally:
        kickfetch.MIN_SECONDS = real_min
        kickfetch.get = served

    print("which rung is pulled")
    real_h, real_ch = kickfetch.MAX_HEIGHT, collector.MAX_HEIGHT
    try:
        kickfetch.MAX_HEIGHT = 720
        rung = kickfetch.rendition(BASE)
        check("la plus haute dans la limite de la chaine",
              rung[0] == BASE + "/720p30/playlist.m3u8" and rung[1] == 720, str(rung))
        check("un palier a 50 fps est refuse, Kick n'en a pas",
              "720p50" not in rung[0], str(rung))
        check("son poids sort de la playlist, pas d'une estimation",
              rung[2] == 2439511 / 8, str(rung[2]))
        # the control: raise the ceiling and the same call takes 1080p, so the
        # check above measures the ceiling and not the order of the playlist
        kickfetch.MAX_HEIGHT = 1080
        check("temoin: plafond releve, c'est le 1080p qui sort",
              kickfetch.rendition(BASE)[1] == 1080)
        kickfetch.MAX_HEIGHT = 300
        check("aucun palier assez petit ne rend rien",
              kickfetch.rendition(BASE) is None)

        # Kick sert son 1080p a 7.62 Mbps contre 2.44 pour le 720p, trois fois
        # le disque, la ou le 1080p YouTube de la meme matiere mesure 1.16 Go/h
        # contre 0.66. Un seul plafond pour les deux paie la prime de Kick ou
        # jette l'image bon marche de YouTube.
        kickfetch.MAX_HEIGHT, collector.MAX_HEIGHT = 720, 1080
        check("le plafond de Kick ne suit pas celui de YouTube",
              kickfetch.rendition(BASE)[1] == 720, str(kickfetch.rendition(BASE)))
        # le temoin: c'est bien la demande a YouTube qui porte 1080
        check("temoin: et la ligne pour la carte demande toujours 1080",
              collector.line_for("abcDEF12345", {}).endswith(" h=1080"),
              collector.line_for("abcDEF12345", {}))
    finally:
        kickfetch.MAX_HEIGHT, collector.MAX_HEIGHT = real_h, real_ch

    print("cutting the stream into parts")
    segs = kickfetch.segments(BASE + "/720p30/playlist.m3u8")
    check("la playlist rend ses segments dans l'ordre",
          len(segs) == 51 and segs[0][0].endswith("/720p30/0.ts"), str(segs[:1]))
    check("chaque segment porte sa propre duree",
          segs[0][1] == 10.0 and segs[-1][1] == 4.101, str(segs[-1]))

    total = sum(secs for _, secs in segs)
    real_part = collector.PART_SECONDS
    try:
        collector.PART_SECONDS = 200
        parts = collector.parts_of(key, int(total))
        taken = [kickfetch.window(segs, span) for _, span in parts]
        seen = [link for links, _ in taken for link in links]
        check("les morceaux couvrent tout le flux",
              len(seen) == len(segs), f"{len(seen)} segments sur {len(segs)}")
        check("et aucun segment n'est pris deux fois",
              len(set(seen)) == len(seen), f"{len(seen) - len(set(seen))} doublons")
        check("la duree rendue est celle des segments, pas celle demandee",
              abs(sum(secs for _, secs in taken) - total) < 0.001,
              str(sum(secs for _, secs in taken)))
        # the control: a window nobody asked for has to come back empty, so the
        # checks above are not passing on a function that returns everything
        check("temoin: une tranche hors du flux ne rend rien",
              kickfetch.window(segs, (9000, 9600)) == ([], 0.0))
    finally:
        collector.PART_SECONDS = real_part

    print("the name that comes out")
    video = vids[0]
    name = kickfetch.filename(video, f"{key}.p02of06")
    check("la date mene, la cle traine, le morceau suit",
          name == f"2026-09-12_Thailand Day 3 w someone-{key}.p02of06.mkv", name)
    check("le collector relit exactement la cle demandee",
          collector.key_of(pathlib.Path(name)) == f"{key}.p02of06")
    check("et le chat lit un titre, pas un nom de fichier",
          common.pretty_title(pathlib.Path(name).stem)
          == "2026-09-12 Thailand Day 3 w someone p02of06",
          common.pretty_title(pathlib.Path(name).stem))
    # a title that is nothing but emoji still has to produce a usable name
    check("un titre illisible ne fabrique pas un nom vide",
          kickfetch.filename({"title": "\U0001f1f9\U0001f1ed", "date": "2026-09-12"},
                             key) == f"2026-09-12_kick-{key}.mkv",
          kickfetch.filename({"title": "\U0001f1f9\U0001f1ed", "date": "2026-09-12"}, key))
    check("un titre sans morceau n'invente pas de suffixe",
          kickfetch.filename(video, key).endswith(f"-{key}.mkv"))

    print("what to take next")
    second = dict(video, key="k00000000aa", secs=21600)
    try:
        collector.PART_SECONDS = 7200
        order = kickfetch.pending([video, second], set())
        check("le premier morceau de chaque VOD avant le deuxieme de la premiere",
              [item[2] for item in order[:2]]
              == [f"{video['key']}.p01of03", "k00000000aa.p01of03"],
              str([item[2] for item in order[:3]]))
        check("ce qui est deja tenu ne revient pas",
              all(item[2] != f"{video['key']}.p01of03"
                  for item in kickfetch.pending([video], {f"{video['key']}.p01of03"})))
        # the control: without a part length a VOD is one entry, not three
        collector.PART_SECONDS = 0
        check("temoin: sans decoupage, une VOD est une seule entree",
              [item[2] for item in kickfetch.pending([video], set())] == [video["key"]])
    finally:
        collector.PART_SECONDS = real_part

    print("what Kick already weighs against its cap")
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / ".arrivee").mkdir()
    (tmp / f"2026-01-01_A-{key}.p01of02.mkv").write_bytes(b"x" * 100)
    (tmp / "Some video-abcDEF12345.mp4").write_bytes(b"y" * 200)
    (tmp / ".arrivee" / f"k00000000aa.p01of02{kickfetch.WORKING_SUFFIX}").write_bytes(b"z" * 50)
    real_lib = kickfetch.LIBRARY
    try:
        kickfetch.LIBRARY = tmp
        check("ses fichiers et ce qui arrive encore sont comptes",
              kickfetch.kick_bytes() == 150, str(kickfetch.kick_bytes()))
        # the control: the YouTube file is right there and must not be counted,
        # or the cap would be measuring the library instead of Kick's share
        check("temoin: le fichier YouTube a cote ne compte pas",
              kickfetch.kick_bytes() != 350)
    finally:
        kickfetch.LIBRARY = real_lib
finally:
    kickfetch.get = real_get

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
