#!/bin/bash
# install.sh <channel> - directories, units, cron block and standby clip for one
# channel. Safe to run again: it only creates what is missing and rewrites the
# channel's own cron block. The channel is described by channel.env alone.
set -euo pipefail
NAME="${1:?usage: install.sh <channel>}"
case "$NAME" in *[!a-z0-9-]* | "") echo "nom refuse: $NAME" >&2; exit 1 ;; esac
V2=/home/ubuntu/v2
ROOT=$V2/$NAME
BIN=$V2/bin
[ -f "$ROOT/channel.env" ] || { echo "$ROOT/channel.env absent" >&2; exit 1; }
chmod 600 "$ROOT/channel.env"
mkdir -p "$ROOT"/{queue,current,aired,chunks/.work,upload,state,log}
touch "$ROOT/sources.txt"

for role in push feed cut; do
  sudo install -m 644 "$BIN/systemd/vodloop-v2-$role@.service" /etc/systemd/system/
done
sudo systemctl daemon-reload

# The standby clip has the channel's own shape: a clip of another size is a
# resolution change on the wire at the worst moment (found at 1280x720 against a
# 1920x1080 channel on 2026-09-14). Encoded once, the only encode in v2.
maxh=$(sed -n 's/^MAXH=//p' "$ROOT/channel.env"); maxh=${maxh:-720}
case "$maxh" in 1080) size=1920x1080 ;; 480) size=854x480 ;; *) size=1280x720 ;; esac
if ! ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 \
     "$ROOT/filler.ts" 2>/dev/null | grep -qx "$size"; then
  ffmpeg -hide_banner -loglevel error \
    -f lavfi -t 20 -i "color=c=0x101014:s=$size:r=30,drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='vod loading...':fontcolor=white@0.82:fontsize=h/16:x=(w-text_w)/2:y=(h-text_h)/2" \
    -f lavfi -t 20 -i "anullsrc=channel_layout=stereo:sample_rate=44100" \
    -c:v libx264 -preset veryfast -crf 21 -pix_fmt yuv420p -g 60 -keyint_min 60 -sc_threshold 0 \
    -c:a aac -b:a 160k -ar 44100 -ac 2 -f mpegts -y "$ROOT/filler.ts.new"
  mv "$ROOT/filler.ts.new" "$ROOT/filler.ts"
  echo "clip d'attente: $size"
fi

block="# v2:$NAME debut
*/10 * * * * CHAN_ROOT=$ROOT /usr/bin/flock -n $ROOT/state/supply.lock /usr/bin/python3 $BIN/supply.py --apply >> $ROOT/log/supply.log 2>&1
*/5 * * * * CHAN_ROOT=$ROOT /usr/bin/flock -n $ROOT/state/watch.lock /usr/bin/python3 $BIN/watch.py --apply >> $ROOT/log/watch.log 2>&1
# v2:$NAME fin"
{ crontab -l 2>/dev/null | sed "/^# v2:$NAME debut\$/,/^# v2:$NAME fin\$/d"; echo "$block"; } | crontab -
echo "installe: $ROOT (demarrer: sudo systemctl enable --now vodloop-v2-cut@$NAME vodloop-v2-push@$NAME)"
