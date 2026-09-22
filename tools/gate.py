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
    lance = ("tr -d '\\r' < /tmp/gate_test_v2.py > /tmp/gate_t.py; "
             "cd /home/ubuntu/v2/nanatty247 && "
             "CHAN_ROOT=/home/ubuntu/v2/nanatty247 "
             "PYTHONPATH=/home/ubuntu/v2/bin python3 /tmp/gate_t.py 2>&1 | tail -1")
    if witness:
        lance = "echo 'temoin: on force l echec'; exit 1"
    done = subprocess.run(ssh_argv("oracle", conf, lance),
                          capture_output=True, text=True, timeout=1200,
                          env=dict(os.environ, MSYS_NO_PATHCONV="1"))
    lignes = (done.stdout or done.stderr or "").strip().splitlines()
    resume = lignes[-1][:96] if lignes else "(aucune sortie)"
    ok = done.returncode == 0
    return (not ok if witness else ok), resume


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
