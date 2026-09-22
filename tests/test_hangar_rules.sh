#!/bin/sh
# The board's pacing rule, checked in the shell that runs it: sh test_hangar_rules.sh
#
# Written after 2026-09-22, where a thin shelf made the board retry into a
# YouTube wall every twenty-five minutes until the throttle hit its floor and
# stayed there for six hours. The rule was eight lines inside fill(), which is
# to say eight lines nothing could call and nothing could check.
#
# Run it on the board. busybox ash and bash disagree about enough that a pass
# here means nothing about there.
set -u

fail=0
ok() { echo "  PASS  $1"; }
no() { echo "  FAIL  $1  ${2:-}"; fail=$((fail + 1)); }
is() { [ "$2" = "$3" ] && ok "$1" || no "$1" "attendu [$3] obtenu [$2]"; }

HANGAR=${1:-/usr/bin/hangar}
[ -r "$HANGAR" ] || { echo "introuvable: $HANGAR"; exit 1; }
HANGAR_SOURCED=1 . "$HANGAR"

echo "le rythme suit le fil quand la source repond"
is "sous quatre heures, on ne patiente pas"        "$(gap_for 2 ok)"  15
is "entre quatre et huit, on patiente un peu"      "$(gap_for 6 ok)"  35
is "au-dela de huit, le rythme de croisiere"       "$(gap_for 10 ok)" "$GAP_MIN"
is "avec seize heures en main, le double"          "$(gap_for 20 ok)" "$((GAP_MIN * 2))"

echo "un echec de notre cote coute une attente, bornee par ce qui reste a diffuser"
is "reserve confortable: l attente complete"       "$(gap_for 10 fail)" "$FAIL_GAP_MIN"
is "reserve mince: dix minutes de plus, pas trois heures" "$(gap_for 6 fail)" 45
is "controle: la meme reserve sans echec"          "$(gap_for 6 ok)" 35

echo "un refus de la source n est pas un echec de notre cote"
is "mur avec une reserve mince: attente complete"  "$(gap_for 2 wall)" "$FAIL_GAP_MIN"
is "mur avec une reserve confortable: la meme"     "$(gap_for 20 wall)" "$FAIL_GAP_MIN"
# the control is the bug itself: before 2026-09-22 a wall was written down as
# any other failure, so this is what the board did with two hours of shelf
is "temoin: en le prenant pour un echec ordinaire, on retapait dans le mur" \
   "$(gap_for 2 fail)" 25

echo "le tirage vise court quand la chaine manque de largeur"
is "file vide: la question n est plus laquelle merite un tour"    "$(shortest_first 0 4)" "file vide"
is "deux enregistrements inedits: le chat n a nulle part ou sauter"    "$(shortest_first 2 2)" "2 inedite(s) seulement"
is "controle: trois, c est assez, on tire large" "$(shortest_first 2 3)" ""
is "controle: un far side trop vieux pour envoyer streams ne declenche rien"    "$(shortest_first 2 0)" ""

echo "on n expedie pas contre une offre qui ne sait pas ce qu elle a deja recu"
now=1790000000
is "rien a fournir: on ne bouge pas"    "$(may_ship 0 $now 0 $now)" "rien a fournir"
is "une offre ecrite avant la derniere livraison ne la connait pas"    "$(may_ship 3600 $((now - 60)) $now $now)" "offre plus vieille que la derniere livraison"
is "une offre de plus d une demi-heure est perimee"    "$(may_ship 3600 $((now - 1801)) 0 $now)" perime
is "controle: fraiche, posterieure, et un besoin: on expedie"    "$(may_ship 3600 $((now - 60)) $((now - 600)) $now)" ""
is "controle: pile a la limite de la demi-heure, ca passe encore"    "$(may_ship 3600 $((now - 1799)) 0 $now)" ""

echo
[ "$fail" -eq 0 ] && echo "tout passe" || { echo "$fail echec(s)"; exit 1; }
