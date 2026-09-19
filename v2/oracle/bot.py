#!/usr/bin/env python3
"""The channel's chat: what is playing, what is next, and a vote to move on.

    CHAN_ROOT=... python3 bot.py                 run the service
    CHAN_ROOT=... python3 bot.py --authorize     print the URL to click, once
    CHAN_ROOT=... python3 bot.py --status        what it knows, without a token

It listens on 127.0.0.1 only; nginx holds the certificate and the rate limit and
passes /kick/webhook and /kick/callback through. Every POST is verified against
Kick's published key before it is read at all, because the endpoint's URL sits in
a form on a public website and a forged chat message would drive the channel.

What a viewer can do:
    !help            the commands
    !vod             what is on, which hour of it, how long is left
    !list            what is on the shelf, numbered
    !pick <n>        vote for what plays next
    !vote            vote to move on now
    !source          where this video comes from
    !stats           the library, the hours aired, the uptime

What stops a vote from becoming a remote control, all of it settable per channel:
    an hour cannot be voted off in its first SKIP_MIN_AIRED_SECONDS
    a successful skip locks the next one for SKIP_COOLDOWN_SECONDS
    SKIP_MAX_PER_HOUR successful skips in a rolling hour and no more
    a vote needs max(VOTE_MIN_VOTES, viewers x VOTE_RATIO), one voice per account
    a vote that fails locks the next one for VOTE_FAIL_COOLDOWN_SECONDS
    no vote opens at all when there is nothing unseen to move on to
    one command per viewer per USER_COOLDOWN_SECONDS, anonymous accounts ignored
The broadcaster and its moderators carry !force, which skips the vote but not the
check that something else exists to play.
"""
import html
import json
import math
import os
import pathlib
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402
import cut  # noqa: E402
import kickapi  # noqa: E402

STATE = chan.STATE / "bot.json"
SKIP = chan.STATE / "skip"
PICK = chan.STATE / "pick"
PORT = int(chan.conf_num("BOT_PORT", 8787))
BROADCASTER = int(chan.conf_num("KICK_USER_ID", 0))
SLUG = chan.CONF.get("KICK_SLUG", chan.NAME)

VOTE_WINDOW = int(chan.conf_num("VOTE_WINDOW_SECONDS", 120))
VOTE_MIN = int(chan.conf_num("VOTE_MIN_VOTES", 3))
VOTE_RATIO = chan.conf_num("VOTE_RATIO", 0.25)
VOTE_FAIL_COOLDOWN = int(chan.conf_num("VOTE_FAIL_COOLDOWN_SECONDS", 600))
SKIP_COOLDOWN = int(chan.conf_num("SKIP_COOLDOWN_SECONDS", 1200))
SKIP_MAX_PER_HOUR = int(chan.conf_num("SKIP_MAX_PER_HOUR", 3))
SKIP_MIN_AIRED = int(chan.conf_num("SKIP_MIN_AIRED_SECONDS", 600))
USER_COOLDOWN = int(chan.conf_num("USER_COOLDOWN_SECONDS", 15))
TITLE_MIN_INTERVAL = int(chan.conf_num("TITLE_MIN_INTERVAL_SECONDS", 90))
SEEN_KEPT = 300

_lock = threading.Lock()
_last_said = [0.0]


# --- reading the pipeline --------------------------------------------------

def pretty(name):
    """A file name as a human would say it: no epoch prefix, no id, no underscores."""
    stem = re.sub(r"^\d{9,}-", "", pathlib.Path(name).stem)
    stem = re.sub(r"-[A-Za-z0-9_-]{11}$", "", stem)
    return re.sub(r"\s+", " ", stem.replace("_", " ")).strip(" -.") or "untitled"


def playing():
    """What is on the wire right now, as far as the cutter has said."""
    job = chan.read_json(cut.JOB, None)
    if not job or not job.get("source"):
        return None
    seconds = float(job.get("seconds") or 0)
    number = int(job.get("number") or 0)
    total = cut.slices_in(seconds) if seconds else 1
    started = float(job.get("started") or 0)
    return {"name": job["source"], "title": pretty(job["source"]),
            "hour": number + 1, "hours": total, "started": started,
            "elapsed": max(0.0, time.time() - started) if started else 0.0,
            "vid": chan.video_id(job["source"]) or ""}


def shelf():
    """Files that still hold an hour nobody has seen, queue first then reserve."""
    book = cut.ledger()
    durations = chan.read_json(chan.STATE / "durations.json", {})
    out = []
    for folder in (chan.QUEUE, chan.AIRED):
        for path in chan.media(folder):
            free = cut.unaired(path, book, durations)
            if free:
                out.append((path, len(free)))
    out.sort(key=lambda row: (not cut.from_board(row[0]), row[0].name))
    return out


def unseen_hours():
    return sum(hours for _, hours in shelf())


# --- state -----------------------------------------------------------------

def load():
    data = chan.read_json(STATE, {})
    data.setdefault("vote", None)
    data.setdefault("skips", [])
    data.setdefault("seen", [])
    data.setdefault("users", {})
    data.setdefault("title", "")
    data.setdefault("title_at", 0)
    return data


def save(data):
    data["seen"] = data["seen"][-SEEN_KEPT:]
    cutoff = time.time() - 7200
    data["skips"] = [t for t in data["skips"] if t > cutoff]
    data["users"] = {u: t for u, t in data["users"].items() if t > time.time() - 3600}
    chan.write_json(STATE, data)


def say(text, reply_to=None):
    """One message, never two within a second and a half of each other."""
    with _lock:
        wait = 1.5 - (time.time() - _last_said[0])
        if wait > 0:
            time.sleep(wait)
        _last_said[0] = time.time()
    return kickapi.say(BROADCASTER, text, reply_to)


# --- the guards ------------------------------------------------------------

def skip_blocked(data, now, live):
    """Why a skip cannot happen now, or None. Checked for votes and for !force."""
    if unseen_hours() < 1:
        return "nothing unseen left to move on to right now"
    recent = [t for t in data["skips"] if t > now - 3600]
    if len(recent) >= SKIP_MAX_PER_HOUR:
        return f"{len(recent)} skips this hour already, that is the limit"
    if recent and now - max(recent) < SKIP_COOLDOWN:
        left = int((SKIP_COOLDOWN - (now - max(recent))) / 60) + 1
        return f"we just skipped one, next vote possible in {left} min"
    if live and live["elapsed"] < SKIP_MIN_AIRED:
        left = int((SKIP_MIN_AIRED - live["elapsed"]) / 60) + 1
        return f"this hour just started, votable in {left} min"
    return None


def threshold():
    return max(VOTE_MIN, math.ceil(kickapi.viewers(SLUG) * VOTE_RATIO))


def do_skip(data, now, reason):
    SKIP.write_text(json.dumps({"at": int(now), "reason": reason}))
    data["skips"].append(now)
    data["vote"] = None


# --- commands --------------------------------------------------------------

def cmd_aide(*_):
    return ("commands: !vod what is playing · !list the library · !pick <n> vote for "
            "what plays next · !vote skip to something else · !source · !stats")


def cmd_vod(*_):
    live = playing()
    if not live:
        return "nothing being cut right now"
    left = max(0, chan.PART_SECONDS - live["elapsed"]) if chan.PART_SECONDS else 0
    piece = f" (hour {live['hour']}/{live['hours']})" if live["hours"] > 1 else ""
    tail = f", {int(left / 60)} min left" if left else ""
    return f"on air: {live['title']}{piece}{tail}"


def cmd_liste(*_):
    rows = shelf()[:5]
    if not rows:
        return "the library is dry, the board is fetching more right now"
    listing = " · ".join(f"{n + 1}. {pretty(p.name)[:38]} ({h}h)"
                         for n, (p, h) in enumerate(rows))
    return f"up next: {listing} — !pick <n> to vote"


def cmd_source(*_):
    live = playing()
    if not live:
        return "nothing playing"
    if not live["vid"]:
        return "unknown source for this file"
    if re.match(r"^k[0-9a-f]{8}\d{2}$", live["vid"]):
        return "this one comes from a Kick VOD of the channel"
    return f"source: https://youtu.be/{live['vid']}"


def cmd_stats(*_):
    book = cut.ledger()
    hours = sum(len(v) for v in book.values())
    rows = shelf()
    return (f"{len(rows)} videos on the shelf, {unseen_hours()}h never aired, "
            f"{hours}h put on the wire so far")


def cmd_vote(data, now, sender, args):
    live = playing()
    vote = data.get("vote")
    if vote and vote["closes"] > now:
        if sender["user_id"] in vote["voters"]:
            return None
        vote["voters"].append(sender["user_id"])
        need = vote["need"]
        if len(vote["voters"]) >= need:
            if vote["kind"] == "pick":
                PICK.write_text(json.dumps({"name": vote["target"], "at": int(now)}))
                data["vote"] = None
                return f"voted: {pretty(vote['target'])} plays next"
            do_skip(data, now, "vote")
            return "voted, moving on"
        return f"skip vote: {len(vote['voters'])}/{need}"
    if vote and now - vote.get("failed_at", 0) < 0:
        return None
    last_fail = data.get("vote_failed_at", 0)
    if now - last_fail < VOTE_FAIL_COOLDOWN:
        left = int((VOTE_FAIL_COOLDOWN - (now - last_fail)) / 60) + 1
        return f"a vote just failed, next one possible in {left} min"
    blocked = skip_blocked(data, now, live)
    if blocked:
        return blocked
    need = threshold()
    data["vote"] = {"kind": "skip", "target": None, "voters": [sender["user_id"]],
                    "need": need, "closes": now + VOTE_WINDOW}
    if need <= 1:
        do_skip(data, now, "vote")
        return "moving on"
    return f"vote to skip started: {need} votes needed in {VOTE_WINDOW}s, type !vote"


def cmd_pick(data, now, sender, args):
    rows = shelf()
    if not rows:
        return "nothing to choose from, the library is empty"
    try:
        index = int(args[0]) - 1
    except (IndexError, ValueError):
        return "usage: !pick <number>, see !list"
    if not 0 <= index < min(5, len(rows)):
        return f"pick between 1 and {min(5, len(rows))}, see !list"
    target = rows[index][0].name
    vote = data.get("vote")
    if vote and vote["closes"] > now and vote["kind"] == "pick" and vote["target"] == target:
        return cmd_vote(data, now, sender, args)
    if vote and vote["closes"] > now:
        return "a vote is already running"
    need = threshold()
    data["vote"] = {"kind": "pick", "target": target, "voters": [sender["user_id"]],
                    "need": need, "closes": now + VOTE_WINDOW}
    if need <= 1:
        PICK.write_text(json.dumps({"name": target, "at": int(now)}))
        data["vote"] = None
        return f"{pretty(target)} plays next"
    return (f"vote to play {pretty(target)[:40]} next: {need} votes in "
            f"{VOTE_WINDOW}s, type !pick {index + 1}")


def cmd_force(data, now, sender, args):
    if not sender["privileged"]:
        return None
    if unseen_hours() < 1:
        return "nothing unseen left to move on to"
    do_skip(data, now, f"forced by {sender['name']}")
    return "moving on"


COMMANDS = {
    "aide": cmd_aide, "help": cmd_aide, "commands": cmd_aide, "commandes": cmd_aide,
    "vod": cmd_vod, "now": cmd_vod, "np": cmd_vod,
    "liste": cmd_liste, "list": cmd_liste, "videos": cmd_liste,
    "source": cmd_source, "stats": cmd_stats,
    "vote": cmd_vote, "skip": cmd_vote, "next": cmd_vote,
    "pick": cmd_pick, "choix": cmd_pick,
    "force": cmd_force,
}


def handle(payload):
    """One chat message in, at most one line of chat out."""
    sender = payload.get("sender") or {}
    if sender.get("is_anonymous") or not sender.get("user_id"):
        return
    content = (payload.get("content") or "").strip()
    if not content.startswith("!"):
        return
    word, _, rest = content[1:].partition(" ")
    action = COMMANDS.get(word.lower())
    if not action:
        return
    badges = {b.get("type") for b in ((sender.get("identity") or {}).get("badges") or [])}
    who = {"user_id": str(sender["user_id"]),
           # the name is only ever used inside our own sentences, never echoed raw
           "name": re.sub(r"[^\w .-]", "", str(sender.get("username") or ""))[:25],
           "privileged": bool(badges & {"moderator", "broadcaster", "owner"})}
    now = time.time()
    data = load()
    last = data["users"].get(who["user_id"], 0)
    if now - last < USER_COOLDOWN and not who["privileged"]:
        return
    data["users"][who["user_id"]] = now
    try:
        answer = action(data, now, who, rest.split())
    except Exception as problem:  # a bad command must never take the service down
        chan.log(f"commande {word} en erreur: {problem}")
        answer = None
    save(data)
    if answer:
        say(answer, payload.get("message_id"))


# --- the title -------------------------------------------------------------

def wanted_title():
    live = playing()
    if not live:
        return None
    piece = f" [{live['hour']}/{live['hours']}]" if live["hours"] > 1 else ""
    head = live["title"][:85]
    return f"{head}{piece} · !vote to skip · 24/7 rerun"


def keep_title():
    """Follow what the cutter is playing, and say it on the channel.

    Only when it has really changed and never more often than the interval: a
    title rewritten every poll is a call to the API every poll, and the channel
    gains nothing from it.
    """
    while True:
        try:
            data = load()
            want = wanted_title()
            now = time.time()
            if (want and want != data.get("title")
                    and now - data.get("title_at", 0) > TITLE_MIN_INTERVAL):
                if kickapi.set_title(want):
                    data["title"], data["title_at"] = want, now
                    save(data)
                    chan.log(f"titre: {want}")
            close_stale_vote(data, now)
        except Exception as problem:
            chan.log(f"boucle titre: {problem}")
        time.sleep(20)


def close_stale_vote(data, now):
    vote = data.get("vote")
    if vote and vote["closes"] <= now:
        data["vote"] = None
        data["vote_failed_at"] = now
        save(data)
        say(f"vote closed without a majority ({len(vote['voters'])}/{vote['need']})")


def ensure_subscription():
    """Make sure Kick is actually sending us the chat, and say so once."""
    try:
        names = {s.get("event") for s in kickapi.subscriptions()}
        if "chat.message.sent" not in names:
            if kickapi.subscribe(BROADCASTER):
                chan.log("abonnement chat.message.sent cree")
            else:
                chan.log("abonnement chat.message.sent refuse")
    except Exception as problem:
        chan.log(f"abonnement: {problem}")


# --- the server ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "vodloop"

    def log_message(self, *_):
        pass

    def _send(self, code, body=b"", kind="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if not path.endswith("/kick/callback"):
            return self._send(404)
        args = urllib.parse.parse_qs(query)
        code = (args.get("code") or [""])[0]
        state = (args.get("state") or [""])[0]
        if not code:
            return self._send(400, b"pas de code")
        ok, message = kickapi.exchange(code, state)
        chan.log(f"callback oauth: {message}")
        if ok:
            ensure_subscription()
        return self._send(200 if ok else 400,
                          html.escape(message).encode(), "text/plain; charset=utf-8")

    def do_POST(self):
        if not self.path.endswith("/kick/webhook"):
            return self._send(404)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 64 * 1024:
            return self._send(400)
        body = self.rfile.read(length)
        if not kickapi.verify(self.headers, body):
            chan.log("webhook refuse: signature invalide")
            return self._send(401)
        try:
            payload = json.loads(body)
        except ValueError:
            return self._send(400)
        # answered before the work, so a slow command never makes Kick retry
        self._send(200)
        if self.headers.get("Kick-Event-Type") != "chat.message.sent":
            return
        if BROADCASTER and int((payload.get("broadcaster") or {}).get("user_id") or 0) != BROADCASTER:
            return
        message_id = payload.get("message_id") or ""
        data = load()
        if message_id and message_id in data["seen"]:
            return
        data["seen"].append(message_id)
        save(data)
        handle(payload)


def main(argv):
    chan.STATE.mkdir(parents=True, exist_ok=True)
    if "--authorize" in argv:
        redirect = kickapi.APP.get(
            "KICK_REDIRECT_URI", "https://vodloop.kicknosubviewer.duckdns.org/kick/callback")
        print(kickapi.authorize_url(redirect))
        return 0
    if "--status" in argv:
        live = playing()
        print(f"a l'antenne: {live['title'] if live else 'rien'}")
        print(f"inedit en reserve: {unseen_hours()} h sur {len(shelf())} videos")
        print(f"titre vise: {wanted_title()}")
        print(f"jeton: {'present' if kickapi.token() else 'absent, --authorize'}")
        return 0
    if not BROADCASTER:
        chan.log("KICK_USER_ID absent de channel.env")
        return 1
    threading.Thread(target=keep_title, daemon=True).start()
    if kickapi.token():
        ensure_subscription()
    else:
        chan.log("pas de jeton: lancer bot.py --authorize et ouvrir l'URL")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    chan.log(f"bot a l'ecoute sur 127.0.0.1:{PORT} pour {SLUG}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
