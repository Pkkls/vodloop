#!/bin/bash
# The one ffmpeg that talks to Kick. Nothing restarts it but watch.py on a
# pusher proven mute, and ceiling.py when the wire has to change height.
#
# What a restart costs, measured on 2026-09-22 on one six second restart: Kick
# kept the session (start_time unchanged through it) and still refixed its
# ladder on the new source, 720p60 replacing 1080p60 in the master playlist.
# So a short restart is cheaper than this comment used to claim. It is one
# measurement of one short gap: what a long one does was never measured, and
# the belief it replaces was that any restart ends the live, closes the VOD
# and drops every viewer. Restart it for a reason, not because it is cheap.
#
# The placeholder writer holds the FIFO open between chunks, otherwise ffmpeg
# reads EOF when one chunk ends and exits.
set -u
: "${CHAN_ROOT:?CHAN_ROOT manquant}"
FIFO="$CHAN_ROOT/pipe"
# the last line wins, as chan.load_env does: a setting is changed by appending
# an override, which every Python component reads correctly and which this read
# would otherwise turn into a two line variable and an ingest URL that resolves
# to nothing. Three keys were in exactly that state on 2026-09-22.
INGEST=$(sed -n 's/^KICK_INGEST=//p' "$CHAN_ROOT/channel.env" | tail -n 1)
KEY=$(sed -n 's/^KICK_STREAM_KEY=//p' "$CHAN_ROOT/channel.env" | tail -n 1)
[ -n "$INGEST" ] && [ -n "$KEY" ] || { echo "KICK_INGEST ou KICK_STREAM_KEY absent" >&2; exit 1; }

[ -p "$FIFO" ] || { rm -f "$FIFO"; mkfifo "$FIFO"; }
sleep infinity > "$FIFO" &

exec ffmpeg -hide_banner -loglevel warning \
  -re -i "$FIFO" -c copy \
  -f flv "$INGEST/$KEY"
