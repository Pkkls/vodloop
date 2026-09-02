#!/bin/sh
# Runs on the Claw board, not on the streaming host. A probe that shares a
# machine with the thing it watches goes down with it, which is the whole point
# of putting it here.
#
# Kick keeps reporting a channel as live for several seconds after the push
# actually stops, so a single reading is not enough: two consecutive misses are
# required before raising anything.
set -u

SLUG="${VODLOOP_SLUG:?set VODLOOP_SLUG}"
TG_TOKEN="${TG_TOKEN:?set TG_TOKEN}"
TG_CHAT="${TG_CHAT:?set TG_CHAT}"
INTERVAL="${INTERVAL:-120}"

misses=0
alerted=0

notify() {
  curl -s -m 20 -o /dev/null \
    --data-urlencode "text=$1" \
    --data "chat_id=$TG_CHAT" \
    "https://api.telegram.org/bot$TG_TOKEN/sendMessage"
}

while :; do
  # a neutral user agent: Kick answers 403 to an empty one and to a spoofed browser
  if curl -s -m 20 -A "vodloop-watchdog/0.1" \
       "https://kick.com/api/v2/channels/$SLUG" -o /tmp/vw.json 2>/dev/null &&
     python3 -c 'import json,sys; sys.exit(0 if json.load(open("/tmp/vw.json")).get("livestream") else 1)' 2>/dev/null
  then
    if [ "$alerted" -eq 1 ]; then
      notify "vodloop: $SLUG est revenu en direct"
    fi
    misses=0
    alerted=0
  else
    misses=$((misses + 1))
    if [ "$misses" -ge 2 ] && [ "$alerted" -eq 0 ]; then
      notify "vodloop: $SLUG n'est plus en direct (2 mesures consecutives)"
      alerted=1
    fi
  fi
  rm -f /tmp/vw.json
  sleep "$INTERVAL"
done
