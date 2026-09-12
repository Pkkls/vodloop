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

# One exec of this script is one RTMP session, and Kick reads the resolution out
# of the sequence header when a session opens and advertises it for the whole
# live. So the first picture this ffmpeg carries names the channel until it dies,
# and left to chance that is whatever chunk sat at the head of the queue. Saying
# here that a new session is starting is what lets the feeder put the 1080p60
# standby clip in front of it instead. The pid is in the line because two starts
# can share a second and a repeated line reads as the same session.
mkdir -p "$ROOT/state" 2>/dev/null
printf '%s %s\n' "$(date +%s)" "$$" > "$ROOT/state/push_session" 2>/dev/null || true

exec ffmpeg -hide_banner -loglevel warning \
  -re -i "$FIFO" -c copy \
  -f flv "$KICK_INGEST/$KICK_STREAM_KEY"
