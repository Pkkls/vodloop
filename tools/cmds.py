#!/usr/bin/env python3
"""Chaque commande du chat, jouee contre l etat reel de la chaine.

Tourne sur Oracle, la ou vivent les modules et l etat:

    scp tools/cmds.py ORACLE:/tmp/cmds.py
    ssh ORACLE 'cd /home/ubuntu/v2/nanatty247 && CHAN_ROOT=$PWD \
        PYTHONPATH=/home/ubuntu/v2/bin python3 /tmp/cmds.py'

Rien n est envoye sur Kick. On appelle les memes fonctions que le bot appelle
quand un message arrive, avec les vraies donnees, et on regarde ce qui sort:
qu il sorte quelque chose, que ca tienne dans la limite de la plateforme, et que
ce soit dans une seule langue.

Le garde d import en tete n est pas de la ceinture et bretelles. Le 2026-09-26
ce meme controle a rendu "encore multilingue" sur neuf commandes: il avait
charge /tmp/bot.py, oublie la quatre jours plus tot, parce qu un script lance
depuis /tmp met /tmp en tete du chemin. J ai failli defaire un correctif qui
marchait. Un harnais qui ne dit pas quel fichier il a lu ne mesure rien.
"""
import io
import os
import sys
import time

DEPLOYE = os.environ.get("VODLOOP_BIN", "/home/ubuntu/v2/bin")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import bot  # noqa: E402

attendu = os.path.join(DEPLOYE, "bot.py")
if os.path.realpath(bot.__file__) != os.path.realpath(attendu):
    sys.exit("module masque: %s au lieu de %s. Un bot.py traine sur le chemin."
             % (bot.__file__, attendu))

LIMITE = 500
qui = {"user_id": "999", "name": "controle"}
essais = [
    ("aide", []), ("now", []), ("liste", []), ("liste", ["2"]),
    ("link", []), ("stats", []), ("fetch", []),
    ("pick", []), ("pick", ["1"]), ("pick", ["9999"]), ("pick", ["abc"]),
    ("skip", []),
]


def cjk(texte):
    return any("　" <= c <= "鿿" for c in texte)


def main():
    print("module mesure : %s" % bot.__file__)
    print()
    data, now = bot.load(), time.time()
    muettes, longues, traduites = [], [], []
    for nom, args in essais:
        fn = bot.COMMANDS.get(nom)
        etiquette = "!%s%s" % (nom, " " + " ".join(args) if args else "")
        if fn is None:
            muettes.append(etiquette)
            print("  %-12s COMMANDE INCONNUE" % etiquette)
            continue
        try:
            texte = str(fn(data, now, qui, args) or "")
        except Exception as exc:
            muettes.append(etiquette)
            print("  %-12s LEVE %s: %s" % (etiquette, type(exc).__name__, str(exc)[:50]))
            continue
        if not texte.strip():
            muettes.append(etiquette)
        if len(texte) > LIMITE:
            longues.append(etiquette)
        # Les titres de la chaine sont en japonais pour certains: un morceau qui
        # commence par un numero de !list est un titre, pas une traduction.
        bouts = [m for m in texte.split(" · ")
                 if cjk(m) and not m.split()[0].rstrip(".").isdigit()]
        if bouts:
            traduites.append(etiquette)
        print("  %-12s %3d car  %s" % (etiquette, len(texte), texte[:88]))

    print()
    print("  %d commande(s) jouees" % len(essais))
    print("  sans reponse         : %s" % (muettes or "aucune"))
    print("  au-dessus de %d car  : %s" % (LIMITE, longues or "aucune"))
    print("  encore multilingues  : %s" % (traduites or "aucune"))
    return 1 if (muettes or longues or traduites) else 0


if __name__ == "__main__":
    sys.exit(main())
