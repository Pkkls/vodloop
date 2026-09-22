#!/usr/bin/env python3
"""Every desk-side check in one pass, with a real N/N at the end.

    python tools/gate.py            # tout, reseau compris
    python tools/gate.py --local    # seulement ce qui tourne sans reseau
    python tools/gate.py --witness  # chaque controle doit rougir

check.py answers what is true of the chain that is running, on Oracle. This
answers what is true here: that the suites still name things the code defines,
that the guard rules hold, that what runs is what is committed, and that the v2
suite passes against the deployed modules.

It exists because the tools it calls were each written after a failure and each
one of them is easy to forget. Three tools nobody remembers to run are three
tools that do not exist. One command with one number does.

The number at the end is the number that ran, not the number that was meant to
run: a step that could not start counts as a failure, never as a pass, because
the whole class of bug these were written for is a check that quietly does
nothing.
"""
import argparse
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOKS = pathlib.Path(os.path.expanduser("~")) / ".claude" / "hooks"


def run(argv, cwd=ROOT, timeout=900):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        done = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "n a pas demarre: %s" % str(exc)[:70]
    sortie = ((done.stdout or "") + (done.stderr or "")).strip().splitlines()
    resume = sortie[-1][:96] if sortie else "(aucune sortie)"
    return done.returncode == 0, resume


# Chaque etape rend (bon, resume), et sous temoin "bon" veut dire "le controle a
# bien rougi". Rien n est inverse apres coup: la premiere version l inversait
# dans main, et le garde, dont le temoin ne se lit pas dans un code de sortie,
# ressortait en echec alors qu il refusait correctement. Une inversion qui vaut
# pour trois etapes sur quatre est une inversion fausse.
def noms_morts(witness):
    argv = [sys.executable, "tools/deadrefs.py"] + (["--witness"] if witness else [])
    ok, resume = run(argv)
    return (not ok if witness else ok), resume


def regles_du_garde(witness):
    outil = HOOKS / "guardrails.py"
    if not outil.exists():
        return False, "garde introuvable: %s" % outil
    if witness:
        # son temoin ne vit pas dans un code de sortie mais dans le verdict
        _, verdict = run([sys.executable, str(outil), "--check", "git add -A"])
        return verdict.startswith("REFUSE"), verdict
    return run([sys.executable, str(outil), "--selftest"])


def derive(witness):
    argv = [sys.executable, "tools/drift.py"] + (["--witness"] if witness else [])
    ok, resume = run(argv, timeout=1800)
    return (not ok if witness else ok), resume


def suite_v2(witness):
    """La suite v2 contre les modules deployes, la ou ils tournent."""
    sys.path.insert(0, str(ROOT / "tools"))
    from drift import hosts, ssh_argv  # noqa: E402
    conf = hosts()
    local = ROOT / "tests" / "test_v2.py"
    if not local.exists():
        return False, "tests/test_v2.py introuvable"
    scp = ["scp", "-i", os.path.expanduser(conf["ORACLE_KEY"]), "-o", "BatchMode=yes",
           "-q", str(local), "%s:/tmp/gate_test_v2.py" % conf["ORACLE"]]
    if subprocess.run(scp, capture_output=True, timeout=300).returncode:
        return False, "envoi de la suite impossible"
    # Le fichier arrive de Windows: ses retours chariot partent avant de courir.
    #
    # Il court dans un dossier a lui, et sa sortie passe par un fichier. Les
    # deux pour la meme raison, mesuree le 2026-09-22. Lance depuis /tmp, le
    # dossier du script passe en tete de sys.path, et /tmp portait cent
    # quarante .py laisses par des sessions passees, dont cut.py, chan.py,
    # feed.py, supply.py et kickapi.py: la suite jugeait ces copies-la et pas
    # le code deploye. Et le resultat partait dans "| tail -1", donc le code de
    # sortie lu etait celui de tail, qui ne rate jamais. Cette etape a rendu
    # OK pendant des jours sans pouvoir rendre autre chose.
    dossier = "/tmp/vodloop-gate"
    lance = (f"rm -rf {dossier} && mkdir -p {dossier} && "
             f"tr -d '\\r' < /tmp/gate_test_v2.py > {dossier}/suite.py && "
             "cd /home/ubuntu/v2/nanatty247 && "
             "CHAN_ROOT=/home/ubuntu/v2/nanatty247 "
             f"PYTHONPATH=/home/ubuntu/v2/bin python3 {dossier}/suite.py "
             f"> {dossier}/sortie 2>&1; code=$?; tail -1 {dossier}/sortie; exit $code")
    if witness:
        # le rouge passe par la meme plomberie que le vert: un echec doit
        # traverser la redirection, le $? et le exit, sinon le temoin ne
        # temoigne que de lui-meme
        lance = (f"rm -rf {dossier} && mkdir -p {dossier} && "
                 f"printf 'raise SystemExit(1)\\n' > {dossier}/suite.py && "
                 f"python3 {dossier}/suite.py > {dossier}/sortie 2>&1; code=$?; "
                 "echo \"temoin: la suite sort en $code\"; exit $code")
    done = subprocess.run(ssh_argv("oracle", conf, lance),
                          capture_output=True, text=True, timeout=1200,
                          env=dict(os.environ, MSYS_NO_PATHCONV="1"))
    lignes = (done.stdout or done.stderr or "").strip().splitlines()
    resume = lignes[-1][:96] if lignes else "(aucune sortie)"
    ok = done.returncode == 0
    return (not ok if witness else ok), resume


ERRORGUARD = [
    pathlib.Path(r"C:\Users\kil\Downloads\02 - Projects\disk-triage\errorguard.py"),
    pathlib.Path(r"C:\Users\kil\Downloads\disk-triage\errorguard.py"),
]


def garde_des_commandes(witness):
    """errorguard et son propre corpus historique.

    Il est ici parce qu il a passe des semaines a sortir 0 sur chaque commande:
    son hook pointait sur un chemin perime et personne ne l a su. Un garde dont
    la mort est invisible est pire qu une absence de garde, et le seul remede
    est que quelque chose le compte.
    """
    outil = next((p for p in ERRORGUARD if p.exists()), None)
    if outil is None:
        return False, "errorguard introuvable dans %d chemin(s) connus" % len(ERRORGUARD)
    if witness:
        # Son rouge: une commande de son corpus doit etre attrapee, et --check
        # rend 1 quand il attrape. Lu sur le code de sortie et non sur le texte:
        # le resume ne garde que la derniere ligne, et l identifiant de regle
        # est sur celle d avant. C est la meme erreur que de lire un verdict a
        # travers un tube, en plus petit.
        ok, verdict = run([sys.executable, str(outil), "--check",
                           "node --check /tmp/x.js 2>&1 | tail -5 && echo 'syntax valid'"])
        return (not ok), verdict
    return run([sys.executable, str(outil), "--selftest"])


def livraison_du_relais(witness):
    """Ce que le relais garde et ce qu il jette: la suite porte ses temoins."""
    cible = "tests/test_relay.py"
    if witness:
        # son rouge ne se fabrique pas en argument: on lui donne un fichier
        # qui n a jamais existe, et il doit refuser plutot que passer
        code = ("import sys; sys.path.insert(0, 'tools'); import relay; "
                "bon, why = relay.verify('nexiste_pas.mkv', 10); "
                "print(why); sys.exit(0 if bon else 1)")
        ok, resume = run([sys.executable, "-c", code])
        return (not ok), resume
    return run([sys.executable, cible])


ETAPES = [
    ("noms morts dans les suites", noms_morts, False),
    ("regles du garde", regles_du_garde, False),
    ("garde des commandes", garde_des_commandes, False),
    ("livraison du relais", livraison_du_relais, False),
    ("derive deploye/commite", derive, True),
    ("suite v2 contre le deploye", suite_v2, True),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="sauter ce qui demande le reseau")
    ap.add_argument("--witness", action="store_true", help="chaque controle doit rougir")
    args = ap.parse_args()

    faits = passes = 0
    for nom, fonction, reseau in ETAPES:
        if reseau and args.local:
            print("  passe    %-30s (--local)" % nom)
            continue
        faits += 1
        try:
            ok, resume = fonction(args.witness)
        except SystemExit as exc:
            ok, resume = False, "interrompu: %s" % str(exc)[:70]
        except Exception as exc:  # une etape qui explose est une etape ratee
            ok, resume = False, "%s: %s" % (type(exc).__name__, str(exc)[:60])
        passes += 1 if ok else 0
        print("  %-8s %-30s %s" % ("OK" if ok else "ECHEC", nom, resume))

    print()
    print("%d/%d" % (passes, faits))
    if args.witness:
        print("temoin: %d/%d signifie que chaque controle sait rougir" % (passes, faits))
    return 0 if passes == faits else 1


if __name__ == "__main__":
    sys.exit(main())
