#!/usr/bin/env python3
"""Check the chain end to end, repair what is safe to repair, say so. From cron.

    python3 watch.py            report
    python3 watch.py --apply    repair and alert

Measured, never inferred: an API saying "live" is not a picture (v1 pushed the
standby clip for twenty minutes under a green live flag), and a probe that
could not run is unknown, not broken. Repairs are the ones v1 needed and did by
hand: a unit that is down is started, a pusher holding a silent socket twice in
a row is restarted, at most every twenty minutes. Alerts fire when a fault
starts, again every three hours while it lasts, and once when it clears.
"""
import pathlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402

STATEFILE = chan.STATE / "watch.json"
REPEAT_SECONDS = 3 * 3600
MUTE_COOLDOWN = 20 * 60
STALE_SECONDS = 30 * 60
DRY_ARRIVAL_SECONDS = 12 * 3600
# under two hours of unseen material the board has about one delivery of grace
THIN_RUNWAY_SECONDS = 2 * 3600
# A delivery is the card's business from end to end: it copies into upload/ and
# it is the only thing that moves the file out of it. So a file that stops
# growing there is one nothing will ever claim, and on 2026-09-22 that was a
# whole seven hour stream, complete and playable, sitting on five gigabytes of
# disk while the channel was short of material and nobody could see it.
UPLOAD_STALL_SECONDS = 60 * 60


def sent_bytes(pid):
    """Bytes the pusher's socket has sent so far, or None if unmeasurable."""
    out = subprocess.run(["ss", "-tnip"], capture_output=True, text=True, timeout=20).stdout
    lines = out.splitlines()
    for n, line in enumerate(lines):
        if f"pid={pid}," in line:
            for follow in lines[n:n + 3]:
                for token in follow.split():
                    if token.startswith("bytes_sent:"):
                        return int(token.split(":")[1])
    return None


def emitting(pid, seconds=8):
    first = sent_bytes(pid)
    if first is None:
        return None
    time.sleep(seconds)
    second = sent_bytes(pid)
    return None if second is None else second - first


def newest_arrival():
    stamps = [p.stat().st_mtime for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED)
              for p in chan.media(folder)]
    return max(stamps) if stamps else 0


def stalled_deliveries(stamped, now, limit=UPLOAD_STALL_SECONDS):
    """Names in upload/ that stopped growing. (name, mtime) pairs in, pure.

    A live copy touches its file continuously, so age alone tells a stalled
    transfer from a slow one whatever its size.
    """
    return [name for name, mtime in stamped if now - mtime > limit]


def main(argv):
    apply = "--apply" in argv
    now = time.time()
    data = chan.read_json(STATEFILE, {})
    faults = {}

    # the bot is watched only where it is installed: a channel without a Kick
    # app has its unit disabled on purpose, and starting it would alarm forever
    roles = ["push", "feed", "cut"]
    if chan.systemctl("is-enabled", chan.unit("bot")) == "enabled":
        roles.append("bot")
    for role in roles:
        if chan.systemctl("is-active", chan.unit(role)) != "active":
            faults[f"unit-{role}"] = f"{chan.unit(role)} arrete"
            if apply:
                subprocess.run(["sudo", "-n", "systemctl", "start", chan.unit(role)])

    pid = int(chan.systemctl("show", "-p", "MainPID", "--value", chan.unit("push")) or 0)
    moved = emitting(pid) if pid > 0 else None
    print(f"pusher pid {pid}: {moved} octets en 8 s")
    if moved == 0:
        data["mute"] = data.get("mute", 0) + 1
        faults["muet"] = "le pusher n'envoie plus rien"
        if (apply and data["mute"] >= 2
                and now - data.get("mute_restart", 0) > MUTE_COOLDOWN):
            subprocess.run(["sudo", "-n", "systemctl", "restart", chan.unit("push")])
            data["mute_restart"], data["mute"] = now, 0
            chan.telegram("pusher muet deux passes de suite: relance")
    else:
        data["mute"] = 0

    chunks = len(list(chan.CHUNKS.glob("*.ts")))
    if chunks == 0:
        faults["tampon"] = "aucun chunk pret: clip d'attente a l'antenne"
    want = chan.read_json(chan.STATE / "want.json", {})
    if now - want.get("at", 0) > STALE_SECONDS:
        faults["supply"] = "want.json perime: supply.py ne tourne plus"
    # The wire never goes empty any more: under the reserve it repeats an hour
    # instead. That is quieter than a loading card and it is still the channel
    # running out, so it is said out loud while there is time to answer it.
    runway = want.get("runway_seconds")
    if runway is not None and runway < THIN_RUNWAY_SECONDS:
        faults["reserve"] = (f"{runway / 3600:.1f} h d'inedit seulement: "
                             f"l'antenne va commencer a repasser des heures")
    board = chan.read_json(chan.ROOT.parent / "board.json", {})
    if now - board.get("at", 0) > STALE_SECONDS:
        faults["carte"] = "la carte ne donne plus signe de vie"
    # A budget the board has halved against itself is invisible from here: the
    # beacon said the card was alive and fetching, and on 2026-09-22 the
    # channel spent a day on half its supply without a word. What the card is
    # allowed to carry belongs in the same list as the disk and the reserve.
    level = int(board.get("throttle") or 0)
    if level:
        faults["budget carte"] = (f"la carte s'est bridee au niveau {level}: "
                                  f"budget divise par {2 ** level} tant que "
                                  f"YouTube refuse")
    if want.get("need_seconds") and now - newest_arrival() > DRY_ARRIVAL_SECONDS:
        faults["approvisionnement"] = "rien de neuf depuis 12 h alors que la file manque"
    free = shutil.disk_usage(chan.ROOT).free
    if free < chan.FLOOR_BYTES:
        faults["disque"] = f"{free / chan.GIB:.1f} Go libres, sous le plancher"
    figes = stalled_deliveries([(p.name, p.stat().st_mtime)
                                for p in chan.media(chan.UPLOAD)], now)
    if figes:
        faults["livraison"] = (f"{len(figes)} fichier(s) figes dans upload/ depuis "
                               f"plus de {UPLOAD_STALL_SECONDS // 60} min, que rien "
                               f"ne viendra prendre: {figes[0][:44]}")

    print(f"chunks {chunks}, file {want.get('queue_hours')} h, inedit "
          f"{(want.get('runway_seconds') or 0) / 3600:.1f} h, besoin "
          f"{(want.get('need_seconds') or 0) / 3600:.1f} h, libre {free / chan.GIB:.1f} Go, "
          f"fautes {sorted(faults) or 'aucune'}")
    alarms = data.get("alarms", {})
    for key, message in faults.items():
        last = alarms.get(key, 0)
        if apply and now - last > REPEAT_SECONDS:
            chan.telegram(f"ALERTE {message}")
            alarms[key] = now
    for key in list(alarms):
        if key not in faults:
            if apply:
                chan.telegram(f"revenu a la normale: {key}")
            alarms.pop(key)
    data["alarms"] = alarms
    if apply:
        chan.write_json(STATEFILE, data)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
