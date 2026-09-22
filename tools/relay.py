#!/usr/bin/env python3
"""The PC as a relay: fetch to a local disk, hand to Oracle as it has room.

    python tools/relay.py --plan          # what it would take, nothing fetched
    python tools/relay.py --fetch 8       # up to 8 recordings, paced
    python tools/relay.py --ship          # push what fits into Oracle's upload/
    python tools/relay.py --fetch 8 --ship

Why the PC and not the board. Measured on 2026-09-22: the PC and the board sit
behind the same router and answer to the same public address, so this buys no
new allowance at all. What it buys is that none of the allowance is wasted. The
board is one RISC-V core with 211 Mo of RAM, and in the last day it spent 8 Go
on a download whose merge then died of memory, plus four more postprocessing
failures. Bytes spent on a file that never arrives cost exactly as much as
bytes spent on one that does. The PC also holds a buffer this deep, where the
far side has fourteen spare gigabytes, so a pause upstream stops costing the
channel anything.

The picture asked for is the channel's own, avc1 at its floor with mp4a sound,
https rungs before m3u8. Not out of habit: m3u8 carries the same picture at a
higher bitrate, which is the same hour of video for more of the allowance.

Addresses come from deploy/hosts.env, which git ignores.
"""
import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from drift import hosts, ssh_argv, remote  # noqa: E402

CHANNEL = "nanatty247"
FAR = "/home/ubuntu/v2/%s" % CHANNEL
STORE = pathlib.Path(os.environ.get("VODLOOP_STORE", r"D:\vodloop"))
HANGAR = STORE / "hangar" / CHANNEL
PART = STORE / "part"
LEDGER = STORE / "state" / "fetched.tsv"

# Le debit soutenu au-dela duquel l adresse a ete refusee le 2026-09-14: 28 Go
# en dix heures, soit 2800 Mo/h. On reste franchement dessous, et la fenetre est
# glissante et non un compteur journalier, parce que c est un debit qui a ete
# puni, pas un total.
HOUR_MB = int(os.environ.get("VODLOOP_HOUR_MB", 1800))
PAUSE_S = int(os.environ.get("VODLOOP_PAUSE_S", 45))
# kil, 2026-09-22: "tu prends que 40 go max sur D". Le disque est le sien et
# n est pas a moi de remplir: le tampon a une taille, pas la place restante.
STORE_MAX_MB = int(os.environ.get("VODLOOP_STORE_MAX_GB", 40)) * 1024
# Mo par heure de video, mesure sur ce que la chaine recoit reellement. Sert a
# savoir si le prochain fichier tient sous le plafond avant de le demander,
# plutot que de s en apercevoir une fois les octets pris.
MB_PER_HOUR_VIDEO = int(os.environ.get("VODLOOP_MB_PER_HOUR", 780))
ID = re.compile(r"-([A-Za-z0-9_-]{11})\.mkv$")


def store_mb():
    """Ce que le tampon pese, partie en cours de telechargement comprise."""
    total = 0
    for d in (HANGAR, PART):
        for p in d.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    return total // 2**20


def have_locally():
    return {m.group(1) for p in HANGAR.glob("*.mkv") for m in [ID.search(p.name)] if m}


def spent_last_hour():
    """Mo pris dans la derniere heure glissante, d apres notre propre registre."""
    if not LEDGER.exists():
        return 0
    floor = time.time() - 3600
    total = 0
    for line in LEDGER.read_text(encoding="utf-8", errors="ignore").splitlines():
        bits = line.split("\t")
        if len(bits) >= 2 and bits[0].isdigit() and float(bits[0]) >= floor:
            total += int(bits[1])
    return total // 1024


def far_state(conf):
    """Ce qu Oracle a deja, ce qu il refuse, et ce qu il peut encore recevoir."""
    out = remote("oracle", conf,
                 "cd %s; cat state/candidates.tsv; echo '--CUT--'; "
                 "ls current queue aired upload 2>/dev/null; echo '--CUT--'; "
                 "cut -f2 state/rejected.tsv 2>/dev/null; echo '--CUT--'; "
                 "df -k . | awk 'NR==2 {print $4}'" % FAR)
    blocs = out.split("--CUT--")
    cands = []
    for line in blocs[0].splitlines():
        bits = line.rstrip("\n").split("\t")
        if len(bits) >= 2 and bits[1].strip().isdigit():
            cands.append((bits[0].strip(), int(bits[1]), bits[2] if len(bits) > 2 else ""))
    here = {m.group(1) for line in blocs[1].splitlines() for m in [ID.search(line.strip())] if m}
    refused = {l.strip() for l in blocs[2].splitlines() if len(l.strip()) == 11}
    libre_ko = int((blocs[3].strip() or "0").split()[0] or 0)
    return cands, here, refused, libre_ko // 1024


def choose(conf, limit):
    cands, far_here, refused, libre_mb = far_state(conf)
    mine = have_locally()
    skip = far_here | refused | mine
    picked = [c for c in cands if c[0] not in skip][:limit]
    return picked, len(cands), len(skip), libre_mb


def fetch_one(vid, want_h=720):
    """Le meme escalier de formats que la carte, pour que la suite ne change pas."""
    cap_mb, vid_mb = 10240, 9840
    rungs = [
        "bv*[vcodec^=avc1][protocol^=https][height<=%d][height>=%d][filesize<%dM]+ba[acodec^=mp4a]",
        "bv*[vcodec^=avc1][height<=%d][height>=%d][filesize_approx<%dM]+ba[acodec^=mp4a]",
        "bv*[vcodec^=avc1][protocol^=https][height<=%d][height>=%d][filesize<?%dM]+ba[acodec^=mp4a]",
    ]
    fmt = "/".join(r % (want_h, want_h, vid_mb) for r in rungs)
    argv = ["yt-dlp", "--force-ipv4", "--no-playlist", "--restrict-filenames",
            "--no-progress", "--max-filesize", "%dM" % cap_mb, "-f", fmt,
            "--merge-output-format", "mkv", "-P", str(PART),
            "-o", "%(title).80B-%(id)s.%(ext)s",
            "https://www.youtube.com/watch?v=%s" % vid]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=7200)
    log = (done.stdout or "") + (done.stderr or "")
    if "Sign in to confirm" in log:
        return None, "mur"
    got = sorted(PART.glob("*-%s.mkv" % vid))
    if done.returncode or not got:
        if "Requested format is not available" in log:
            return None, "pas d avc1 a %d lignes" % want_h
        return None, (log.strip().splitlines() or ["echec"])[-1][:120]
    return got[0], ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--fetch", type=int, default=0)
    ap.add_argument("--ship", action="store_true")
    args = ap.parse_args()
    conf = hosts()
    for d in (HANGAR, PART, LEDGER.parent):
        d.mkdir(parents=True, exist_ok=True)

    picked, n_cands, n_skip, libre_mb = choose(conf, max(args.fetch, 5))
    stock = sorted(HANGAR.glob("*.mkv"))
    stock_mb = sum(p.stat().st_size for p in stock) // 2**20
    occupe = store_mb()
    print("RELAIS %s" % STORE)
    print("  tampon        %.1f Go sur %.0f autorises, %d fichier(s)"
          % (occupe / 1024, STORE_MAX_MB / 1024, len(stock)))
    print("  place sur D:  %.1f Go (le plafond ci-dessus mord bien avant)"
          % (shutil.disk_usage(STORE).free / 2**30))
    print("  candidats     %d, dont %d deja vus ou refuses" % (n_cands, n_skip))
    print("  pris cette h. %d Mo sur %d autorises" % (spent_last_hour(), HOUR_MB))
    print("  libre Oracle  %.1f Go" % (libre_mb / 1024))
    print()
    if args.plan or not (args.fetch or args.ship):
        print("PROCHAINS DANS LA FILE")
        for vid, secs, title in picked:
            print("  %s  %5.1f h  %s" % (vid, secs / 3600, title[:58]))
        return 0

    if args.fetch:
        for vid, secs, title in picked[:args.fetch]:
            # Le plafond du tampon se verifie AVANT de demander, avec la taille
            # attendue du fichier: s en apercevoir apres coup, c est avoir deja
            # pris les octets et rempli le disque de quelqu un d autre.
            attendu = int(secs / 3600 * MB_PER_HOUR_VIDEO)
            if store_mb() + attendu > STORE_MAX_MB:
                if args.ship:
                    print("  tampon plein, on livre d abord a Oracle")
                    ship(conf, libre_mb)
                if store_mb() + attendu > STORE_MAX_MB:
                    print("  tampon a %d Mo sur %d autorises, le prochain en "
                          "demande %d: on s arrete la"
                          % (store_mb(), STORE_MAX_MB, attendu))
                    break
            waited = 0
            while spent_last_hour() >= HOUR_MB:
                if waited == 0:
                    print("  plafond horaire atteint, on patiente")
                time.sleep(60)
                waited += 1
                if waited > 90:
                    print("  toujours au plafond apres 90 min, on s arrete")
                    return 0
            print("  -> %s  %.1f h  %s" % (vid, secs / 3600, title[:50]))
            began = time.time()
            got, why = fetch_one(vid)
            if not got:
                print("     refuse: %s" % why)
                if why == "mur":
                    print("     YouTube demande une connexion: on s arrete la")
                    return 1
                continue
            size = got.stat().st_size
            final = HANGAR / got.name
            got.replace(final)
            with LEDGER.open("a", encoding="utf-8") as fh:
                fh.write("%d\t%d\t%s\n" % (time.time(), size // 1024, vid))
            print("     %d Mo en %.0f s" % (size // 2**20, time.time() - began))
            time.sleep(PAUSE_S)

    if args.ship:
        ship(conf, libre_mb)
    return 0


def ship(conf, libre_mb):
    """Ce qui tient au-dessus du plancher d Oracle part, le reste attend ici."""
    garde_mb = 5 * 1024 + 1024   # FLOOR_GB de la chaine, plus une marge
    envoyes = 0
    for p in sorted(HANGAR.glob("*.mkv"), key=lambda q: q.stat().st_mtime):
        taille_mb = p.stat().st_size // 2**20
        if libre_mb - taille_mb < garde_mb:
            print("  %s reste ici: Oracle n a plus la place" % p.name[:40])
            break
        argv = ["scp", "-i", os.path.expanduser(conf["ORACLE_KEY"]),
                "-o", "BatchMode=yes", str(p),
                "%s:%s/upload/" % (conf["ORACLE"], FAR)]
        if subprocess.run(argv, capture_output=True, timeout=3600).returncode:
            print("  envoi echoue: %s" % p.name[:50])
            continue
        p.unlink()
        libre_mb -= taille_mb
        envoyes += 1
        print("  livre a Oracle: %s (%d Mo)" % (p.name[:46], taille_mb))
    print("  %d fichier(s) livre(s)" % envoyes)


if __name__ == "__main__":
    sys.exit(main())
