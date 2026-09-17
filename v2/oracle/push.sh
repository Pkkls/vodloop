#!/bin/bash
# The one ffmpeg that talks to Kick. Restarting it ends the live, closes the
# VOD and drops every viewer, so nothing restarts it but watch.py on a pusher
# proven mute. The placeholder writer holds the FIFO open between chunks,
# otherwise ffmpeg reads EOF when one chunk ends and exits.
set -u
: "${CHAN_ROOT:?CHAN_ROOT manquant}"
FIFO="$CHAN_ROOT/pipe"
INGEST=$(sed -n 's/^KICK_INGEST=//p' "$CHAN_ROOT/channel.env")
KEY=$(sed -n 's/^KICK_STREAM_KEY=//p' "$CHAN_ROOT/channel.env")
[ -n "$INGEST" ] && [ -n "$KEY" ] || { echo "KICK_INGEST ou KICK_STREAM_KEY absent" >&2; exit 1; }

[ -p "$FIFO" ] || { rm -f "$FIFO"; mkfifo "$FIFO"; }
sleep infinity > "$FIFO" &

exec ffmpeg -hide_banner -loglevel warning \
  -re -i "$FIFO" -c copy \
  -f flv "$INGEST/$KEY"
