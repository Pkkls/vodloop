#!/usr/bin/env python3
"""Ce qui ne se retelecharge pas, mis a l abri tous les jours.

    python backup.py --apply        # ecrit l archive du jour
    python backup.py                # dit ce qu il ferait
    python backup.py --selftest     # les regles, avec leurs temoins

Les videos ne sont pas ici. Vingt-huit gigaoctets qui existent encore sur
YouTube ne sont pas des donnees, ce sont des copies. Ce qui ne se retrouve nulle
part tient en quelques megaoctets: les registres qui disent ce qui est passe a
l antenne, le catalogue, les durees mesurees, le reglage de la chaine.

Ecrit le 2026-09-26, apres avoir ouvert les sauvegardes existantes. backup.sh
archivait kicknosub-web, un autre projet, dont le dossier n existe plus: il
produisait 45 octets par jour depuis des semaines. Son controle d integrite
lisait l archive pour verifier qu elle s ouvre, et une archive vide s ouvre tres
bien. C est la panne que ce fichier existe pour ne pas refaire, donc le controle
ici porte sur le CONTENU: un nombre de fichiers et une taille plancher, et
l archive du jour est jetee plutot que gardee si elle ne les atteint pas.
"""
import argparse
import os
import pathlib
import subprocess
import sys
import tarfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import chan  # noqa: E402

DEST = pathlib.Path(os.environ.get("VODLOOP_BACKUPS",
                                   str(pathlib.Path.home() / "backups-vodloop")))
GARDE = 14
# Une archive plus maigre que ca n est pas une sauvegarde de cette chaine. Les
# deux bornes ensemble: un seul gros fichier passerait la taille, et cent
# fichiers vides passeraient le compte.
MIN_FICHIERS = 8
MIN_OCTETS = 20 * 1024
# state/ contient aussi des caches volumineux et rejouables; ils ne montent pas.
IGNORE = (".tmp", ".lock", ".pyc")


def a_sauver():
    """Les chemins qui ne se retrouvent nulle part ailleurs."""
    out = []
    for p in sorted((chan.STATE).glob("*")):
        if p.is_file() and not p.name.endswith(IGNORE):
            out.append(p)
    for nom in ("channel.env", "sources.txt", "shorts.txt"):
        p = chan.ROOT / nom
        if p.exists():
            out.append(p)
    return out


def verdict(chemin):
    """(bon, pourquoi). Lit l archive et compte, au lieu de la croire."""
    try:
        with tarfile.open(chemin, "r:gz") as tf:
            membres = [m for m in tf.getmembers() if m.isfile()]
    except (OSError, tarfile.TarError) as exc:
        return False, "illisible: %s" % str(exc)[:60]
    octets = sum(m.size for m in membres)
    if len(membres) < MIN_FICHIERS:
        return False, "%d fichier(s), plancher %d" % (len(membres), MIN_FICHIERS)
    if octets < MIN_OCTETS:
        return False, "%d octets dedans, plancher %d" % (octets, MIN_OCTETS)
    return True, "%d fichiers, %d Ko" % (len(membres), octets // 1024)


def purge():
    vieilles = sorted(DEST.glob("etat-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    for p in vieilles[:-GARDE]:
        p.unlink(missing_ok=True)


def selftest():
    import tempfile
    bac = pathlib.Path(tempfile.mkdtemp(prefix="sauv-"))
    fail = 0

    def check(nom, ok, detail=""):
        nonlocal fail
        if not ok:
            fail += 1
        print("  %s  %-52s %s" % ("PASS" if ok else "FAIL", nom, detail))

    vide = bac / "vide.tar.gz"
    with tarfile.open(vide, "w:gz"):
        pass
    bon, why = verdict(vide)
    check("TEMOIN: une archive vide est refusee", not bon, why)
    check("et c est exactement ce que backup.sh gardait", vide.stat().st_size < 200,
          "%d octets, comme les 45 d hier" % vide.stat().st_size)

    maigre = bac / "maigre.tar.gz"
    with tarfile.open(maigre, "w:gz") as tf:
        for n in range(MIN_FICHIERS + 2):
            f = bac / ("p%d.txt" % n)
            f.write_text("x")
            tf.add(f, arcname=f.name)
    bon, why = verdict(maigre)
    check("TEMOIN: assez de fichiers mais rien dedans, refusee", not bon, why)

    vraie = bac / "vraie.tar.gz"
    with tarfile.open(vraie, "w:gz") as tf:
        for n in range(MIN_FICHIERS + 2):
            f = bac / ("g%d.txt" % n)
            f.write_text("y" * 4096)
            tf.add(f, arcname=f.name)
    bon, why = verdict(vraie)
    check("une archive qui porte vraiment quelque chose passe", bon, why)

    check("TEMOIN: un fichier qui n est pas une archive est refuse",
          not verdict(bac / "p0.txt")[0])
    print()
    print("%d echec(s)" % fail)
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    fichiers = a_sauver()
    print("a sauver: %d fichier(s), %d Ko"
          % (len(fichiers), sum(p.stat().st_size for p in fichiers) // 1024))
    if not args.apply:
        for p in fichiers[:12]:
            print("  %s" % p.relative_to(chan.ROOT))
        return 0

    DEST.mkdir(parents=True, exist_ok=True)
    cible = DEST / ("etat-%s.tar.gz" % time.strftime("%Y%m%d-%H%M%S"))
    with tarfile.open(cible, "w:gz") as tf:
        for p in fichiers:
            tf.add(p, arcname=str(p.relative_to(chan.ROOT)))
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if cron:
            tmp = DEST / "crontab.txt"
            tmp.write_text(cron)
            tf.add(tmp, arcname="crontab.txt")
            tmp.unlink(missing_ok=True)

    bon, pourquoi = verdict(cible)
    if not bon:
        cible.unlink(missing_ok=True)
        chan.log(f"sauvegarde refusee et jetee: {pourquoi}")
        chan.telegram(f"sauvegarde du jour refusee: {pourquoi}. "
                      f"La derniere bonne date d avant.")
        return 1
    purge()
    chan.log(f"sauvegarde {cible.name}: {pourquoi}")
    print("  %s  %s" % (cible, pourquoi))
    return 0


if __name__ == "__main__":
    sys.exit(main())
