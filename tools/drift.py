#!/usr/bin/env python3
"""What is running against what is committed, byte for byte.

Written on 2026-09-22, after answering the same question three times in one
session and getting it wrong twice: first "everything matches" when the board
was running a different hangar, then "eight files differ" when my own hashing
was broken and the bytes were identical. A comparison that says "different"
without being able to say WHERE is a broken probe, not a finding, so this one
locates the first differing byte or it does not claim a difference.

    python tools/drift.py              # the table, exit 1 if anything drifted
    python tools/drift.py --witness    # prove the check can go red

Connection details are not in this repo. Put them in deploy/hosts.env, which
git ignores:

    ORACLE=user@host
    ORACLE_KEY=~/.ssh/some-key
    CLAW=root@192.168.1.x
    CLAW_KEY=/home/you/.ssh/some-key    # path inside WSL, reached through wsl
"""
import argparse
import base64
import hashlib
import io
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOSTS = ROOT / "deploy" / "hosts.env"

# (machine, repertoire distant, repertoire du depot, fichiers)
# Sur Oracle, v2/bin/install.sh est une copie perimee: le vrai vit a la racine
# de v2, et c est celui-la qui est compare.
PLAN = [
    ("oracle", "/home/ubuntu/vodloop/bin", "bin", [
        "allowlist.py", "chat.py", "chatlogic.py", "collector.py", "common.py",
        "dashboard.py", "feeder.py", "janitor.py", "kickfetch.py", "live.py",
        "medic.py", "normalise.py", "oauth.py", "prep.py", "quality.py",
        "retire-kick.py", "slice.py", "tgbot.py", "make-filler.sh", "pusher.sh"]),
    ("oracle", "/home/ubuntu/v2/bin", "v2/oracle", [
        "bot.py", "ceiling.py", "chan.py", "cut.py", "feed.py", "kick.py",
        "kickapi.py", "kickfetch.py", "shorts.py", "supply.py", "watch.py",
        "push.sh",
        "systemd/vodloop-v2-bot@.service", "systemd/vodloop-v2-cut@.service",
        "systemd/vodloop-v2-feed@.service", "systemd/vodloop-v2-push@.service"]),
    ("oracle", "/home/ubuntu/v2", "v2/oracle", ["install.sh"]),
    ("claw", "/usr/bin", "v2/claw", ["hangar"]),
]


def hosts():
    """The addresses, from the environment or from the file git ignores."""
    conf = dict(os.environ)
    if HOSTS.exists():
        for line in io.open(HOSTS, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                conf.setdefault(key.strip(), value.strip())
    missing = [k for k in ("ORACLE", "ORACLE_KEY", "CLAW", "CLAW_KEY")
               if not conf.get(k)]
    if missing:
        sys.exit("adresses manquantes: %s. Voir l en-tete de ce fichier."
                 % ", ".join(missing))
    return conf


def ssh_argv(machine, conf, payload):
    if machine == "oracle":
        return ["ssh", "-i", os.path.expanduser(conf["ORACLE_KEY"]),
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                conf["ORACLE"], payload]
    # la carte n est joignable que depuis WSL, ou vit sa cle
    return ["wsl", "ssh", "-i", conf["CLAW_KEY"], "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=25", conf["CLAW"], payload]


def remote(machine, conf, script):
    """Run one shell script on a machine and hand back stdout.

    The script travels as base64, not as text. Between Python, the Windows
    command line, wsl, ssh and the far shell there are five layers and each one
    eats a level of quoting: sending the pipes and quotes themselves is how you
    get `sh: syntax error: unexpected "|"`. Base64 has no character any of them
    wants to touch.
    """
    blob = base64.b64encode(script.encode("utf-8")).decode("ascii")
    payload = "echo %s | base64 -d | sh" % blob
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    done = subprocess.run(ssh_argv(machine, conf, payload),
                          capture_output=True, timeout=180, env=env)
    if done.returncode:
        sys.exit("%s injoignable: %s" % (machine, done.stderr.decode(errors="replace")[:200]))
    return done.stdout.decode(errors="replace")


def lf(data):
    """The same bytes the deploy step writes: no carriage returns."""
    return data.replace(b"\r", b"")


def digest(data):
    return hashlib.md5(lf(data)).hexdigest()


def first_difference(a, b):
    """Where two byte strings part company, or None if they do not."""
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))


def fetch(machine, conf, path):
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    argv = ssh_argv(machine, conf, "cat %s" % path)
    return subprocess.run(argv, capture_output=True, timeout=120, env=env).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--witness", action="store_true",
                    help="alter one local file in memory, so the table must go red")
    args = ap.parse_args()
    conf = hosts()

    drifted = absent = 0
    checked = 0
    for machine, far, near, names in PLAN:
        # Une seule connexion par groupe: les empreintes de tout le lot. Le
        # retour chariot est retire des DEUX cotes avant de hacher, sinon un
        # fichier deploye en CRLF sort en derive alors que ses octets utiles
        # sont les memes, et la difference reste introuvable.
        # Pas de $( ) ni de guillemets imbriques: la commande traverse ssh, et
        # sur la carte elle traverse wsl en plus. Chaque couche en mange une.
        listing = remote(machine, conf,
                         "cd %s 2>/dev/null; for f in %s; do tr -d '\\r' < $f | "
                         "md5sum | awk -v n=$f '{print $1, n}'; done"
                         % (far, " ".join(names)))
        theirs = {}
        for line in listing.splitlines():
            bits = line.split(None, 1)
            if len(bits) == 2 and len(bits[0]) == 32:
                theirs[bits[1].strip().lstrip("*")] = bits[0]

        print("%s:%s" % (machine, far))
        for name in names:
            checked += 1
            local = ROOT / near / name
            if not local.exists():
                print("  ABSENT ICI    %s" % name)
                absent += 1
                continue
            raw = local.read_bytes()
            # Le temoin altere les octets COMPARES, pas seulement l empreinte:
            # sinon il produit exactement la signature de la sonde cassee que
            # cet outil existe pour attraper, une derive dont la difference est
            # introuvable.
            if args.witness and checked == 1:
                raw = raw + b"# temoin\n"
            mine = digest(raw)
            far_sum = theirs.get(name)
            if far_sum is None:
                print("  ABSENT LA-BAS %s" % name)
                absent += 1
            elif far_sum == mine:
                print("  identique     %s" % name)
            else:
                here = lf(raw)
                there = lf(fetch(machine, conf, "%s/%s" % (far, name)))
                at = first_difference(here, there)
                where = ("octet %d" % at) if at is not None else "introuvable"
                print("  DERIVE        %-42s depot %d o, distant %d o, "
                      "1re difference: %s" % (name, len(here), len(there), where))
                drifted += 1
        print()

    print("%d fichier(s) compares, %d en derive, %d absent(s)"
          % (checked, drifted, absent))
    if args.witness:
        print("temoin: le premier fichier DOIT apparaitre en derive ci-dessus")
    return 1 if (drifted or absent) else 0


if __name__ == "__main__":
    sys.exit(main())
