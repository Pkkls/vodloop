#!/usr/bin/env python3
"""Ramener ce que la chaine ne peut pas se refabriquer, et rien d autre.

    python tools/statesave.py            dire ce qui serait ramene, sans rien ecrire
    python tools/statesave.py --pull     le ramener dans backups/<chaine>/<date>/

Mesure du 2026-09-22 sur Oracle: 124 ko d etat irremplacable (les registres de
ce qui a ete diffuse, le catalogue, les candidats, l etat du bot) contre 20.6 Go
de medias, qui se re-telechargent. Et la sauvegarde quotidienne de la machine
produit des archives de 45 octets depuis le 09/09: elle archive un dossier qui
n existe plus, meurt avant son propre controle, et son journal est fige sur un
dernier "ok" rassurant.

Deux fichiers ne sont PAS ramenes et ne le seront pas par cet outil:
channel.env porte la cle de stream et kick_tokens.json porte le jeton du chat.
Les sortir de la machine est une decision de kil, pas un effet de bord d un
script de sauvegarde. L outil les nomme et s arrete la.
"""
import argparse
import base64
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from drift import hosts, ssh_argv  # noqa: E402

CHAINE = "nanatty247"
LOIN = "/home/ubuntu/v2/%s" % CHAINE
# ce qui ne se refabrique pas: la memoire de ce qui a ete diffuse, et de ce
# qui peut encore l etre
GARDES = ("state/aired.tsv", "state/units.tsv", "state/hours.tsv",
          "state/chunkmap.json", "state/durations.json", "state/catalog.tsv",
          "state/candidates.tsv", "state/rejected.tsv", "state/failed.tsv",
          "state/kick.tsv", "state/bot.json", "state/parts.json",
          "state/watch.json", "sources.txt")
SECRETS = ("channel.env", "state/kick_tokens.json")


def distant(conf, script):
    """Un script sur Oracle, en base64: cinq couches de guillemets entre ici et
    la-bas, et chacune en mange une."""
    charge = "echo %s | base64 -d | sh" % base64.b64encode(
        script.encode("utf-8")).decode("ascii")
    fait = subprocess.run(ssh_argv("oracle", conf, charge), capture_output=True,
                          timeout=300, env=dict(os.environ, MSYS_NO_PATHCONV="1"))
    if fait.returncode:
        sys.exit("Oracle injoignable: %s"
                 % fait.stderr.decode(errors="replace")[:160])
    return fait.stdout.decode(errors="replace")


def inventaire(conf, fige=None):
    """[(nom, octets)] de ce qui existe la-bas, parmi ce qu on garde.

    Avec fige, on regarde une copie arretee plutot que les fichiers vivants.
    Sans, units.tsv grossit et chunkmap.json se reecrit entre le moment ou on
    lit leur taille et celui ou on les copie: mesure le 2026-09-22, +25 et
    -123 octets, et le controle de taille criait a l echec sur un systeme qui
    faisait simplement son travail.
    """
    base = fige or LOIN
    script = "cd %s || exit 1\n" % base
    for nom in GARDES:
        script += "[ -f '%s' ] && echo \"%s $(wc -c < '%s')\"\n" % (nom, nom, nom)
    lignes = [l.split() for l in distant(conf, script).splitlines() if l.strip()]
    return [(bouts[0], int(bouts[1])) for bouts in lignes if len(bouts) == 2]


def figer(conf):
    """Une copie arretee la-bas, dont les tailles ne bougent plus. Rend son
    chemin. Les fichiers vivent sous state/, l arborescence est recreee."""
    fige = "/tmp/vodloop-statesave"
    script = "rm -rf %s && mkdir -p %s/state && cd %s || exit 1\n" % (fige, fige, LOIN)
    for nom in GARDES:
        script += "[ -f '%s' ] && cp -p '%s' '%s/%s'\n" % (nom, nom, fige, nom)
    script += "echo fige\n"
    if "fige" not in distant(conf, script):
        sys.exit("la copie arretee n a pas pu etre faite")
    return fige


def ramener(conf, dossier, noms, base):
    """Chaque fichier arrive par scp depuis la copie arretee, et sa taille est
    comparee a celle qu elle avait la-bas. Rend le nombre de fichiers exacts."""
    bons = 0
    for nom, taille in noms:
        cible = dossier / nom.replace("/", "-")
        argv = ["scp", "-i", os.path.expanduser(conf["ORACLE_KEY"]),
                "-o", "BatchMode=yes", "-q",
                "%s:%s/%s" % (conf["ORACLE"], base, nom), str(cible)]
        if subprocess.run(argv, capture_output=True, timeout=300).returncode:
            print("  %-26s envoi echoue" % nom)
            continue
        vu = cible.stat().st_size if cible.exists() else -1
        if vu != taille:
            print("  %-26s %d octets attendus, %d arrives" % (nom, taille, vu))
            continue
        bons += 1
    return bons


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", action="store_true", help="ecrire, au lieu de dire")
    args = ap.parse_args(argv)
    conf = hosts()
    fige = figer(conf) if args.pull else None
    noms = inventaire(conf, fige)
    total = sum(taille for _, taille in noms)
    print("%d fichier(s) irremplacables, %.0f ko" % (len(noms), total / 1024))
    for nom, taille in noms:
        print("  %-26s %7d o" % (nom, taille))
    print()
    print("jamais ramenes par cet outil, ils portent des identifiants:")
    for nom in SECRETS:
        print("  %s" % nom)
    if not args.pull:
        print()
        print("rien ecrit. --pull pour ramener.")
        return 0
    dossier = ROOT / "backups" / CHAINE / time.strftime("%Y%m%d-%H%M%S")
    dossier.mkdir(parents=True, exist_ok=True)
    bons = ramener(conf, dossier, noms, fige)
    distant(conf, "rm -rf %s\n" % fige)
    print()
    print("%d/%d fichier(s) ramenes avec la bonne taille dans %s"
          % (bons, len(noms), dossier))
    return 0 if bons == len(noms) else 1


if __name__ == "__main__":
    sys.exit(main())
