#!/bin/bash
# The single ffmpeg that talks to Kick. It must never be restarted while live:
# any RTMP disconnect ends the stream, starts a new VOD and drops viewers.
#
# The placeholder writer below is what makes that possible. Without a writer
# permanently holding the FIFO open, ffmpeg sees EOF the moment one chunk ends
# and exits before the next one starts.
set -u

ROOT="${VODLOOP_ROOT:-$HOME/vodloop}"
FIFO="$ROOT/pipe"

# shellcheck disable=SC1091
. "$ROOT/.env"

[ -p "$FIFO" ] || { rm -f "$FIFO"; mkfifo "$FIFO"; }

# hold the FIFO open forever, so it never runs out of writers between chunks
sleep infinity > "$FIFO" &
PLACEHOLDER=$!
trap 'kill $PLACEHOLDER 2>/dev/null' EXIT

exec ffmpeg -hide_banner -loglevel warning \
  -re -i "$FIFO" -c copy \
  -f flv "$KICK_INGEST/$KICK_STREAM_KEY"
