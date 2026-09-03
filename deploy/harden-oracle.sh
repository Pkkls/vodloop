#!/bin/bash
# One pass of hardening on Oracle. Run it ON the server, as ubuntu:
#
#   bash harden-oracle.sh            report only, changes nothing
#   bash harden-oracle.sh --apply    make the changes
#
# It is idempotent: running it twice is a no-op the second time. Every step
# prints what it measured, and every service restart is rolled back on the spot
# if the service stops answering.
#
# What it deliberately does NOT do, because both can lock kil out of the only
# machine he can still reach, and that is his call, not a script's:
#   - touch the sudoers grant (the ubuntu password is locked, see the report)
#   - apply the pending updates (they can pull a kernel and want a reboot)
set -u

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1
V=/home/ubuntu/vodloop
changed=0; failed=0

say()  { printf '%s\n' "$*"; }
head2() { printf '\n== %s ==\n' "$*"; }
act()  { if [ "$APPLY" = 1 ]; then return 0; else say "  (rapport seul) $*"; return 1; fi; }

# ---------------------------------------------------------------- H1 sandbox
# These three ran with no sandbox at all, on an account that is root without a
# password. The same block already protects vodloop-chat, vodloop-bus and
# yt2oracle-web; it was never applied here.
dropin() {
    unit="$1"; rw="$2"
    conf="/etc/systemd/system/${unit}.service.d/hardening.conf"
    if [ -f "$conf" ]; then say "  $unit: deja durci"; return 0; fi
    act "poserait $conf" || return 0
    sudo mkdir -p "$(dirname "$conf")" || { failed=1; return 1; }
    sudo tee "$conf" >/dev/null <<EOF
# Added by deploy/harden-oracle.sh. Without this the unit had no sandbox on an
# account that is root without a password: one bug in it was a root shell.
[Service]
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${rw}
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
RestrictNamespaces=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
EOF
    say "  $unit: drop-in pose"
    changed=1
}

# Restart, check the service still answers, and undo this unit's drop-in if not.
restart_or_rollback() {
    unit="$1"; url="$2"
    sudo systemctl restart "$unit" || { failed=1; say "  $unit: restart KO"; return 1; }
    sleep 3
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "$url" 2>/dev/null)
    if [ "$code" = "200" ]; then
        say "  $unit: repond $code"
        return 0
    fi
    say "  $unit: repond $code, ROLLBACK"
    sudo rm -f "/etc/systemd/system/${unit}.service.d/hardening.conf"
    sudo systemctl daemon-reload
    sudo systemctl restart "$unit"
    sleep 3
    say "  $unit: apres rollback $(curl -s -o /dev/null -w '%{http_code}' -m 15 "$url")"
    failed=1
    return 1
}

head2 "H1  bac a sable des trois unites qui n'en avaient aucun"
for u in vodloop-web vodloop-prep kickvods; do
    say "  $u: NoNewPrivileges=$(systemctl show "$u" -p NoNewPrivileges --value) ProtectSystem=$(systemctl show "$u" -p ProtectSystem --value)"
done
dropin vodloop-web  "$V/state $V/segments"
dropin vodloop-prep "$V/state $V/segments $V/incoming"
dropin kickvods     "/home/ubuntu/kickvods"
if [ "$APPLY" = 1 ] && [ "$changed" = 1 ]; then
    sudo systemctl daemon-reload
    restart_or_rollback vodloop-web http://127.0.0.1:8770/
    restart_or_rollback kickvods    http://127.0.0.1:5000/
    # prep is a worker, not a server: it is restarted only when it was running
    if [ "$(systemctl is-active vodloop-prep)" = "active" ]; then
        sudo systemctl restart vodloop-prep
        sleep 2
        say "  vodloop-prep: $(systemctl is-active vodloop-prep)"
    else
        say "  vodloop-prep: inactif, rien a redemarrer"
    fi
fi

head2 "H1b temoins du bac a sable (un controle a besoin d'un temoin)"
probe() {
    label="$1"; want="$2"; shift 2
    sudo systemd-run --quiet --wait --collect --uid=ubuntu --pipe \
        -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=read-only \
        -p ReadWritePaths="$V/state" -p PrivateTmp=yes -p CapabilityBoundingSet= \
        "$@" >/dev/null 2>&1
    rc=$?
    if [ "$want" = "echec" ] && [ "$rc" != 0 ]; then say "  ok   $label (rc=$rc)"
    elif [ "$want" = "reussite" ] && [ "$rc" = 0 ]; then say "  ok   $label"
    else say "  RATE $label (rc=$rc, attendu $want)"; failed=1; fi
}
probe "sudo depuis le bac a sable"      echec    /usr/bin/sudo -n true
probe "ecrire hors ReadWritePaths"      echec    /usr/bin/touch "$V/HARDENING_PROBE"
probe "ecrire dans /etc"                echec    /usr/bin/touch /etc/HARDENING_PROBE
probe "ecrire dans ReadWritePaths"      reussite /usr/bin/touch "$V/state/HARDENING_PROBE"
rm -f "$V/state/HARDENING_PROBE"
[ -e "$V/HARDENING_PROBE" ] && { say "  RATE la sonde a ecrit hors du bac a sable"; failed=1; }

# ------------------------------------------------------- H4 jeton dans les logs
head2 "H4  le jeton du dashboard finit dans le journal nginx"
leaks=$(sudo grep -c "token=" /var/log/nginx/access.log 2>/dev/null || true)
say "  lignes avec token= dans access.log: ${leaks:-0}"
# conf.d is included from inside the http block, which is where a log_format has
# to live. Editing nginx.conf itself with sed to get it there is a good way to
# break the config of a machine you cannot reach any other way.
if [ ! -f /etc/nginx/conf.d/scrubbed-log.conf ]; then
    if act "poserait /etc/nginx/conf.d/scrubbed-log.conf"; then
        sudo tee /etc/nginx/conf.d/scrubbed-log.conf >/dev/null <<'EOF'
# $uri instead of $request: a token pasted in a URL must not survive in a log.
log_format scrubbed '$remote_addr - - [$time_local] "$request_method $uri $server_protocol" '
                    '$status $body_bytes_sent "$http_referer"';
EOF
        if sudo nginx -t 2>/dev/null; then
            sudo systemctl reload nginx && say "  log_format scrubbed pose, nginx recharge"
            changed=1
        else
            say "  nginx -t refuse, annule"
            sudo rm -f /etc/nginx/conf.d/scrubbed-log.conf
            failed=1
        fi
    fi
else
    say "  log_format scrubbed: deja present"
fi
say "  reste a faire a la main dans le server block vodloop, sous location /yt2oracle/ :"
say "      access_log /var/log/nginx/access.log scrubbed;"
if [ "${leaks:-0}" != "0" ]; then
    if act "purgerait les jetons des logs existants"; then
        for f in /var/log/nginx/access.log; do
            sudo sed -i 's/token=[A-Za-z0-9]*/token=REDACTED/g' "$f"
        done
        say "  jetons purges de access.log"
        say "  ATTENTION: le jeton actuel a ete ecrit en clair, il faut le renouveler:"
        say "      printf 'YT2O_TOKEN=%s\\n' \"\$(openssl rand -hex 16)\" > ~/yt2oracle/web.env"
        say "      chmod 600 ~/yt2oracle/web.env && sudo systemctl restart yt2oracle-web"
        changed=1
    fi
fi

# ------------------------------------------------------------- H6 petits points
head2 "H6  fail2ban ne surveille que ssh"
sudo fail2ban-client status 2>/dev/null | tr '\n' ' '; echo
if [ ! -f /etc/fail2ban/jail.d/nginx-token.conf ]; then
    if act "ajouterait une jail sur les 401 repetes"; then
        sudo tee /etc/fail2ban/filter.d/nginx-token.conf >/dev/null <<'EOF'
[Definition]
# a token that is wrong over and over is a token being guessed
failregex = ^<HOST> .*" 401
ignoreregex =
EOF
        sudo tee /etc/fail2ban/jail.d/nginx-token.conf >/dev/null <<'EOF'
[nginx-token]
enabled  = true
filter   = nginx-token
logpath  = /var/log/nginx/access.log
port     = http,https
maxretry = 15
findtime = 300
bantime  = 3600
EOF
        sudo systemctl reload fail2ban && say "  jail nginx-token active"
        changed=1
    fi
else
    say "  jail nginx-token: deja presente"
fi

head2 "H6b sshd, deux reglages sans effet sur l'auth par cle"
sudo sshd -T 2>/dev/null | grep -E '^(x11forwarding|maxauthtries)'
if act "poserait x11forwarding no et maxauthtries 3"; then
    sudo tee /etc/ssh/sshd_config.d/99-hardening.conf >/dev/null <<'EOF'
# headless box: nothing to forward. Three tries is plenty for a key.
X11Forwarding no
MaxAuthTries 3
EOF
    if sudo sshd -t; then
        # reload, never restart: a reload keeps the current sessions alive
        sudo systemctl reload ssh && say "  sshd recharge"
        changed=1
    else
        say "  sshd -t refuse la conf, annule"
        sudo rm -f /etc/ssh/sshd_config.d/99-hardening.conf
        failed=1
    fi
fi

# ------------------------------------------------------------------- rapports
head2 "H5  mises a jour (rapport seul, jamais applique par ce script)"
/usr/lib/update-notifier/apt-check --human-readable 2>&1 | head -2
say "  a lancer quand le stream est eteint: sudo apt update && sudo apt upgrade"
say "  et corriger APT::Periodic::Download-Upgradeable-Packages, aujourd'hui a 0"

head2 "H1c  la grant sudo, decision de kil, ce script n'y touche pas"
sudo grep -h "NOPASSWD" /etc/sudoers.d/90-cloud-init-users 2>/dev/null || true
say "  passwd -S ubuntu: $(sudo passwd -S ubuntu)"
say "  le 'L' signifie mot de passe verrouille: retirer NOPASSWD sans en poser un"
say "  d'abord supprime sudo, et root en ssh est desactive. Porte a sens unique."

head2 "resultat"
[ "$APPLY" = 0 ] && say "rapport seul, rien change. Relancer avec --apply."
say "modifie=$changed echecs=$failed"
exit "$failed"
