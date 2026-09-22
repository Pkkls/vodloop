#!/usr/bin/env python3
"""What is true of the running chain, and proof that the probe could say no.

    CHAN_ROOT=... python3 check.py            everything, one pass
    CHAN_ROOT=... python3 check.py --quiet    the verdict line only

Thirty-one test files prove the code. Nothing proved the chain, which is the
gap every handoff since the cutover has named and none has closed.

The shape is the whole point. Every line asks a question of the live system,
then asks the *same* question, through the same code, of a case built to fail.
A probe that cannot say no has not said yes, it has said nothing, and the two
are indistinguishable exactly when it matters. A line whose witness does not
fail is reported as blind rather than as passing.

Nothing here writes to the channel. The failing cases are empty directories,
made-up numbers and a unit name that was never installed.
"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402
import cut  # noqa: E402
import feed  # noqa: E402
import watch  # noqa: E402

ARRIVAL_HOURS = int(chan.conf_num("CHECK_ARRIVAL_HOURS", 12))
BEACON_MINUTES = int(chan.conf_num("CHECK_BEACON_MINUTES", 45))

rows = []


def check(name, live, witness, detail=""):
    """live is the answer about the channel. witness is the same question put
    to something built to fail, so it has to come back false."""
    rows.append((name, bool(live), witness is False, detail))


# --- the probes, each one a function of its inputs so a witness can reach it -

def moving(pid, seconds):
    """Bytes the pusher's socket has sent over a window, or None.

    The counter is the socket's, not the process's: /proc/<pid>/io counts what
    a process writes to storage, and a pusher writes to a socket, so it reads
    zero while the wire is perfectly alive. This probe said the channel was
    dead the first time it ran, which is the whole reason the line below asks
    the same question of a window nothing can happen in.
    """
    first = watch.sent_bytes(pid)
    if first is None:
        return None
    time.sleep(seconds)
    second = watch.sent_bytes(pid)
    return None if second is None else second - first


def unseen_units(folders, book, durations, held):
    """Units on the disk nobody has been shown, over the folders given."""
    total = 0
    for folder in folders:
        for path in chan.media(folder):
            total += len(cut.unaired(path, book, durations, held))
    return total


def doubled(book):
    """Files whose ledger holds the same unit twice: a repeat that already happened."""
    return [vid for vid, units in book.items() if len(units) != len(set(units))]


def oversized(paths, session):
    """Those whose picture the session would refuse, plus those unreadable."""
    out = []
    for path in paths:
        info = chan.probe(path)
        if info is None:
            out.append(path.name)
        elif session and feed.exceeds((info["width"], info["height"], info["fps"]),
                                      tuple(session)):
            out.append(path.name)
    return out


def aired_rows():
    """(epoch, video id) of every unit the wire has sent, oldest first.

    Both ledgers, because the granularity moved and the old rows are still
    history: units.tsv is the one that grows, hours.tsv stopped on 2026-09-20.
    """
    rows = []
    for name in ("units.tsv", "hours.tsv"):
        try:
            for line in (chan.STATE / name).read_text().splitlines():
                fields = line.split("	")
                if len(fields) >= 3:
                    rows.append((int(fields[0]), fields[1]))
        except (OSError, ValueError):
            continue
    return sorted(rows)


def skips_that_landed_nowhere(skips, rows):
    """Skips after which the wire went back to the recording it had just left.

    The promise a skip makes is a different stream, not the next hour of the
    same one. It is checkable from history alone: what aired last before the
    skip, what aired first after it. No state has to be fabricated, which is
    what makes this one of the few promise guards that can be read off the
    running channel rather than reasoned about.
    """
    out = []
    for skip in skips:
        when = skip.get("at", 0)
        before = [vid for at, vid in rows if at <= when]
        after = [vid for at, vid in rows if at > when]
        if before and after and before[-1] == after[0]:
            out.append(int(when))
    return out


def crontab_lines():
    """The channel's own cron block, as the daemon would read it."""
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                             timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.splitlines()
            if f"CHAN_ROOT={chan.ROOT}" in line and not line.startswith("#")]


def scheduled(lines, script):
    """Whether something runs that script for this channel."""
    return any(f"/{script}" in line for line in lines)


def newest_arrival(folders):
    newest = 0
    for folder in folders:
        for path in chan.media(folder):
            head = path.name.split("-", 1)[0]
            if head.isdigit():
                newest = max(newest, int(head))
    return newest


def hour_overrun(title):
    """An hour count in a title that is past the total it counts out of.

    Every other probe here watches a state. This one watches a sentence, which
    is the thing viewers actually read, and it exists because on 2026-09-22 the
    channel said "hour 3/2" all evening: seven of the eight files on disk
    announced one hour more than they hold. Nothing could have caught that, both
    calculations being correct on their own.
    """
    found = re.search(r"hour (\d+)/(\d+)", title or "")
    return bool(found) and int(found.group(1)) > int(found.group(2))


# --- the pass ---------------------------------------------------------------

def run():
    pid = int(chan.systemctl("show", "-p", "MainPID", "--value", chan.unit("push")) or 0)
    moved = moving(pid, 6) if pid > 0 else None
    # the witness is the same probe put to a process that holds no socket: the
    # feeder writes into a pipe. A zero second window was tried first and is not
    # one, since the two readings themselves take long enough for a megabit
    # stream to move, which this harness caught on the run that added it.
    mute = int(chan.systemctl("show", "-p", "MainPID", "--value", chan.unit("feed")) or 0)
    check("le pousseur envoie des octets", moved is not None and moved > 0,
          witness=(moving(mute, 0) or 0) > 0 if mute > 0 else False,
          detail=f"{moved} octets en 6 s" if moved is not None else "pas de pid")

    session = chan.read_json(feed.SESSION, {}).get("profile")
    clip = chan.probe(chan.FILLER)
    shape = (clip["width"], clip["height"], clip["fps"]) if clip else None
    check("la session porte le profil du clip d attente",
          bool(session and shape and tuple(session) == shape),
          witness=bool(session) and tuple(session) == (856, 480, 30.0),
          detail=f"session {session}, clip {shape}")

    waiting = sorted(chan.CHUNKS.glob("*.ts"))
    with tempfile.TemporaryDirectory() as empty:
        check("des morceaux attendent d etre envoyes", len(waiting) > 0,
              witness=len(sorted(pathlib.Path(empty).glob("*.ts"))) > 0,
              detail=f"{len(waiting)} morceaux")

    book = cut.ledger()
    check("aucune heure n est inscrite deux fois au registre", not doubled(book),
          witness=not doubled({"temoin": [3, 3]}),
          detail=", ".join(doubled(book))[:60])

    durations = chan.read_json(chan.STATE / "durations.json", {})
    held = cut.reserved()
    unseen = unseen_units((chan.QUEUE, chan.CURRENT, chan.AIRED), book, durations, held)
    with tempfile.TemporaryDirectory() as empty:
        check("il reste de l inedit a montrer", unseen > 0,
              witness=unseen_units((pathlib.Path(empty),), book, durations, held) > 0,
              detail=f"{unseen * chan.CHUNK_SECONDS / 3600:.1f} h")

    said = chan.read_json(chan.STATE / "bot.json", {}).get("title") or ""
    check("l heure annoncee ne depasse pas le total qu elle annonce",
          not hour_overrun(said),
          witness=not hour_overrun("Day 9.1 IRL Pushkar (18 Apr 2026) · hour 3/2"),
          detail=(found.group(0) if (found := re.search(r"hour \d+/\d+", said))
                  else ("titre sans heure" if said else "aucun titre pose")))

    ready = sorted(chan.SHORTS.glob("*.ts"))
    check("aucun short ne depasse la session", not oversized(ready, session),
          # a picture no session opened on can accept, put to the same function
          witness=not (session and feed.exceeds((3840, 2160, 60.0), tuple(session))),
          detail=f"{len(ready)} shorts prets")

    rows = aired_rows()
    skips = chan.read_json(chan.STATE / "bot.json", {}).get("skips") or []
    landed = skips_that_landed_nowhere(skips, rows)
    check("aucun saut n est retombe sur le meme enregistrement", not landed,
          witness=not skips_that_landed_nowhere(
              [{"at": 10}], [(5, "meme"), (15, "meme")]),
          detail=f"{len(skips)} saut(s) examine(s)"
                 + (f", {len(landed)} en faute" if landed else ""))

    board = chan.read_json(chan.ROOT.parent / "board.json", {})
    age = (time.time() - board.get("at", 0)) / 60
    check("la carte donne signe de vie", age < BEACON_MINUTES,
          witness=(time.time() - 0) / 60 < BEACON_MINUTES,
          detail=f"balise il y a {age:.0f} min")

    # the card runs its own rule suite every tick and says so here, which is the
    # only way this side learns that an edit over there broke a rule: Oracle
    # cannot reach the board, and nothing else crosses the NAT
    rules = board.get("rules", "inconnu")
    check("les regles de la carte passent chez elle", rules == "ok",
          witness={"rules": "casse"}.get("rules") == "ok",
          detail=rules)

    newest = newest_arrival((chan.QUEUE, chan.CURRENT, chan.AIRED))
    hours = (time.time() - newest) / 3600 if newest else 999
    with tempfile.TemporaryDirectory() as empty:
        empty_age = newest_arrival((pathlib.Path(empty),))
        check("une livraison est arrivee recemment", hours < ARRIVAL_HOURS,
              witness=(time.time() - empty_age) / 3600 < ARRIVAL_HOURS,
              detail=f"derniere il y a {hours:.1f} h")

    free = shutil.disk_usage(chan.ROOT).free
    check("le disque est au-dessus du plancher", free > chan.FLOOR_BYTES,
          witness=0 > chan.FLOOR_BYTES,
          detail=f"{free / chan.GIB:.1f} Go libres")

    lines = crontab_lines()
    for script in ("supply.py", "watch.py", "ceiling.py"):
        check(f"{script} est bien dans le cron de la chaine", scheduled(lines, script),
              # the same reading, asked about something no channel ever schedules
              witness=scheduled(lines, "jamais-programme.py"),
              detail=f"{len(lines)} ligne(s) pour cette chaine")

    clip = chan.probe(chan.FILLER)
    blocked = 0
    if clip and clip["height"] != chan.MAXH:
        book, held = cut.ledger(), cut.reserved()
        durations = chan.read_json(chan.STATE / "durations.json", {})
        for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED):
            for path in chan.media(folder):
                free = len(cut.unaired(path, book, durations, held))
                info = chan.probe(path) if free else None
                if info and info["height"] > chan.MAXH:
                    blocked += free
    check("le fil est a la hauteur que disent les reglages",
          bool(clip) and clip["height"] == chan.MAXH,
          witness=bool(clip) and clip["height"] == chan.MAXH + 1,
          detail=(f"clip {clip['height'] if clip else '?'} lignes, MAXH {chan.MAXH}"
                  + (f", {blocked} unites au-dessus a passer" if blocked else "")))

    for role in ("push", "feed", "cut", "bot"):
        if role == "bot" and chan.systemctl("is-enabled", chan.unit(role)) != "enabled":
            continue
        state = chan.systemctl("is-active", chan.unit(role))
        check(f"l unite {role} tourne", state == "active",
              witness=chan.systemctl("is-active", chan.unit("jamais-installee")) == "active",
              detail=state)


def main(argv):
    try:
        run()
    except Exception as problem:  # a probe that raises is a finding, not a crash
        check("la passe est allee au bout", False, witness=False, detail=str(problem)[:80])
    wrong = [r for r in rows if not r[1]]
    blind = [r for r in rows if not r[2]]
    if "--quiet" not in argv:
        for name, live, witnessed, detail in rows:
            mark = "AVEUGLE" if not witnessed else ("OK     " if live else "NON    ")
            print(f"  {mark}  {name}{'  ' + detail if detail else ''}")
    print(f"{len(rows) - len(wrong)}/{len(rows)} vrai, "
          f"{len(blind)} sonde(s) incapable(s) de dire non")
    return 1 if wrong or blind else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
