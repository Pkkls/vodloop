#!/bin/sh
# Generate the standby clip a channel shows when its chunk queue runs dry.
#
#   VODLOOP_ROOT=/path/to/channel bin/make-filler.sh
#   VODLOOP_ROOT=/path/to/channel bin/make-filler.sh "vod loading..."
#
# The feeder falls back to this clip whenever segments/ is empty, so it is what
# viewers see during a handover the queue could not cover. It used to be a flat
# dark field, which was indistinguishable from a dead stream: for viewers, and
# for whoever was diagnosing it, the only way to tell them apart was to measure
# the luma of a frame. Words remove that whole class of question.
#
# The clip MUST carry the same picture the channel sends. One found on
# 2026-09-14 was 1280x720 at 30 fps against a 1920x1080 target, left over from
# an older profile, and a clip of the wrong shape is a resolution change on the
# wire at the worst possible moment.
#
# Encoding settings come from common.ENCODE, the same ones every chunk gets, so
# this cannot drift from the channel by being edited here.
set -eu

TEXTE="${1:-${VODLOOP_FILLER_TEXT:-vod loading...}}"
RACINE="${VODLOOP_ROOT:-$HOME/vodloop}"
BIN="$(cd "$(dirname "$0")" && pwd)"
POLICE="${VODLOOP_FILLER_FONT:-/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf}"
SECONDES="${VODLOOP_FILLER_SECONDS:-20}"
CIBLE="$RACINE/filler.ts"

[ -d "$RACINE" ] || { echo "racine introuvable: $RACINE" >&2; exit 1; }
[ -f "$POLICE" ] || { echo "police introuvable: $POLICE" >&2; exit 1; }

# the channel's own picture and its own encode, read rather than restated
LARGEUR=$(VODLOOP_ROOT="$RACINE" python3 -c "import sys;sys.path.insert(0,'$BIN');import common;print(common.WIDTH)")
HAUTEUR=$(VODLOOP_ROOT="$RACINE" python3 -c "import sys;sys.path.insert(0,'$BIN');import common;print(common.HEIGHT)")
VODLOOP_ROOT="$RACINE" python3 -c "import sys;sys.path.insert(0,'$BIN');import common;print(chr(10).join(common.ENCODE))" > /tmp/encode.$$

echo "clip d attente: ${LARGEUR}x${HAUTEUR}, ${SECONDES}s, texte \"$TEXTE\""

# drawn on the lavfi source rather than through -vf, because ENCODE already
# carries its own filter chain and two of them cannot both be passed
FOND="color=c=0x101014:s=${LARGEUR}x${HAUTEUR}"
FOND="$FOND,drawtext=fontfile=$POLICE:text='$TEXTE'"
FOND="$FOND:fontcolor=white@0.82:fontsize=h/16"
FOND="$FOND:x=(w-text_w)/2:y=(h-text_h)/2"

TEMPO="$CIBLE.nouveau"
# shellcheck disable=SC2046
ffmpeg -hide_banner -loglevel error \
  -f lavfi -t "$SECONDES" -i "$FOND" \
  -f lavfi -t "$SECONDES" -i "anullsrc=channel_layout=stereo:sample_rate=44100" \
  $(tr '\n' ' ' < /tmp/encode.$$) \
  -f mpegts -y "$TEMPO"
rm -f /tmp/encode.$$

# Proven before it replaces anything. A flat clip has zero bright pixels; one
# carrying words has them, and they sit in the middle band and nowhere near the
# corners. Without this check a missing glyph would ship a blank card that looks
# exactly like the thing this script exists to replace.
CLAIRS=$(ffmpeg -v error -i "$TEMPO" -vf "select=eq(n\,30),crop=iw/2:ih/8:iw/4:ih/2-ih/16,format=gray" \
  -frames:v 1 -f rawvideo - 2>/dev/null | od -An -tu1 -v | tr ' ' '\n' | awk '$1>128' | wc -l)
COINS=$(ffmpeg -v error -i "$TEMPO" -vf "select=eq(n\,30),crop=iw/6:ih/6:0:0,format=gray" \
  -frames:v 1 -f rawvideo - 2>/dev/null | od -An -tu1 -v | tr ' ' '\n' | awk '$1>128' | wc -l)

echo "  pixels clairs au centre: $CLAIRS   dans un coin: $COINS"
if [ "$CLAIRS" -lt 100 ]; then
  rm -f "$TEMPO"
  echo "  REFUS: aucun texte visible, l ancien clip est conserve" >&2
  exit 1
fi
if [ "$COINS" -gt 0 ]; then
  rm -f "$TEMPO"
  echo "  REFUS: du clair hors du centre, le rendu n est pas celui attendu" >&2
  exit 1
fi

# replaced by rename, so a feeder already reading the old one keeps its file
mv "$TEMPO" "$CIBLE"
echo "  installe: $CIBLE"
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate \
  -of csv=p=0 "$CIBLE" | sed 's/^/  forme: /'
