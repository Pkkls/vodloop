#!/bin/bash
# The order of the units is the whole point of vodloopctl, so it is what gets
# tested. systemctl is replaced by a stub that records its arguments, so this
# runs anywhere and touches nothing.
#
#   bash tests/test_vodloopctl.sh
set -u

CTL="$(cd "$(dirname "$0")/.." && pwd)/deploy/vodloopctl"
STUB=$(mktemp -d)
LOG="$STUB/calls"

cat > "$STUB/systemctl" <<EOF
#!/bin/sh
echo "\$*" >> "$LOG"
# the status verb reads this back, so give it something shaped like an answer
[ "\$1" = "is-active" ] && echo active
exit 0
EOF
chmod +x "$STUB/systemctl"
export PATH="$STUB:$PATH"

pass=0; fail=0
check() {
    label="$1"; want="$2"; got="$3"
    if [ "$got" = "$want" ]; then pass=$((pass + 1)); echo "ok   $label"
    else fail=$((fail + 1)); echo "RATE $label"; echo "     attendu: $want"; echo "     obtenu : $got"; fi
}

run() { : > "$LOG"; sh "$CTL" "$@" >/dev/null 2>&1; echo "rc=$?"; }
calls() { tr '\n' '|' < "$LOG"; }

rc=$(run go-live)
check "go-live rc" "rc=0" "$rc"
check "go-live: le pusher demarre en premier, le bot suit" \
    "start vodloop-push|start vodloop-feed|start vodloop-prep|start vodloop-chat|" "$(calls)"

rc=$(run stop)
check "stop rc" "rc=0" "$rc"
check "stop: le chat en premier, le pusher en dernier" \
    "stop vodloop-chat|stop vodloop-feed|stop vodloop-prep|stop vodloop-push|" "$(calls)"

rc=$(run pause)
check "pause rc" "rc=0" "$rc"
check "pause ne touche QUE le feeder, jamais le pusher" \
    "stop vodloop-feed|" "$(calls)"

rc=$(run resume)
check "resume ne redemarre que le feeder" "start vodloop-feed|" "$(calls)"

rc=$(run bot-stop)
check "bot-stop ne touche que le chat" "stop vodloop-chat|" "$(calls)"

# no verb anywhere may restart the pusher: that is what ends the live
: > "$LOG"
for verb in go-live stop pause resume bot-start bot-stop status; do
    sh "$CTL" "$verb" >/dev/null 2>&1
done
check "aucun verbe ne redemarre le pusher" "" "$(grep -c 'restart' "$LOG" | grep -v '^0$')"

check "verbe inconnu refuse"        "rc=2" "$(run definitely-not-a-verb)"
check "sans argument refuse"        "rc=2" "$(run)"
check "deux arguments refuses"      "rc=2" "$(run stop 'ohno; rm -rf /')"
check "une injection reste un mot"  "rc=2" "$(run 'stop; touch /tmp/pwned')"
[ -e /tmp/pwned ] && { echo "RATE l'injection a produit un effet"; fail=$((fail + 1)); }

rm -rf "$STUB"
echo
echo "$pass/$((pass + fail)) passent"
[ "$fail" = 0 ] || exit 1
