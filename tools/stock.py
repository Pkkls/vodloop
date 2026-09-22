#!/usr/bin/env python3
"""What the channel has, what it can still get, and what limits it.

Runs on Oracle, where the modules and the disk are:

    scp tools/stock.py ORACLE:/tmp/stock.py
    ssh ORACLE 'CHAN_ROOT=/home/ubuntu/v2/nanatty247 \
        PYTHONPATH=/home/ubuntu/v2/bin python3 /tmp/stock.py'

Written on 2026-09-22 to replace the throwaway version of this script I keep
rewriting. The throwaway one printed "plafond MAXH = 720 h, marge 695.3 h".
MAXH is a picture height, 720 lines, and those two numbers were noise dressed
as a measurement. Every figure below carries the name of the setting it came
from and the unit that setting is in, both read from the code that defines it,
because a number whose unit I supplied from memory is a number I invented.
"""
import pathlib
import shutil
import sys

import chan
import cut
import supply

CH = pathlib.Path(chan.ROOT)

# (cle, unite, ce que le reglage gouverne reellement)
REGLAGES = [
    ("MAXH", "lignes", "hauteur d image acceptee, plafond"),
    ("MINH", "lignes", "hauteur d image acceptee, plancher"),
    ("MIN_SECONDS", "s", "duree minimale d un enregistrement"),
    ("MAX_SECONDS", "s", "duree maximale d un enregistrement"),
    ("PART_SECONDS", "s", "longueur d une part a la decoupe"),
    ("WINDOW_HOURS", "h", "profondeur de file visee"),
    ("BUDGET_GB", "Gio", "empreinte totale autorisee sur le disque"),
    ("FLOOR_GB", "Gio", "libre a garder, jamais entame"),
    ("MAX_FILE_GB", "Gio", "taille maximale d un seul fichier"),
    ("REPEAT_AFTER_DAYS", "j", "delai avant qu un chunk puisse repasser"),
]


def heures(secondes):
    return secondes / 3600.0


def main():
    durations = chan.read_json(chan.STATE / "durations.json", {})
    book, held = cut.ledger(), cut.reserved()
    unite = cut.unit_seconds()

    print("SUR LE DISQUE")
    total = inedit = 0.0
    n = 0
    for dossier in ("current", "queue", "aired"):
        for p in sorted((CH / dossier).glob("*.mkv")):
            n += 1
            total += chan.duration(p, durations)
            inedit += len(cut.unaired(p, book, durations, held)) * unite
    print("  %d enregistrement(s), %.1f h au total" % (n, heures(total)))
    print("  %.1f h jamais diffusees (chunks inedits x %.0f s)"
          % (heures(inedit), unite))
    print("  %d morceau(x) prets a l antenne"
          % len(list((CH / "chunks").glob("*.ts"))))

    print()
    print("ENCORE TELECHARGEABLE")
    cat = (chan.STATE / "catalog.tsv")
    n_cat = len(cat.read_text(errors="ignore").splitlines()) if cat.exists() else 0
    cands = list(supply.candidates())
    print("  catalogue      : %d video(s) connues de la chaine" % n_cat)
    print("  eligibles      : %d" % len(cands))
    if cands:
        print("  soit           : %.1f h" % heures(sum(c[1] for c in cands)))

    print()
    print("CE QUE LE PLAN DEMANDE (state/want.json, ecrit par supply.py)")
    want = chan.read_json(chan.STATE / "want.json", {})
    if want:
        print("  besoin         : %.1f h" % heures(want.get("need_seconds") or 0))
        print("  offre          : %.1f Gio (ce qu un fichier peut peser au plus)"
              % ((want.get("offer_bytes") or 0) / chan.GIB))
        print("  reserve        : %.1f h non encore diffusees"
              % heures(want.get("runway_seconds") or 0))
        print("  debit mesure   : %.2f Mo/s sur ce que la chaine recoit"
              % ((want.get("rate_bps") or 0) / 1e6))
    else:
        print("  (pas encore de plan ecrit)")

    print()
    print("LE DISQUE")
    usage = shutil.disk_usage(CH)
    libre = usage.free / chan.GIB
    print("  total %.1f Gio, occupe %.1f Gio, libre %.1f Gio"
          % (usage.total / chan.GIB, usage.used / chan.GIB, libre))
    print("  plancher %.1f Gio, marge au-dessus %.1f Gio"
          % (chan.FLOOR_BYTES / chan.GIB, libre - chan.FLOOR_BYTES / chan.GIB))
    empreinte = sum(p.stat().st_size for d in ("current", "queue", "aired", "chunks")
                    for p in (CH / d).glob("*") if p.is_file())
    print("  empreinte de la chaine %.1f Gio sur %.1f autorisees"
          % (empreinte / chan.GIB, chan.BUDGET_BYTES / chan.GIB))
    # Lequel des deux mord: relever le budget ne sert a rien tant que c est le
    # plancher qui borne, et l inverse est vrai aussi.
    par_budget = (chan.BUDGET_BYTES - empreinte) / chan.GIB
    par_plancher = libre - chan.FLOOR_BYTES / chan.GIB
    borne = "le budget" if par_budget < par_plancher else "le plancher du disque"
    print("  peut encore prendre %.1f Gio, borne par %s"
          % (min(par_budget, par_plancher), borne))

    print()
    print("LES REGLAGES, AVEC LEUR UNITE")
    for cle, unit, sens in REGLAGES:
        valeur = chan.CONF.get(cle)
        if valeur is None:
            continue
        print("  %-18s %-8s %-6s %s" % (cle, valeur, unit, sens))
    return 0


if __name__ == "__main__":
    sys.exit(main())
