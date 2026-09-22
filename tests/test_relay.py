"""What the relay keeps and what it throws away: sh -c 'python tests/test_relay.py'

Three files made on the spot with ffmpeg. No network, no large file, because
what is under test is the verdict and not the download.

The rule being checked is the one that cost 8 Go on 2026-09-22: yt-dlp returned
zero, the merge had died at the last frame, and what was left looked enough like
a file to be counted as one. Exit codes are not delivery.
"""
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import relay  # noqa: E402


def ffmpeg(args):
    subprocess.run(["ffmpeg", "-y", "-v", "error"] + args, capture_output=True, timeout=180)


tmp = pathlib.Path(tempfile.mkdtemp(prefix="relay-"))
complet, sans_son = tmp / "complet.mkv", tmp / "sans_son.mkv"
ffmpeg(["-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(complet)])
ffmpeg(["-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=4",
        "-c:v", "libx264", str(sans_son)])
if not complet.exists():
    sys.exit("ffmpeg n a rien produit: le test ne peut rien dire")

fail = 0


def check(nom, obtenu, attendu, detail=""):
    global fail
    if obtenu != attendu:
        fail += 1
    print("  %s  %-52s %s" % ("PASS" if obtenu == attendu else "FAIL", nom, detail))


print("relay: ce qui est livre a ete ouvert")
bon, why = relay.verify(complet, 4)
check("un fichier entier avec ses deux pistes passe", bon, True, why)
bon, why = relay.verify(complet, 400)
check("TEMOIN: la meme video annoncee 400 s est jugee tronquee", bon, False, why)
bon, why = relay.verify(sans_son, 4)
check("TEMOIN: sans piste audio, refuse", bon, False, why)
bon, why = relay.verify(tmp / "rien.mkv", 4)
check("TEMOIN: fichier absent, refuse", bon, False, why)
bon, why = relay.verify(complet, 0)
check("duree inconnue: on ne refuse pas sur ce qu on ignore", bon, True, why)

print()
print("relay: le plafond est lu contre la taille de ce qui vient")
budget = relay.budget_mb()
check("un fichier entier tient dans la fenetre a vide",
      relay.peut_prendre(0, 5280), True, "5280 Mo sur %d" % budget)
check("TEMOIN: rien de pris, mais plus gros que la fenetre entiere: refuse",
      relay.peut_prendre(0, budget + 1), False,
      "c est exactement ce que l ancien plafond laissait passer")
check("TEMOIN: la place restante est lue contre la taille qui vient",
      relay.peut_prendre(budget - 1000, 2340), False, "1000 Mo restants")
check("ce qui rentre dans la place restante passe",
      relay.peut_prendre(budget - 1000, 900), True, "")
check("la fenetre tient le plus gros fichier possible (8 h de video)",
      relay.peut_prendre(0, 8 * relay.MB_PER_HOUR_VIDEO), True,
      "sinon le relais patiente sur un fichier qu il ne prendra jamais")

print()
print("relay: un titre que la console ne sait pas ecrire ne tue pas le relais")
# La vraie console de kil encode en cp1252. On la reproduit dans un sous-processus
# plutot que de deviner celle qui lance la suite, et on imprime le crochet
# japonais du titre qui a tue --plan le 2026-09-22.
titre = ("import sys; sys.path.insert(0, %r); import relay; "
         "print('titre \\u300cjaponais\\u300d')" % str(ROOT / "tools"))
done = subprocess.run([sys.executable, "-c", titre], capture_output=True,
                      env=dict(os.environ, PYTHONIOENCODING="cp1252"), timeout=120)
souci = done.stderr.decode("utf-8", "replace").strip().rsplit("\n", 1)[-1]
check("TEMOIN: un titre CJK sur une console cp1252 ne leve rien",
      done.returncode, 0, souci[:60])

print()
print("%d echec(s)" % fail)
sys.exit(1 if fail else 0)
