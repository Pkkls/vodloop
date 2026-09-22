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

# Les titres de la chaine sont ecrits par leur auteur, pas par nous: celui du
# 2026-09-22 portait un crochet japonais. La console Windows encode en cp1252,
# qui ne sait pas l ecrire, et print() leve alors UnicodeEncodeError. --plan est
# mort au cinquieme titre de sa propre file, et --fetch serait mort au meme
# endroit, apres avoir choisi quoi prendre et avant de le prendre. Le nom d une
# video n est pas une raison d interrompre le relais: il s affiche approxime.
sys.stdout.reconfigure(errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from drift import hosts, ssh_argv, remote  # noqa: E402

CHANNEL = "nanatty247"
FAR = "/home/ubuntu/v2/%s" % CHANNEL
STORE = pathlib.Path(os.environ.get("VODLOOP_STORE", r"D:\vodloop"))
HANGAR = STORE / "hangar" / CHANNEL
PART = STORE / "part"
LEDGER = STORE / "state" / "fetched.tsv"
COOKIES = STORE / "state" / "cookies.txt"

# Le debit soutenu au-dela duquel l adresse a ete refusee le 2026-09-14: 28 Go
# en dix heures, soit 2800 Mo/h. On reste franchement dessous, et la fenetre est
# glissante et non un compteur journalier, parce que c est un debit qui a ete
# puni, pas un total.
HOUR_MB = int(os.environ.get("VODLOOP_HOUR_MB", 1800))
# Un plafond horaire ne gouverne rien quand l unite de travail pese cinq heures
# de video. Mesure du 2026-09-22: 1800 Mo/h annonces, 5938 Mo pris dans l heure,
# parce que le plafond etait lu avant de choisir et jamais contre la taille de
# ce qui venait. En regime ca donne un fichier par heure, soit 2.3 a 6.8 Go/h
# selon le fichier, au-dessus des 2800 Mo/h qui ont fait murer l adresse.
# Le budget se compte donc sur une fenetre ou un fichier entier tient, et ce qui
# vient est pese avec ce qui est deja pris. 1800 Mo/h de moyenne tiennent, le
# tampon de 40 Go se remplit en une vingtaine d heures, et aucune heure ne part
# seule au-dessus du debit puni.
WINDOW_H = int(os.environ.get("VODLOOP_WINDOW_H", 6))
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


def _hour_from(lines, floor):
    total = 0
    for line in lines:
        bits = line.split("\t") if "\t" in line else line.split()
        if len(bits) >= 2 and bits[0].strip().isdigit() and float(bits[0]) >= floor:
            try:
                total += int(bits[1])
            except ValueError:
                pass
    return total // 1024


def budget_mb():
    """Ce que la fenetre entiere autorise."""
    return HOUR_MB * WINDOW_H


def peut_prendre(pris_mb, attendu_mb):
    """Le plafond se lit contre la taille de ce qui vient, pas seulement contre
    ce qui est deja pris. Lu autrement, un tampon vide autorise n importe quel
    fichier, et c est ainsi que 5938 Mo sont passes sous 1800 le 2026-09-22.
    """
    return pris_mb + attendu_mb <= budget_mb()


def spent_in_window(conf=None):
    """Mo pris dans la fenetre glissante, PAR L ADRESSE, pas par moi.

    Le PC et la carte sortent par la meme adresse publique, mesure le
    2026-09-22: 82.67.100.152 des deux cotes. Deux gouverneurs qui s ignorent
    sur une seule adresse, c est un gouverneur qui ne gouverne rien, et c est
    la somme des deux que YouTube voit. Le registre de la carte est donc lu
    avec le notre, et le plafond s applique au total.
    """
    floor = time.time() - WINDOW_H * 3600
    mine = _hour_from(
        LEDGER.read_text(encoding="utf-8", errors="ignore").splitlines()
        if LEDGER.exists() else [], floor)
    if conf is None:
        return mine
    try:
        far = remote("claw", conf, "cat /root/v2/fetched.tsv 2>/dev/null")
        return mine + _hour_from(far.splitlines(), floor)
    except SystemExit:
        # carte injoignable: on compte ce qu on sait, et on le dit
        print("  (carte injoignable, son quota n est pas compte)")
        return mine


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
            "-o", "%(title).80B-%(id)s.%(ext)s"]
    # Le pot, s il existe. Anonyme sinon: la chaine tourne comme ca depuis le
    # debut, l absence de pot ralentit, elle n arrete pas.
    if COOKIES.exists():
        argv += ["--cookies", str(COOKIES)]
    argv += ["https://www.youtube.com/watch?v=%s" % vid]
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


def held_elsewhere():
    """Un seul relais a la fois, sinon deux courses demandent la meme video.

    Le verrou porte son age: un processus tue laisse le fichier, et un verrou
    qu on ne sait pas perimer est un relais qui ne repart jamais.
    """
    lock = STORE / "state" / "relay.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 3 * 3600:
        return lock, True
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock, False


def verify(path, attendu_s):
    """(bon, pourquoi). Un fichier qu on n a pas ouvert n est pas un fichier livre.

    Le relais comptait un telechargement comme reussi sur le code de sortie de
    yt-dlp seul. C est precisement ce qui a coute 8 Go a la carte le
    2026-09-22: les deux pistes etaient la, le merge est mort a la derniere
    image, et ce qui restait avait l air d un fichier. Deux pistes et une duree
    qui tient la route, sinon ce sont des octets depenses pour rien, et ils sont
    inscrits au registre comme tels parce que l adresse les a payes pareil.
    """
    argv = ["ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type", "-of", "csv=p=0", str(path)]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=300).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "ffprobe n a pas tourne: %s" % str(exc)[:50]
    pistes = {l.strip() for l in out.splitlines() if l.strip() in ("video", "audio")}
    duree = 0.0
    for ligne in out.splitlines():
        try:
            duree = max(duree, float(ligne.strip()))
        except ValueError:
            continue
    if not {"video", "audio"} <= pistes:
        return False, "pistes trouvees: %s" % (", ".join(sorted(pistes)) or "aucune")
    if attendu_s and duree < attendu_s * 0.97:
        return False, ("tronque: %.0f s sur %d attendues (%.0f%%)"
                       % (duree, attendu_s, 100 * duree / attendu_s))
    return True, "%.1f h, video et audio" % (duree / 3600)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--fetch", type=int, default=0)
    ap.add_argument("--ship", action="store_true")
    args = ap.parse_args()
    conf = hosts()
    for d in (HANGAR, PART, LEDGER.parent):
        d.mkdir(parents=True, exist_ok=True)
    lock = None
    if args.fetch:
        lock, pris = held_elsewhere()
        if pris:
            print("un relais tourne deja (%s), rien fait" % lock)
            return 0
    try:
        return run(args, conf)
    finally:
        if lock is not None and lock.exists():
            lock.unlink()


def run(args, conf):

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
    print("  pris en %2d h.  %d Mo sur %d autorises (PC + carte, une seule adresse)"
          % (WINDOW_H, spent_in_window(conf), budget_mb()))
    print("  session       %s" % ("connectee" if COOKIES.exists() else
                                  "anonyme (tools/cookies.py --install)"))
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
            while not peut_prendre(spent_in_window(conf), attendu):
                if waited == 0:
                    print("  %d Mo pris sur %d dans les %d h, le prochain en "
                          "demande %d: on patiente"
                          % (spent_in_window(conf), budget_mb(), WINDOW_H, attendu))
                time.sleep(60)
                waited += 1
                if waited > 90:
                    # break et non return: renoncer au quota n est pas une
                    # raison de ne pas livrer ce qui est deja sur le disque.
                    print("  toujours au plafond apres 90 min, on n en prend plus")
                    break
            if not peut_prendre(spent_in_window(conf), attendu):
                break
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
            # Les octets sont inscrits avant le verdict: l adresse les a payes,
            # que le fichier serve ou non. Un registre qui ne compte que les
            # reussites sous-estime exactement ce qu il est la pour brider.
            with LEDGER.open("a", encoding="utf-8") as fh:
                fh.write("%d\t%d\t%s\n" % (time.time(), size // 1024, vid))
            bon, pourquoi = verify(got, secs)
            if not bon:
                got.unlink(missing_ok=True)
                print("     %d Mo pris et jetes: %s" % (size // 2**20, pourquoi))
                continue
            got.replace(HANGAR / got.name)
            print("     %d Mo en %.0f s, %s"
                  % (size // 2**20, time.time() - began, pourquoi))
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
