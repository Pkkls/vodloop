#!/usr/bin/env python3
"""Check a cookie jar and put it where the fetchers look, without reading it.

    python tools/cookies.py --check <fichier>     # verdict only
    python tools/cookies.py --install <fichier>   # here and on the board

YouTube said "Sign in to confirm you're not a bot" twelve times in the board's
log, and the board has never had a jar: every fetch since the beginning has been
anonymous. Signing in is not a way around that message, it is the thing the
message asks for, and the allowance of a named account has nothing to do with
the allowance of nobody.

Nothing here prints a cookie value, and nothing here reads a browser. This tool
takes a file somebody else produced and answers one question: does it carry a
signed-in YouTube session. It answers that from the NAMES present, which is all
that question needs.

A jar without those names is refused rather than installed. Installing one
would replace anonymous fetching, which works, with fetching that still works
and now also carries a file everyone believes is doing something.
"""
import argparse
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from drift import hosts, ssh_argv  # noqa: E402

LOCAL = pathlib.Path(os.environ.get("VODLOOP_STORE", r"D:\vodloop")) / "state" / "cookies.txt"
FAR = "/root/v2/cookies.txt"
# de quoi est faite une session YouTube connectee
AUTH = ("SID", "HSID", "SSID", "APISID", "SAPISID",
        "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO")


def inspect(path):
    """(noms d authentification presents, nombre de lignes youtube). Aucune valeur."""
    noms, lignes = set(), 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        champs = line.split("\t")
        if len(champs) < 7:
            continue
        domaine, nom = champs[0].lower(), champs[5]
        if "youtube" in domaine or "google" in domaine:
            lignes += 1
            if nom in AUTH:
                noms.add(nom)
    return noms, lignes


def verdict(path):
    if not path.exists():
        return False, "fichier introuvable: %s" % path
    noms, lignes = inspect(path)
    if lignes == 0:
        return False, "aucun cookie youtube ou google dans ce fichier"
    if not noms:
        return False, ("%d cookies youtube/google, mais aucun cookie de session: "
                       "ce navigateur n etait pas connecte" % lignes)
    return True, ("%d cookies youtube/google, session connectee (%s)"
                  % (lignes, ", ".join(sorted(noms))))


def wsl_path(chemin):
    """Un chemin Windows tel que wsl le comprend. Pur.

    Ne connaissait que D:\\, parce que c est la que le tampon vit aujourd hui.
    VODLOOP_STORE peut le poser ailleurs, et un pot de cookies qui part sur un
    chemin que wsl ne resout pas echoue au pire moment: celui ou kil agit enfin.
    """
    texte = str(chemin).replace("\\", "/")
    if len(texte) > 1 and texte[1] == ":":
        return "/mnt/%s%s" % (texte[0].lower(), texte[2:])
    return texte


def install(path, conf):
    LOCAL.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() != LOCAL.resolve():
        LOCAL.write_bytes(path.read_bytes())
    print("  pose ici       : %s" % LOCAL)
    # La carte lit son propre pot; elle tient deja la cle SSH d Oracle, ce n est
    # pas un cran de confiance de plus.
    argv = ["wsl", "scp", "-i", conf["CLAW_KEY"], "-o", "BatchMode=yes",
            wsl_path(LOCAL), "%s:%s" % (conf["CLAW"], FAR)]
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    if subprocess.run(argv, capture_output=True, timeout=120, env=env).returncode:
        print("  carte          : envoi echoue")
        return 1
    # Le chmod etait lance et son verdict jete: l outil annoncait 600 sans
    # jamais l avoir regarde, sur un fichier d identifiants. On lit le mode
    # qu on obtient et on dit celui-la.
    vu = subprocess.run(ssh_argv("claw", conf, "chmod 600 %s 2>/dev/null; "
                                 "ls -l %s | cut -c1-10" % (FAR, FAR)),
                        capture_output=True, timeout=60, env=env)
    mode = vu.stdout.decode(errors="replace").strip()
    if vu.returncode or not mode.startswith("-rw-------"):
        print("  carte          : droits non confirmes (%s)" % (mode or "muet"))
        return 1
    print("  pose sur carte : %s (%s, verifie)" % (FAR, mode))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", metavar="FICHIER")
    ap.add_argument("--install", metavar="FICHIER")
    args = ap.parse_args()
    cible = args.check or args.install
    if not cible:
        print(__doc__)
        return 0
    path = pathlib.Path(cible)
    bon, pourquoi = verdict(path)
    print("  %s" % pourquoi)
    if not bon:
        print("  refuse: un pot sans session ne sert a rien et fait croire le contraire")
        return 1
    if args.install:
        return install(path, hosts())
    return 0


if __name__ == "__main__":
    sys.exit(main())
