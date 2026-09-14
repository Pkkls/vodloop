#!/usr/bin/env python3
"""Sortir les fichiers Kick des que YouTube peut tenir l antenne seul.

Decision de l operateur: plus de Kick sur cette chaine. Mais les retirer avant
que YouTube ne couvre l antenne ferait tomber la chaine sur le clip d attente,
donc la bascule attend un seuil et se fait toute seule, depuis cron, sur la
machine qui reste allumee.

Les fichiers sont DEPLACES, jamais supprimes. Le script retire sa propre ligne
de cron une fois la bascule faite: c est une transition, pas une regle.

Se configure comme le reste de la stack, par VODLOOP_ROOT et VODLOOP_LIBRARY,
pour qu aucun nom de chaine ne vive dans le code.
"""
import os
import pathlib
import re
import subprocess
import sys
import time

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import prep  # noqa: E402

ROOT = pathlib.Path(os.environ.get("VODLOOP_ROOT") or (BIN.parent))
LIB = pathlib.Path(os.environ.get("VODLOOP_LIBRARY") or (ROOT.parent / "videos"))
OUT = ROOT / "ecartes"
LOG = ROOT / "retire-kick.log"
KICK = re.compile(r"-k[0-9a-f]{10}(\.p\d+of\d+)?\.mkv$")
HEURES_MINI, FICHIERS_MINI = 8.0, 5


def note(texte):
    with LOG.open("a") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + texte + "\n")
    print(texte)


def duree(f):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(f)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out) / 3600
    except ValueError:
        return 0.0


kick, autres_h, autres_n = [], 0.0, 0
for f in sorted(LIB.glob("*.mkv")):
    if KICK.search(f.name):
        kick.append(f)
    elif prep.remux_verdict(f):
        autres_h += duree(f)
        autres_n += 1

if not kick:
    note("plus aucun fichier Kick, la bascule est faite")
    subprocess.run(["bash", "-c",
                    "crontab -l | grep -v retire-kick.py | crontab -"])
    raise SystemExit(0)

if autres_h < HEURES_MINI or autres_n < FICHIERS_MINI:
    note("pas encore: %.1f h de YouTube jouable sur %d fichiers "
         "(il en faut %.0f h sur %d), %d Kick gardes"
         % (autres_h, autres_n, HEURES_MINI, FICHIERS_MINI, len(kick)))
    raise SystemExit(0)

OUT.mkdir(parents=True, exist_ok=True)
for f in kick:
    f.rename(OUT / f.name)
note("bascule faite: %d fichiers Kick ecartes, %.1f h de YouTube prennent le relais"
     % (len(kick), autres_h))
subprocess.run(["bash", "-c", "crontab -l | grep -v retire-kick.py | crontab -"])
note("ligne de cron retiree, ce script ne tournera plus")
