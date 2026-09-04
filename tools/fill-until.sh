#!/bin/sh
# Keep adding batches from the allowlisted channel until the library reaches a
# size, or the disk gets tight, whichever comes first.
#
# The floor is not politeness. Chunks are written to the same volume, so a full
# disk stops the encoder and the channel falls back to the standby clip: the
# library would otherwise grow until it took the stream off the air.
#
#     sh tools/fill-until.sh 30      stop at a 30 GB library
set -u
TARGET_GB="${1:-30}"
FLOOR_GB="${VODLOOP_FLOOR_GB:-5}"
BATCH="${VODLOOP_BATCH:-10}"
CHANNEL="${VODLOOP_CHANNEL:-https://www.youtube.com/channel/UCqQlq8W2OVF8Dn2LYTdz3lA/videos}"
KEY="${VODLOOP_KEY:-$HOME/.ssh/ssh-key-2026-05-07.key}"
HOST="${VODLOOP_HOST:-ubuntu@89.168.60.67}"
OUT="${VODLOOP_OUT:-$HOME/Downloads/vodloop-out}"
HERE=$(dirname "$0")
LOG="$OUT/fill-until.log"

# On Windows "python3" is a Store shim that prints an advert and exits 0, so a
# run picked no ids, produced an empty batch, and reported the channel as
# exhausted. Pick an interpreter that answers, and refuse to run without one.
PY=""
for candidate in python3 python py; do
    if "$candidate" -c "import sys" > /dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "aucun interpreteur python utilisable" >&2
    exit 1
fi

mkdir -p "$OUT"
: > "$LOG"
echo "interpreteur: $PY" >> "$LOG"

remote() { ssh -n -o BatchMode=yes -i "$KEY" "$HOST" "$1"; }

# listed once: 800 odd entries do not change between batches, and asking again
# every round costs a minute for nothing
if [ ! -s "$OUT/channel.txt" ]; then
    echo "listing de la chaine..." >> "$LOG"
    yt-dlp --flat-playlist --no-warnings --print "%(id)s|%(duration)s" "$CHANNEL" \
        > "$OUT/channel.txt" 2>> "$LOG"
fi

round=0
while : ; do
    round=$((round + 1))
    lib=$(remote "du -sm ~/videos | cut -f1")
    free=$(remote "df -Pm / | tail -1 | awk '{print \$4}'")
    lib_gb=$((lib / 1024))
    free_gb=$((free / 1024))
    echo "tour $round: bibliotheque ${lib_gb} Go, libre ${free_gb} Go" >> "$LOG"

    if [ "$lib_gb" -ge "$TARGET_GB" ]; then
        echo "ARRET: cible de ${TARGET_GB} Go atteinte (${lib_gb} Go)" >> "$LOG"
        break
    fi
    if [ "$free_gb" -le "$FLOOR_GB" ]; then
        echo "ARRET: plus que ${free_gb} Go libres, plancher a ${FLOOR_GB}" >> "$LOG"
        break
    fi

    remote "ls -1 ~/videos" > "$OUT/have.txt" 2>> "$LOG"
    rm -f "$OUT/batch.txt"
    # the exit code decides, not the size of what came out: an empty batch from
    # a selector that crashed reads exactly like a channel with nothing left
    if ! "$PY" - "$OUT" "$BATCH" >> "$LOG" 2>&1 <<'PY'
import pathlib, sys
out = pathlib.Path(sys.argv[1])
batch = int(sys.argv[2])
rows = [l.strip().split("|") for l in
        (out / "channel.txt").read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
have = (out / "have.txt").read_text(encoding="utf-8", errors="replace")
todo = [r[0] for r in rows
        if len(r) > 1 and r[1].isdigit() and 300 <= int(r[1]) <= 3600 and r[0] not in have]
(out / "batch.txt").write_bytes(("\n".join(todo[:batch]) + "\n").encode())
print(f"{len(todo)} manquantes, lot de {len(todo[:batch])}")
PY
    then
        echo "ARRET: la selection des ids a echoue" >> "$LOG"
        break
    fi
    if [ ! -s "$OUT/batch.txt" ]; then
        echo "ARRET: plus rien a ajouter sur cette chaine" >> "$LOG"
        break
    fi

    sh "$HERE/fill-library.sh" "$OUT/batch.txt" >> "$LOG" 2>&1
    pushed=$(grep -c "pousse (" "$OUT/fill.log")
    echo "tour $round termine, $pushed pousse(s)" >> "$LOG"
    if [ "$pushed" -eq 0 ]; then
        echo "ARRET: un tour entier sans un seul telechargement, le mur est en place" >> "$LOG"
        break
    fi
done

echo "TERMINE" >> "$LOG"
remote 'echo "bibliotheque: $(ls -1 ~/videos | wc -l) fichiers, $(du -sh ~/videos | cut -f1)"' >> "$LOG" 2>&1
remote 'df -h / | tail -1' >> "$LOG" 2>&1
