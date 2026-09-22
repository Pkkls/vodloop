"""What the relay keeps and what it throws away: sh -c 'python tests/test_relay.py'

Three files made on the spot with ffmpeg. No network, no large file, because
what is under test is the verdict and not the download.

The rule being checked is the one that cost 8 Go on 2026-09-22: yt-dlp returned
zero, the merge had died at the last frame, and what was left looked enough like
a file to be counted as one. Exit codes are not delivery.
"""
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
print("%d echec(s)" % fail)
sys.exit(1 if fail else 0)
