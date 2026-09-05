#!/usr/bin/env python3
"""Chunks eaten mid-encode. Run: python3 tests/test_prep_consumed.py

The feeder deletes each chunk as soon as it has played it, so on a dry queue a
whole video can be gone from disk before ffmpeg returns. Judging the encode by
what is left on disk then reports a perfectly good video as a failure and exiles
the file to incoming/failed/, which is how the channel ends up on the standby
clip with nothing left to play.
"""
import os
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(tempfile.mkdtemp(prefix="prep-consumed-"))
for name in ("segments", "incoming", "state"):
    (root / name).mkdir()
os.environ["VODLOOP_ROOT"] = str(root)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import common  # noqa: E402
import prep  # noqa: E402

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


class FakeEncoder:
    """ffmpeg as far as prepare() can tell: writes the segment list the muxer
    would write, then lets the feeder take the chunk before returning."""

    segments = 1
    consume = True
    last_args = []

    def __init__(self, args, **kwargs):
        # tolerated missing on purpose, so this test can be pointed at a version
        # that does not ask the muxer for a list and be seen to fail
        i = args.index("-segment_list") if "-segment_list" in args else -1
        listing = pathlib.Path(args[i + 1]) if i > 0 else None
        names = []
        for n in range(self.segments):
            chunk = pathlib.Path(args[-1] % n)
            chunk.write_text("chunk")
            names.append(chunk.name)
            if self.consume:
                chunk.unlink()  # the feeder played it and moved on
        if listing is not None:
            listing.write_text("".join(f"{n}\n" for n in names))
        FakeEncoder.last_args = list(args)
        self.returncode = 0

    def communicate(self, timeout=None):
        return (b"", b"")


def run(item):
    # This is about how a finished encode is judged, so the source is declared
    # not already in target shape. Without it the probe inside matches_target
    # goes through subprocess.run, which reaches the same patched Popen.
    real, prep.subprocess.Popen = prep.subprocess.Popen, FakeEncoder
    real_match, prep.matches_target = prep.matches_target, lambda path: False
    try:
        return prep.prepare(item)
    finally:
        prep.subprocess.Popen = real
        prep.matches_target = real_match


item = {"id": 8, "path": str(common.INCOMING / "v.mp4"), "url": "v.mp4",
        "title": "V", "by_name": "", "duration": 128}
ok = run(item)
check("un encode dont les chunks sont deja joues reste un succes", ok, item.get("error"))
check("le nombre de chunks vient du muxeur", item.get("chunks") == 1, item.get("chunks"))
check("rien ne reste dans incoming/failed", not (common.INCOMING / "failed").exists())

FakeEncoder.segments = 3
item3 = dict(item, id=9)
run(item3)
check("plusieurs chunks sont comptes", item3.get("chunks") == 3, item3.get("chunks"))

# the control: a real failure still has to be one
FakeEncoder.segments = 0
item0 = dict(item, id=10)
check("temoin: zero chunk produit reste un echec", not run(item0))
check("temoin: l'echec est explique", bool(item0.get("error")), item0)

# the list file is scratch, it must not pile up in state/
check("aucune liste laissee derriere",
      not list(common.STATE.glob("list_*.txt")),
      list(common.STATE.glob("list_*.txt")))

# --- a file in the wrong shape is fixed once, not re-encoded forever -------
calls = []
real_norm, prep.normalise_in_place = prep.normalise_in_place, lambda p, title=None: calls.append(p)
real_match, prep.matches_target = prep.matches_target, lambda p: False
real_popen, prep.subprocess.Popen = prep.subprocess.Popen, FakeEncoder
FakeEncoder.segments = 1
try:
    prep.prepare({"id": 20, "path": str(common.INCOMING / "depose.mp4"), "url": "d",
                  "title": "D", "by_name": "", "duration": 60})
    check("un fichier depose n'est pas normalise, il ne sert qu'une fois", calls == [], calls)

    library = common.ROOT / "videos"
    library.mkdir(exist_ok=True)
    # with a thin backlog the file is encoded the slow way instead: normalising
    # feeds the channel nothing while it runs, so it must not start here
    for chunk in common.SEGMENTS.glob("*.ts"):
        chunk.unlink()
    prep.prepare({"id": 21, "path": str(library / "vieux.mp4"), "url": "v",
                  "title": "V", "by_name": "", "duration": 60})
    check("reserve mince: on n'entame pas une normalisation", calls == [], calls)

    # with enough backlog to cover it, it goes ahead
    needed = prep.NORMALISE_ABOVE_SECONDS // common.CHUNK_SECONDS + 1
    for n in range(needed):
        (common.SEGMENTS / f"00099_{n:05d}.ts").write_text("chunk")
    prep.prepare({"id": 22, "path": str(library / "vieux.mp4"), "url": "v",
                  "title": "V", "by_name": "", "duration": 60})
    check("reserve confortable: la normalisation demarre", len(calls) == 1, calls)

    # Un fichier conforme est remuxe, et un remux ne peut rien dessiner. Il a
    # donc droit a une passe, une seule, pour graver son titre. Sans la seconde
    # verification ci-dessous cette exception se rejouerait a chaque tour et
    # rencoderait la bibliotheque en boucle.
    prep.matches_target = lambda p: True
    prep.prepare({"id": 23, "path": str(library / "bon.mp4"), "url": "b",
                  "title": "B", "by_name": "", "duration": 60})
    check("un fichier conforme sans titre grave recoit une passe",
          len(calls) == 2, calls)

    prep.mark_captioned("bon.mp4")
    prep.prepare({"id": 24, "path": str(library / "bon.mp4"), "url": "b",
                  "title": "B", "by_name": "", "duration": 60})
    check("une fois grave, il n'est plus jamais retouche", len(calls) == 2, calls)
    check("et il est remuxe, pas reencode", "copy" in FakeEncoder.last_args,
          FakeEncoder.last_args[:6])
finally:
    prep.normalise_in_place = real_norm
    prep.matches_target = real_match
    prep.subprocess.Popen = real_popen

print()
print(f"{passed}/{passed + failed} passent")
sys.exit(0 if failed == 0 else 1)
