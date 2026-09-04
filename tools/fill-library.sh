#!/bin/sh
# Add videos to the library from the machine that can still reach YouTube.
#
# The streaming host answers the bot check on its datacenter address, so it
# cannot download anything itself. This runs where a browser would: it fetches,
# hands the file to the server, and keeps nothing locally.
#
#     yt-dlp --flat-playlist --print "%(id)s" <channel url> > ids.txt
#     sh tools/fill-library.sh ids.txt
#
# Files land in the library, not in incoming/: the library is replayed forever,
# incoming/ is a single pass. The format preference asks for exactly what a
# chunk is, so the result plays as a remux; run tools/normalise-library.sh on
# the server afterwards for whatever YouTube had in another shape.
#
# YouTube also answers the bot check to a home address once it has pulled too
# much too fast, and that does not decay in minutes. Pacing is the only lever
# here that does not involve handing an account session to a script, so a run
# is slow on purpose and backs off further every time it is refused.
set -u
OUT="${VODLOOP_OUT:-$HOME/Downloads/vodloop-out}"
KEY="${VODLOOP_KEY:-$HOME/.ssh/ssh-key-2026-05-07.key}"
HOST="${VODLOOP_HOST:-ubuntu@89.168.60.67}"
PAUSE="${VODLOOP_PAUSE:-45}"
BACKOFF="${VODLOOP_BACKOFF:-600}"
TRIES="${VODLOOP_TRIES:-3}"
IDS="$1"
LOG="$OUT/fill.log"

mkdir -p "$OUT"
: > "$LOG"

FORMAT='bv*[height=720][fps=50][vcodec^=avc1]+ba/bv*[height<=720][vcodec^=avc1]+ba/b[height<=720]/b'

push() {
    file="$1"
    base=$(basename "$file")
    # uploaded under .part so the server never sees a half-copied file
    scp -q -o BatchMode=yes -i "$KEY" "$file" "$HOST:videos/$base.part" || return 1
    # -n, or ssh drains the id list this runs inside and the loop ends after one
    ssh -n -o BatchMode=yes -i "$KEY" "$HOST" "mv ~/videos/'$base'.part ~/videos/'$base'" || return 1
    rm -f "$file"
    return 0
}

fetch() {
    yt-dlp --no-warnings --no-progress --restrict-filenames -P "$OUT" -f "$FORMAT" --merge-output-format mp4 -o '%(title)s-%(id)s.%(ext)s' "https://www.youtube.com/watch?v=$1" >> "$LOG" 2>&1
}

# anything left here by an interrupted run goes up first
for f in "$OUT"/*.mp4 "$OUT"/*.mkv; do
    [ -f "$f" ] || continue
    case "$f" in *.temp.mp4|*.f[0-9]*.mp4|*.part) continue;; esac
    size=$(du -m "$f" | cut -f1)
    if push "$f"; then
        echo "reprise: $(basename "$f") pousse (${size} Mo)" >> "$LOG"
    else
        echo "reprise: $(basename "$f") ECHEC upload" >> "$LOG"
    fi
done

n=0
total=$(grep -c . "$IDS")
# read on fd 3, so nothing in the body can drain the list
while read -r id <&3; do
    id=$(printf %s "$id" | tr -d '\015')
    [ -n "$id" ] || continue
    n=$((n + 1))
    echo "[$n/$total] $id telechargement" >> "$LOG"

    try=0
    ok=0
    while [ "$try" -lt "$TRIES" ]; do
        try=$((try + 1))
        if fetch "$id"; then
            ok=1
            break
        fi
        if [ "$try" -lt "$TRIES" ]; then
            wait_for=$((BACKOFF * try))
            echo "[$n/$total] $id refuse, nouvel essai dans ${wait_for}s" >> "$LOG"
            sleep "$wait_for"
        fi
    done
    if [ "$ok" -eq 0 ]; then
        echo "[$n/$total] $id ECHEC apres $TRIES essai(s)" >> "$LOG"
        continue
    fi

    got=""
    for f in "$OUT"/*"$id".mp4 "$OUT"/*"$id".mkv "$OUT"/*"$id".webm; do
        if [ -f "$f" ]; then
            got="$f"
            break
        fi
    done
    if [ -z "$got" ]; then
        echo "[$n/$total] $id fichier introuvable apres telechargement" >> "$LOG"
        continue
    fi
    size=$(du -m "$got" | cut -f1)
    if push "$got"; then
        echo "[$n/$total] $id pousse (${size} Mo)" >> "$LOG"
    else
        echo "[$n/$total] $id ECHEC upload" >> "$LOG"
    fi
    sleep "$PAUSE"
done 3< "$IDS"

echo "TERMINE" >> "$LOG"
ssh -n -o BatchMode=yes -i "$KEY" "$HOST" 'ls -1 ~/videos | wc -l; du -sh ~/videos' >> "$LOG" 2>&1
