"""What the relay keeps and what it throws away: sh -c 'python tests/test_relay.py'

Three files made on the spot with ffmpeg. No network, no large file, because
what is under test is the verdict and not the download.

The rule being checked is the one that cost 8 Go on 2026-09-22: yt-dlp returned
zero, the merge had died at the last frame, and what was left looked enough like
a file to be counted as one. Exit codes are not delivery.
"""
import base64
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
print("relay: ce qui tient dans le budget restant passe devant")
file = [("gros", 25200, ""), ("moyen", 21240, ""), ("court", 8640, "")]
ordre = lambda reste: [c[0] for c in relay.abordables_d_abord(file, reste)]
check("TEMOIN: 3074 Mo restants, seul le 2.4 h tient: il passe premier",
      ordre(3074)[0], "court", "sinon le relais attend 90 min sur un 7 h")
check("entre deux abordables, l ordre de la file est garde",
      ordre(5000), ["moyen", "court", "gros"], "5000 Mo: le 7 h ne tient pas")
check("controle: budget large, on ne reclasse rien",
      ordre(100000), ["gros", "moyen", "court"], "")
check("rien n est ecarte, seulement repousse",
      len(ordre(0)), 3, "un budget a zero garde les trois")

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
print("relay: une livraison n est finie qu une fois le fichier en file")
# seule la carte sortait un fichier de upload/, et seulement celui qu elle
# venait d envoyer, donc tout ce que ce relais livrait restait dans un
# cul-de-sac: sept heures jouables y dormaient le 2026-09-22 pendant que la
# chaine manquait de matiere et en reclamait vingt-deux
faux_conf = {"ORACLE": "ubuntu@exemple", "ORACLE_KEY": "cle"}
vu = {"rend": b"ok", "code": 0, "appels": 0}


class Repondu:
    def __init__(self, sortie, code):
        self.stdout, self.stderr, self.returncode = sortie, b"", code


def faux_run(argv, **kw):
    vu["appels"] += 1
    vu["payload"] = argv[-1]
    return Repondu(vu["rend"], vu["code"])


vrai_run = relay.subprocess.run
try:
    relay.subprocess.run = faux_run
    livre = pathlib.Path(tempfile.gettempdir()) / "Un_Titre-aB3dEfGhIjK.mkv"
    livre.write_bytes(b"x" * 4096)
    mis = relay.deliver(faux_conf, livre)
    check("le nom mis en file porte le prefixe d epoque, comme ceux de la carte",
          bool(mis) and mis.endswith("-" + livre.name)
          and mis.split("-")[0].isdigit(), True, mis)
    envoye = base64.b64decode(vu["payload"].split()[1]).decode()
    check("la taille locale voyage avec, pour etre comparee la-bas",
          str(livre.stat().st_size) in envoye, True)
    check("et la destination est bien la file", "queue/" in envoye, True)
    # le temoin qui compte: un refus doit rendre None, parce que c est ce qui
    # garde la copie locale dans ship()
    vu["rend"] = b"taille 10"
    check("TEMOIN: une taille qui ne correspond pas ne rend rien",
          relay.deliver(faux_conf, livre), None)
    vu["rend"], vu["code"] = b"", 255
    check("TEMOIN: une machine injoignable non plus",
          relay.deliver(faux_conf, livre), None)
    avant = vu["appels"]
    check("TEMOIN: un nom que le shell ne peut pas porter est refuse sans reseau",
          relay.deliver(faux_conf, livre.with_name("l'apostrophe.mkv")), None)
    check("TEMOIN: et rien n a ete tente", vu["appels"], avant)
finally:
    relay.subprocess.run = vrai_run
    livre.unlink(missing_ok=True)

print()
print("%d echec(s)" % fail)
sys.exit(1 if fail else 0)
