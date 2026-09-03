#!/bin/sh
# Bring every library file into the exact shape a chunk must have, once.
#
# Runs on the streaming host. A source already in that shape is remuxed at play
# time and costs almost nothing; anything else is re-encoded on every pass, and
# this box encodes at 0.77x realtime, so one odd file drains the whole backlog
# and the channel falls back to the standby clip.
#
# The target and the encoder settings are read from common.py rather than
# repeated here: two copies of that list would drift, and a chunk that differs
# by one setting breaks concatenation at the junction.
#
#     sh tools/normalise-library.sh            report what would be touched
#     sh tools/normalise-library.sh --apply    do it
set -u
BIN="${VODLOOP_BIN:-/home/ubuntu/vodloop/bin}"
LIB="${VODLOOP_LIBRARY:-/home/ubuntu/videos}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

WANT=$(python3 -c "import sys; sys.path.insert(0, '$BIN'); import common; print(f'h264,{common.WIDTH},{common.HEIGHT},{common.FPS}/1')")
ENCODE=$(python3 -c "import sys; sys.path.insert(0, '$BIN'); import common; print(' '.join(common.ENCODE))")
[ -n "$WANT" ] || { echo "impossible de lire le format cible dans common.py" >&2; exit 1; }
echo "format cible: $WANT"

done_n=0
skip_n=0
fail_n=0
for f in "$LIB"/*; do
    [ -f "$f" ] || continue
    case "$f" in *.mp4|*.mkv|*.mov|*.webm|*.ts|*.m4v|*.avi) ;; *) continue;; esac
    got=$(ffprobe -v error -select_streams v:0 \
          -show_entries stream=codec_name,width,height,r_frame_rate \
          -of csv=p=0 "$f" | head -1)
    if [ "$got" = "$WANT" ]; then
        skip_n=$((skip_n + 1))
        continue
    fi
    if [ "$APPLY" -eq 0 ]; then
        echo "a normaliser: $got  $(basename "$f")"
        done_n=$((done_n + 1))
        continue
    fi
    echo "normalisation ($got): $(basename "$f")"
    # nice 19: the live pipeline always wins, this can take as long as it likes
    # shellcheck disable=SC2086
    if nice -n 19 ffmpeg -v error -y -i "$f" $ENCODE -f mp4 "$f.norm.mp4"; then
        # rename is atomic, and a reader already holding the old file keeps it
        mv "$f.norm.mp4" "${f%.*}.mp4"
        [ "${f%.*}.mp4" = "$f" ] || rm -f "$f"
        done_n=$((done_n + 1))
        echo "  fait"
    else
        rm -f "$f.norm.mp4"
        fail_n=$((fail_n + 1))
        echo "  ECHEC, fichier laisse tel quel"
    fi
done

if [ "$APPLY" -eq 0 ]; then
    echo "$done_n a normaliser, $skip_n deja conformes. Relancer avec --apply."
else
    echo "$done_n normalise(s), $skip_n deja conformes, $fail_n echec(s)."
fi
