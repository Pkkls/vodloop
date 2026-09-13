#!/bin/sh
# Four mechanisms the board's scripts rest on, checked in the shell that runs
# them. Run on the board: sh test_claw_scripts.sh
#
# None of these can be checked on a development machine: bash and busybox ash
# disagree about exactly the things being measured here, and the awk is
# busybox's. Every check has a control that must FAIL the same test, because a
# filter that accepts everything and a filter that works look identical when
# only the good case is tried.
set -u

fail=0
ok() { echo "  PASS  $1"; }
no() { echo "  FAIL  $1  ${2:-}"; fail=$((fail + 1)); }
is() { [ "$2" = "$3" ] && ok "$1" || no "$1" "attendu [$3] obtenu [$2]"; }

echo "1. l'etoile de --download-sections survit au shell"
work=$(mktemp -d)
cd "$work" || exit 1
# a file the star would match, so the control below has something to expand to
: > "x0-7200"
PART=p01of08
RANGE=0-7200
SECTION="*$RANGE"
got=$(for a in ${SECTION:+--download-sections} ${SECTION:+"$SECTION"}; do printf '[%s]' "$a"; done)
is "l'argument arrive entier a yt-dlp" "$got" "[--download-sections][*0-7200]"
# the control: the same value unquoted IS eaten by the shell, which is the bug
loose="--download-sections *$RANGE"
got=$(for a in $loose; do printf '[%s]' "$a"; done)
is "temoin: non protegee, l'etoile devient un nom de fichier" "$got" \
   "[--download-sections][x0-7200]"
SECTION=""
got=$(for a in ${SECTION:+--download-sections} ${SECTION:+"$SECTION"}; do printf '[%s]' "$a"; done)
is "sans morceau, aucun argument n'est ajoute" "$got" ""

echo "2. ce que inbox-pull accepte d'une inbox ecrite ailleurs"
clean() {
  awk '
    {
      dest = $1; url = ""; part = ""; range = ""; h = ""
      if (dest !~ /^videos(-[a-z0-9]+)?$/) next
      for (i = 2; i <= NF; i++) {
        t = $i
        if (url == "" && (t ~ /^https:\/\/www\.youtube\.com\/watch\?v=[A-Za-z0-9_-]+$/ ||
                          t ~ /^https:\/\/youtu\.be\/[A-Za-z0-9_-]+$/)) url = t
        else if (t ~ /^part=p[0-9]+of[0-9]+$/) part = t
        else if (t ~ /^range=[0-9]+-[0-9]+$/) range = t
        else if (t ~ /^h=[0-9]+$/) h = t
      }
      if (url == "") next
      line = url
      if (part != "") line = line " " part
      if (range != "") line = line " " range
      if (h != "") line = line " " h
      print line " dest=" dest
    }'
}
got=$(echo 'videos-second https://www.youtube.com/watch?v=abcDEF12345 part=p02of06 range=7200-14400 h=720' | clean)
is "une ligne de morceau passe entiere" "$got" \
   "https://www.youtube.com/watch?v=abcDEF12345 part=p02of06 range=7200-14400 h=720 dest=videos-second"
got=$(echo 'videos https://www.youtube.com/watch?v=abcDEF12345' | clean)
is "une URL nue reste une URL nue" "$got" \
   "https://www.youtube.com/watch?v=abcDEF12345 dest=videos"
got=$(echo 'videos https://www.youtube.com/watch?v=abcDEF12345 --exec=rm_-rf_/ ;reboot' | clean)
is "tout ce qui n'est pas un des quatre jetons tombe" "$got" \
   "https://www.youtube.com/watch?v=abcDEF12345 dest=videos"
got=$(echo 'videos http://evil.example.com/x part=p01of02' | clean)
is "une ligne sans URL YouTube ne produit rien" "$got" ""
got=$(echo '/etc/passwd https://www.youtube.com/watch?v=abcDEF12345' | clean)
is "une destination qui n'est pas une bibliotheque ne produit rien" "$got" ""

echo "3. la file alterne entre les chaines"
QUEUE="$work/urls.txt"
cat > "$QUEUE" <<EOF
https://www.youtube.com/watch?v=aaaaaaaaaaa dest=videos
https://www.youtube.com/watch?v=bbbbbbbbbbb dest=videos
https://www.youtube.com/watch?v=ccccccccccc dest=videos-second
EOF
pick() {
  last=$1
  useful=$(grep -vE '^[[:space:]]*(#|$)' "$QUEUE")
  line=$(printf '%s\n' "$useful" | awk -v last="$last" '
    {
      d = "videos"
      for (i = 1; i <= NF; i++) if ($i ~ /^dest=/) d = substr($i, 6)
      if (d != last) { print; exit }
    }')
  [ -n "$line" ] || line=$(printf '%s\n' "$useful" | head -1)
  case "$line" in *" dest="*) printf '%s' "${line##* dest=}" ;; *) printf 'videos' ;; esac
}
is "sans tour precedent, c'est la premiere ligne" "$(pick '')" "videos"
is "apres la premiere chaine, c'est l'autre" "$(pick videos)" "videos-second"
is "et apres l'autre, on revient" "$(pick videos-second)" "videos"

# Une ligne d'avant les destinations n'a aucun jeton dest= et appartient a la
# premiere chaine. Cherchee par son texte elle ne correspond a rien, donc elle
# n'etait jamais ecartee et la file la reprenait sans fin: le 2026-09-13 trois
# telechargements de la premiere chaine se sont enchaines pendant que les huit
# lignes de l'autre attendaient.
cat > "$QUEUE" <<EOF
https://www.youtube.com/watch?v=aaaaaaaaaaa
https://www.youtube.com/watch?v=bbbbbbbbbbb
https://www.youtube.com/watch?v=ccccccccccc dest=videos-second
EOF
is "une ligne sans destination compte pour la premiere chaine" \
   "$(pick videos)" "videos-second"
is "temoin: et c'est bien elle qu'on sert quand c'est son tour" \
   "$(pick videos-second)" "videos"
# the control: with only one channel queued there is nothing to alternate with,
# and the file has to keep draining rather than stop
cat > "$QUEUE" <<EOF
https://www.youtube.com/watch?v=aaaaaaaaaaa dest=videos
EOF
is "temoin: une seule chaine sert quand meme son tour" "$(pick videos)" "videos"

echo "4. le compte par bibliotheque publie par status-push"
cat > "$QUEUE" <<EOF
https://www.youtube.com/watch?v=aaaaaaaaaaa dest=videos
https://www.youtube.com/watch?v=bbbbbbbbbbb dest=videos-second
https://www.youtube.com/watch?v=ccccccccccc dest=videos-second

https://www.youtube.com/watch?v=ddddddddddd
EOF
counts() {
  awk '
    NF {
      d = "videos"
      for (i = 1; i <= NF; i++) if ($i ~ /^dest=videos(-[a-z0-9]+)?$/) d = substr($i, 6)
      c[d]++
    }
    END {
      s = ""
      for (k in c) { if (s != "") s = s ","; s = s "\"" k "\":" c[k] }
      print "{" s "}"
    }' "$1" 2>/dev/null
}
got=$(counts "$QUEUE")
# the order of an awk array is not defined, so both spellings are the same answer
case "$got" in
  '{"videos":2,"videos-second":2}' | '{"videos-second":2,"videos":2}')
    ok "chaque bibliotheque compte ses propres lignes  $got" ;;
  *) no "chaque bibliotheque compte ses propres lignes" "$got" ;;
esac
# busybox awk n'execute PAS son bloc END quand le fichier n'existe pas: il sort
# sur l'erreur et ne rend rien du tout. C'est pourquoi status-push ne se repose
# pas sur le "{}" de l'awk mais retombe dessus lui-meme, et c'est cette
# retombee qui est mesuree ici. Sans elle le JSON publie serait "queues":
# suivi de rien, que le collector lit comme une file inconnue.
got=$(counts "$work/pas-de-fichier")
is "temoin: seul, l'awk ne rend rien sur un fichier absent" "$got" ""
[ -n "$got" ] || got='{}'
is "la retombee de status-push publie du JSON valide quand meme" "$got" "{}"

echo "5. ce que yt2oracle refuse avant de s'en servir"
# Ces valeurs viennent d'une inbox ecrite sur Oracle et finissent dans une ligne
# de commande yt-dlp et dans un chemin de destination sur Oracle. inbox-pull les
# filtre deja; ceci est la deuxieme serrure, a l'endroit ou elles servent.
guard() {
  DEST_DIR=$1; PART=$2; RANGE=$3; MAXH=$4
  suffix=${DEST_DIR#/home/ubuntu/videos}
  case "$suffix" in
    "") ;;
    -*) case "$suffix" in *[!a-z0-9-]*) echo "DEST_DIR"; return ;; esac ;;
    *) echo "DEST_DIR"; return ;;
  esac
  case "$PART" in
    "") ;;
    p[0-9]*of[0-9]*) case "$PART" in *[!0-9pof]*) echo "PART"; return ;; esac ;;
    *) echo "PART"; return ;;
  esac
  case "$RANGE" in
    "") ;;
    [0-9]*-[0-9]*) case "$RANGE" in *[!0-9-]*) echo "RANGE"; return ;; esac ;;
    *) echo "RANGE"; return ;;
  esac
  case "$MAXH" in "" | 1080 | 720 | 480 | 360) ;; *) echo "MAXH"; return ;; esac
  [ -z "$PART" ] || [ -n "$RANGE" ] || { echo "PART-SANS-RANGE"; return; }
  echo "ok"
}
is "la bibliotheque d'une deuxieme chaine passe" \
   "$(guard /home/ubuntu/videos-nanatty247 p01of08 0-7200 720)" "ok"
is "celle d'aujourd'hui passe aussi, sans rien d'autre" \
   "$(guard /home/ubuntu/videos '' '' '')" "ok"
is "une destination ailleurs sur le disque est refusee" \
   "$(guard /etc '' '' '')" "DEST_DIR"
is "et une qui remonte depuis une bibliotheque aussi" \
   "$(guard /home/ubuntu/videos-a/../../../etc '' '' '')" "DEST_DIR"
is "un morceau qui n'est pas un morceau est refuse" \
   "$(guard /home/ubuntu/videos 'p1;reboot' 0-10 '')" "PART"
is "une tranche qui n'est pas une tranche est refusee" \
   "$(guard /home/ubuntu/videos p01of08 '0-10 --exec' '')" "RANGE"
is "une hauteur hors de l'echelle est refusee" \
   "$(guard /home/ubuntu/videos '' '' 4320)" "MAXH"
# un morceau sans tranche telechargerait la video entiere sous le nom d'un
# morceau, et les sept autres viendraient se poser a cote
is "un morceau sans tranche est refuse" \
   "$(guard /home/ubuntu/videos p01of08 '' '')" "PART-SANS-RANGE"

cd / && rm -rf "$work"
echo
[ "$fail" -eq 0 ] && echo "all passed" || { echo "$fail failed"; exit 1; }
