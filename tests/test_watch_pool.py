"""L alerte de vivier, avec ses temoins. Pure: aucun reseau, aucune chaine.

Rejoue la chute du 2026-09-25, 344 candidats a 202, celle que rien n a dite et
qui a coute un host de 400 personnes le lendemain.
"""
import sys

POOL_DROP = 0.10
POOL_FLOOR = 60


def passe(vivier, avant):
    """(alertes, nouveau repere), exactement la logique de watch.py."""
    faults = []
    if vivier and avant and vivier < avant * (1 - POOL_DROP):
        faults.append("chute")
    if vivier and vivier < POOL_FLOOR:
        faults.append("plancher")
    repere = max(vivier, int(avant * 0.98)) if vivier else avant
    return faults, repere


fail = 0


def check(nom, ok, detail=""):
    global fail
    if not ok:
        fail += 1
    print("  %s  %-54s %s" % ("PASS" if ok else "FAIL", nom, detail))


f, r = passe(202, 344)
check("la chute du 25 septembre se dit", "chute" in f, "344 -> 202")
f, r = passe(328, 344)
check("TEMOIN: une variation normale ne dit rien", not f, "344 -> 328, -5%")
f, r = passe(344, 0)
check("TEMOIN: sans repere, on ne crie pas au premier tour", not f)
f, r = passe(40, 344)
check("un vivier sous le plancher se dit aussi", "plancher" in f, "40 < 60")

# La falaise leve la faute tout de suite, et la faute s eteint d elle-meme une
# fois le nouveau niveau admis. Combien de MESSAGES partent est decide ailleurs,
# par alarm_traffic et son REPEAT_SECONDS: ce test ne mesure que ce que cette
# logique-ci promet, sinon il testerait deux choses et n en garantirait aucune.
repere = 344
premier, _ = passe(202, repere)
check("la falaise leve la faute des la premiere passe", "chute" in premier)
for passe_n in range(30):
    dernier, repere = passe(202, repere)
check("et la faute s eteint une fois le niveau admis", "chute" not in dernier,
      "apres 30 passes, soit environ 2 h 30")
check("le repere a rejoint le nouveau niveau", repere == 202, repere)

# temoin du temoin: sans la decroissance, ca alarmerait sans fin
repere, dits = 344, 0
for passe_n in range(30):
    f = ["chute"] if 202 < repere * (1 - POOL_DROP) else []
    repere = max(202, repere)          # l ancienne version, sans decroissance
    dits += 1 if f else 0
check("TEMOIN: sans la decroissance, 30 passes = 30 alertes", dits == 30, dits)

print()
print("%d echec(s)" % fail)
sys.exit(1 if fail else 0)
