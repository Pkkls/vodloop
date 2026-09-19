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
import feed  # noqa: E402
import kickapi  # noqa: E402
import supply  # noqa: E402

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
# A paid fetch rides on top of the supply the channel already needs, so the
# board's own ceilings can swallow one. kil, 2026-09-19: "si ca bloque, refund
# les points". Both numbers exist so that the points come back.
REQUEST_MAX_PENDING = int(chan.conf_num("REQUEST_MAX_PENDING", 3))
REQUEST_DEADLINE = int(chan.conf_num("REQUEST_DEADLINE_HOURS", 6)) * 3600
TITLE_MIN_INTERVAL = int(chan.conf_num("TITLE_MIN_INTERVAL_SECONDS", 90))
TITLE_CHECK_INTERVAL = int(chan.conf_num("TITLE_CHECK_INTERVAL_SECONDS", 300))
SEEN_KEPT = 300

_lock = threading.Lock()
_last_said = [0.0]


# --- reading the pipeline --------------------------------------------------

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# what an uploader leaves in a file name and no viewer needs: the channel said
# twice, the word VOD, and the recording's own reference (c223, @na)
NOISE = re.compile(r"^(kick|vod|vods|full|twitch|youtube|@\w{1,4}|[a-z]\d{3,})$", re.I)


def pretty(name):
    """A file name as a human would say it: no epoch prefix, no id, no underscores."""
    stem = re.sub(r"^\d{9,}-", "", pathlib.Path(name).stem)
    stem = re.sub(r"-[A-Za-z0-9_-]{11}$", "", stem)
    return re.sub(r"\s+", " ", stem.replace("_", " ")).strip(" -.") or "untitled"


def said_date(stem):
    """(date as a viewer reads it, what is left) from the two prefixes uploads use."""
    found = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ _-]+(.*)$", stem)
    if found:
        year, month, day, rest = found.groups()
    else:
        found = re.match(r"^(\d{2})(\d{2})(\d{2})[ _-]+(.*)$", stem)
        if not found:
            return "", stem
        year, month, day, rest = "20" + found.group(1), found.group(2), found.group(3), found.group(4)
    try:
        number = int(month)
        if not 1 <= number <= 12 or not 1 <= int(day) <= 31:
            return "", stem
    except ValueError:
        return "", stem
    return f"{int(day)} {MONTHS[number - 1]} {year}", rest


def clean_title(name, slug=""):
    """What the channel should say it is playing.

    An uploader's file name is not a title: "250512_nanatty_Kick_VOD_@na___c223"
    carries a date, the channel's own name twice over and the reference of the
    recording, and none of the last three mean anything to someone arriving on
    the stream. The date does, on a channel that plays nothing but past streams,
    so it is kept and written the way it is read.
    """
    stem = pretty(name)
    when, rest = said_date(stem)
    # the channel's own name, however the uploader spelled it, is already the
    # last thing in the title and says nothing twice
    mine = {slug.lower(), re.sub(r"\d+$", "", slug).lower(),
            chan.CONF.get("TITLE_SUFFIX", "").lstrip("@").lower()} - {""}
    words = [w for w in re.split(r"[ _]+", rest.replace("-", " ")) if w]
    kept = [w for w in words if not NOISE.match(w) and w.lower() not in mine]
    body = re.sub(r"\s+", " ", " ".join(kept)).strip(" -.,")
    if when and body:
        return f"{body} ({when})"
    return body or when or "past stream"


def playing():
    """What is going out this second, taken from the feeder and not the cutter.

    The cutter runs an hour ahead of the wire and deletes its job the moment it
    finishes, so a title read from it announced the next hour an hour early and
    then froze on it. The feeder is the only thing that knows which chunk is
    leaving, and it writes that down as it sends.
    """
    live = chan.read_json(feed.ONAIR, None)
    if not live or live.get("filler") or not live.get("source"):
        return None
    seconds = float(live.get("seconds") or 0)
    number = int(live.get("number") or 0)
    started = float(live.get("at") or 0)
    return {"name": live["source"], "title": pretty(live["source"]),
            "hour": number + 1, "hours": cut.slices_in(seconds) if seconds else 1,
            "started": started,
            "elapsed": max(0.0, time.time() - started) if started else 0.0,
            "vid": chan.video_id(live["source"]) or ""}


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
    data.setdefault("said_playing", "")
    data.setdefault("asked", {})
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
    """How many voices carry a vote, never more than there are people to give them.

    kil, 2026-09-19: asking three when one person is watching is asking for
    nothing to ever happen. The floor is what stops one viewer overruling a
    crowd, so it only means anything up to the size of the crowd: alone, one
    voice is the whole room and it decides.

        1 viewer  -> 1      3 viewers -> 3      20 viewers -> 5
        2 viewers -> 2     12 viewers -> 3      40 viewers -> 10
    """
    seen = max(1, kickapi.viewers(SLUG))
    return max(1, min(VOTE_MIN, seen), math.ceil(seen * VOTE_RATIO))


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


# --- how long a viewer has to wait -----------------------------------------

# Measured on the board's own log: 1364 Mo in 16 min and 3389 Mo in 34, so
# about ninety a minute over the home line. The slot allowance is the gap the
# board keeps between downloads when a channel is short.
BOARD_MB_PER_MIN = chan.conf_num("BOARD_MB_PER_MIN", 90)
BOARD_SLOT_MIN = chan.conf_num("BOARD_SLOT_MINUTES", 15)
# what the board allows itself to pull in any rolling hour, mirrored from its
# own HOUR_MB. Only used to say how long a wait is, never to gate anything.
BOARD_HOUR_MB = chan.conf_num("BOARD_HOUR_MB", 2500)


def catalog_seconds(vid):
    """How long the requested video runs, from the catalogue that listed it."""
    for row in supply.read_catalog():
        if row[0] == vid:
            return row[1]
    return 7200


def board_busy_minutes():
    """Minutes the board still owes its own hourly cap before it may fetch.

    The board holds itself under HOUR_MB in any rolling hour and sits out the
    rest of it once it is over. Oracle cannot read that counter, but every
    delivery carries what it cost: the arrival epoch is the queue prefix and
    the size is on the disk. Leaving this out is what made the first ETA on
    2026-09-19 forty-five minutes short of the truth.
    """
    now, spent, newest = time.time(), 0.0, 0
    for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED):
        for path in chan.media(folder):
            head = path.name.split("-", 1)[0]
            if not head.isdigit() or now - int(head) > 3600:
                continue
            try:
                spent += path.stat().st_size / 1e6
            except OSError:
                continue
            newest = max(newest, int(head))
    if not newest or spent < BOARD_HOUR_MB:
        return 0
    return max(0, int((3600 - (now - newest)) / 60))


def fetch_eta(seconds):
    """Minutes before a video of this length is on the disk, roughly.

    Rough on purpose: the board may be mid-download, the line varies, and a
    number to the minute would be a promise nobody can keep. Rounded to five so
    it reads as the estimate it is.
    """
    rate = chan.read_json(chan.STATE / "want.json", {}).get("rate_bps") or 250_000
    minutes = (board_busy_minutes() + BOARD_SLOT_MIN
               + (seconds * rate / 1e6) / max(1.0, BOARD_MB_PER_MIN))
    return int(round(minutes / 5.0) * 5)


def air_eta():
    """Minutes before the hour on the wire ends and the next one is drawn."""
    live = playing()
    if not live or not chan.PART_SECONDS:
        return 0
    return max(0, int((chan.PART_SECONDS - live["elapsed"]) / 60))


def waiting_for(seconds):
    """The sentence a viewer gets when something has to be fetched first."""
    soon, then = fetch_eta(seconds), air_eta()
    return f"here in ~{soon} min, on air ~{soon + then} min" if then else f"here in ~{soon} min"


# --- channel points --------------------------------------------------------

# What the channel sells, and what each one does. The cost is a starting point;
# change it in the dashboard and nothing here cares. A reward is matched on its
# title, so renaming one in the dashboard unhooks it, which is the honest
# failure: better a reward that does nothing and refunds than one that does
# something nobody expected.
REWARDS = [
    {"key": "skip", "title": "Skip this hour", "cost": 150, "input": False,
     "description": "Ends the hour playing now and draws another one. "
                    "Refunded if the channel just skipped or has nothing else unseen."},
    {"key": "stay", "title": "Keep this one going", "cost": 150, "input": False,
     "description": "Another hour of the stream playing now, instead of moving on. "
                    "Refunded if this one has no hours left."},
    {"key": "pick", "title": "Pick what plays next", "cost": 250, "input": True,
     "description": "Type the number of a video from !list. "
                    "Refunded if the number is not on the shelf."},
    {"key": "request", "title": "Request a stream", "cost": 400, "input": True,
     "description": "Paste a YouTube link to a nanatty stream and it gets "
                    "fetched and played. It has to come from a channel this "
                    "rerun follows, so it can be checked first. "
                    "Points back otherwise."},
    {"key": "place", "title": "Take me somewhere", "cost": 300, "input": True,
     "description": "Type a country or a city: Japan, Turkey, Peru, India, "
                    "Korea, Chile, Argentina, Osaka, Cappadocia, Lima. The "
                    "next hour is filmed there, fetched if it is not here yet. "
                    "Points back if nothing matches."},
]
BY_TITLE = {r["title"].lower(): r for r in REWARDS}


def reward_skip(data, now, who, text):
    """(honoured, what to say). Points come back when the answer is no."""
    blocked = skip_blocked(data, now, playing())
    if blocked:
        return False, f"@{who} {blocked} — points refunded"
    do_skip(data, now, f"points from {who}")
    return True, f"@{who} spent points to move on"


def reward_pick(data, now, who, text):
    rows = shelf()
    try:
        index = int(re.sub(r"\D", "", text or "")) - 1
    except ValueError:
        return False, f"@{who} that is not a number from !list — points refunded"
    if not rows or not 0 <= index < min(5, len(rows)):
        return False, f"@{who} nothing on the shelf at that number — points refunded"
    PICK.write_text(json.dumps({"name": rows[index][0].name, "at": int(now)}))
    return True, f"@{who} picked {pretty(rows[index][0].name)[:48]} for next"


def reward_stay(data, now, who, text):
    """Another hour of what is on, which is the opposite of a skip.

    Nothing new is needed: the chat's pick lever names a file, and the draw
    takes an unaired hour from whatever it names. Naming the file already
    playing is how a viewer buys more of it.
    """
    live = playing()
    if not live:
        return False, f"@{who} nothing is playing right now — points refunded"
    book, durations = cut.ledger(), chan.read_json(chan.STATE / "durations.json", {})
    for folder in (chan.QUEUE, chan.AIRED):
        path = folder / live["name"]
        if path.exists() and cut.unaired(path, book, durations):
            PICK.write_text(json.dumps({"name": live["name"], "at": int(now)}))
            return True, f"@{who} bought another hour of {live['title'][:44]}"
    return False, f"@{who} this one has no hours left — points refunded"


# Titles name cities, not countries: "japan" matched six videos while Osaka
# alone had a hundred and three. Measured against the real catalogue on
# 2026-09-19, so a country asked for finds the streams shot in it. Ask for a
# city and you get that city; ask for a country and you get all of it.
# the table lives in chan.py: supply.py orders the candidate list with it too
PLACES = chan.PLACES


def place_terms(wanted):
    """Every spelling worth looking for, given what somebody typed."""
    return PLACES.get(wanted, (wanted,))


# every shape a viewer might paste, down to the bare id
LINK = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[\w=&]*&)?v=|live/|embed/|shorts/|v/)|^)"
    r"([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")


def video_asked(text):
    """The video id in whatever somebody pasted, or None."""
    found = LINK.search((text or "").strip())
    return found.group(1) if found else None


def waiting_requests(data):
    """The paid fetches the board still owes."""
    return [v for v, row in (data.get("asked") or {}).items()
            if row.get("state") != "here"]


def queue_request(data, now, who, vid, title):
    """Put a paid fetch in the board's way, or say why it will not fit.

    Refusing here costs the viewer nothing: the redemption has not been
    accepted yet, so False refunds it in the same second. Letting a fourth one
    in would cost them the points and six hours of silence, because the board
    fetches one video at a time under a ceiling the channel's own supply
    already spends most of.
    """
    held = waiting_requests(data)
    if vid in held:
        # overwriting the row would strand the first viewer's redemption in
        # Kick's queue for good, with their points gone and nobody to settle it
        return False, (f"@{who} {title[:40]} is already on its way for somebody "
                       f"else - points refunded")
    if len(held) >= REQUEST_MAX_PENDING:
        return False, (f"@{who} {len(held)} requests are already waiting on the "
                       f"board and it fetches one at a time - points refunded, "
                       f"try again when one lands")
    with supply.REQUESTS.open("a") as fh:
        fh.write(vid + "\n")
    data.setdefault("asked", {})[vid] = {"who": who, "title": title, "at": int(now),
                                         "state": "waiting", "redemption": None}
    # hand the id back to redeemed(), which alone knows it. Comparing the keys
    # of asked before and after cannot: a video already waiting is not a new
    # key, and the redemption behind it was then settled as if it were done.
    data["queued_now"] = vid
    return True, None


def reward_request(data, now, who, text):
    """A stream a viewer found on YouTube, fetched next.

    Only what the channel already lists as a source is accepted. Oracle cannot
    ask YouTube about a single video at all, the player API is walled from a
    datacenter address, so anything outside the catalogue could not be checked
    for length, shape or content before the board spent an hour on it. Inside
    the catalogue every one of those is already known.
    """
    vid = video_asked(text)
    if not vid:
        return False, f"@{who} that is not a YouTube link — points refunded"
    titles = supply.catalog_titles()
    if vid not in titles:
        return False, (f"@{who} that one is not in this channel's sources, "
                       f"so it cannot be checked before fetching — points refunded")
    if vid in supply.excluded(now):
        return False, f"@{who} {titles[vid][:40]} has already been on, or was refused"
    for path, _ in shelf():
        if chan.video_id(path) == vid:
            PICK.write_text(json.dumps({"name": path.name, "at": int(now)}))
            return True, f"@{who} it is already here: {titles[vid][:44]} plays next"
    queued, refusal = queue_request(data, now, who, vid, titles[vid])
    if not queued:
        return False, refusal
    return True, (f"@{who} fetching {titles[vid][:40]}: "
                  f"{waiting_for(catalog_seconds(vid))}")


def reward_place(data, now, who, text):
    """A stream shot somewhere in particular, from the disk or from the shelf's
    six hundred entries.

    Matching only what is downloaded made this unanswerable: the disk holds two
    files, so every country but those two was refused. The catalogue knows the
    other six hundred, so a place it has is either played next or fetched for
    it, and only a place nobody filmed comes back refunded.
    """
    wanted = re.sub(r"[^\w ]", "", text or "").strip().lower()
    if len(wanted) < 3:
        return False, f"@{who} name a place, like Thailand — points refunded"
    terms = place_terms(wanted)
    for path, _ in shelf():
        low = clean_title(path.name, SLUG).lower()
        if any(t in low for t in terms):
            PICK.write_text(json.dumps({"name": path.name, "at": int(now)}))
            return True, f"@{who} next up: {clean_title(path.name, SLUG)[:52]}"
    skip = supply.excluded(now)
    for vid, title in supply.catalog_titles().items():
        if any(t in title.lower() for t in terms) and vid not in skip:
            queued, refusal = queue_request(data, now, who, vid, title)
            if not queued:
                return False, refusal
            return True, (f"@{who} {wanted}: fetching {title[:38]}, "
                          f"{waiting_for(catalog_seconds(vid))}")
    return False, f"@{who} nothing from there in the library — points refunded"


ACTIONS = {"skip": reward_skip, "stay": reward_stay, "pick": reward_pick,
           "place": reward_place, "request": reward_request}


def redeemed(payload):
    """One reward redemption: do it or refund it, and say which in chat."""
    if (payload.get("status") or "").lower() not in ("", "pending"):
        return
    reward = payload.get("reward") or {}
    known = BY_TITLE.get((reward.get("title") or "").strip().lower())
    person = payload.get("redeemer") or {}
    who = re.sub(r"[^\w .-]", "", str(person.get("username") or ""))[:25]
    if not known:
        return
    data = load()
    data.pop("queued_now", None)
    honoured, answer = ACTIONS[known["key"]](
        data, time.time(), who, payload.get("user_input") or "")
    # A video the board has not brought back yet: accepting now would take the
    # points for a delivery the daily ceiling may still swallow, so the
    # redemption stays in Kick's queue and announce_arrivals settles it either
    # way, which is the only path that can still refund.
    queued = data.pop("queued_now", None)
    if queued:
        data["asked"][queued]["redemption"] = payload.get("id")
    save(data)
    if payload.get("id") and not queued:
        kickapi.settle_redemption(payload["id"], honoured)
    if answer:
        say(answer)
    chan.log(f"recompense {known['key']} par {who}: "
             + ("en attente de la carte" if queued
                else "honoree" if honoured else "remboursee"))


def check_rewards():
    """Nothing here may be silently cut in half by the API's own limit."""
    too_long = [r["title"] for r in REWARDS
                if len(r["description"]) > kickapi.DESCRIPTION_MAX]
    if too_long:
        chan.log(f"descriptions trop longues, elles seraient tronquees: {too_long}")
    return not too_long


def sync_rewards(update=False):
    """Create what is missing. Only push costs when asked to.

    A restart must not undo a price somebody set in the dashboard, so the
    default is create-and-leave-alone. `bot.py --rewards` is the deliberate act
    that makes this file the source of truth again.
    """
    check_rewards()
    have = {(r.get("title") or "").strip().lower(): r for r in kickapi.rewards()}
    for spec in REWARDS:
        found = have.get(spec["title"].lower())
        if not found:
            done = kickapi.create_reward(spec["title"], spec["cost"],
                                         spec["description"], spec["input"])
            chan.log(f"recompense {'creee' if done else 'refusee'}: "
                     f"{spec['title']} ({spec['cost']} points)")
        elif update and (found.get("cost") != spec["cost"]
                         or (found.get("description") or "") != spec["description"]):
            done = kickapi.update_reward(found["id"], spec["cost"], spec["description"])
            chan.log(f"recompense {'ajustee' if done else 'inchangee'}: "
                     f"{spec['title']} {found.get('cost')} -> {spec['cost']}")


# --- the title -------------------------------------------------------------

def wanted_title():
    """The line the channel carries, with the handle always last.

    kil, 2026-09-19: the handle is not decoration, it has to be at the end of
    every title the channel ever shows. So it is appended after the truncation
    and never inside it: what gets cut when the name is long is the name.
    """
    live = playing()
    if not live:
        return None
    # a Kick part carries the whole stream's title, so the title has to say
    # which part it is or two of them are the same line on the channel
    part = re.match(r"^k[0-9a-f]{8}(\d{2})$", live["vid"] or "")
    piece = f" · part {int(part.group(1))}" if part else ""
    piece += f" · hour {live['hour']}/{live['hours']}" if live["hours"] > 1 else ""
    tail = f"{piece} · !vote to skip"
    suffix = chan.CONF.get("TITLE_SUFFIX", "").strip()
    if suffix:
        tail = f"{tail} · {suffix}"
    room = max(20, 138 - len(tail))
    head = clean_title(live["name"], SLUG)
    if len(head) > room:
        head = head[:room - 1].rstrip(" -.,") + "\u2026"
    return f"{head}{tail}"


def keep_title():
    """Follow what the cutter is playing, and say it on the channel.

    Only when it has really changed and never more often than the interval: a
    title rewritten every poll is a call to the API every poll, and the channel
    gains nothing from it.
    """
    checked = 0
    while True:
        try:
            data = load()
            want = wanted_title()
            now = time.time()
            # Every few minutes, believe the channel rather than our own memory.
            # Comparing a wanted title to the last one we think we set means a
            # failed call, or somebody editing the title by hand, is never
            # noticed: the channel then says one thing while playing another.
            if want and now - checked > TITLE_CHECK_INTERVAL:
                checked = now
                if kickapi.live_title(SLUG) not in ("", want):
                    data["title"] = ""
            if (want and want != data.get("title")
                    and now - data.get("title_at", 0) > TITLE_MIN_INTERVAL):
                if kickapi.set_title(want):
                    data["title"], data["title_at"] = want, now
                    save(data)
                    chan.log(f"titre: {want}")
            announce(data)
            announce_arrivals(data)
            save(data)
            close_stale_vote(data, now)
        except Exception as problem:
            chan.log(f"boucle titre: {problem}")
        time.sleep(20)


def announce(data):
    """Say what changed, because a channel that never speaks is a black box.

    One line when the hour on the wire changes, so at most once an hour and
    once per skip, and one line the first time the shelf runs dry. What was
    said is remembered, so a restart does not repeat it.
    """
    live = playing()
    mark = f"{live['name']}#{live['hour']}" if live else "filler"
    if mark == data.get("said_playing"):
        return
    data["said_playing"] = mark
    if not live:
        say("nothing unseen left on the shelf, the next stream is on its way")
        return
    piece = f" (hour {live['hour']}/{live['hours']})" if live["hours"] > 1 else ""
    say(f"now playing: {clean_title(live['name'], SLUG)}{piece} · !vod !list !vote")


def settle_request(row, honoured):
    """Take the points, or give them back. Silent for a request made by hand."""
    if row.get("redemption"):
        kickapi.settle_redemption(row["redemption"], honoured)


def announce_arrivals(data):
    """Follow a paid request to its end: here, on air, or refunded.

    kil, 2026-09-19: a request rides on top of the supply the channel already
    needs, so the board's ceilings can swallow one. The old code dropped it
    after a day without a word and with the points already spent, which is the
    worst of the three things it could have done.
    """
    asked = data.get("asked") or {}
    if not asked:
        return
    now = time.time()
    here = {chan.video_id(p) for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED)
            for p in chan.media(folder)}
    landing = {chan.video_id(p) for p in chan.media(chan.UPLOAD)}
    live = playing()
    barred = None
    for vid in list(asked):
        row = asked[vid]
        who, title = row.get("who", "someone"), str(row.get("title"))[:40]
        if row.get("state") == "here":
            # it is on the disk and picked; the only thing left to say is that
            # it is actually going out, which is the thing that was paid for
            if live and live["vid"] == vid:
                say(f"@{who} the stream you asked for is on now: {title}")
                asked.pop(vid)
            elif now - row.get("at", 0) > REQUEST_DEADLINE:
                asked.pop(vid)  # still on the disk, it will come round by itself
            continue
        if vid in here:
            for path, _ in shelf():
                if chan.video_id(path) == vid:
                    # it was paid for, so it does not take its chances in the draw
                    PICK.write_text(json.dumps({"name": path.name, "at": int(now)}))
                    break
            settle_request(row, True)
            say(f"@{who} the stream you asked for landed: {title} "
                f"- on next, in ~{air_eta()} min")
            row["state"], row["at"] = "here", int(now)
            continue
        if vid in landing:
            continue  # mid-transfer from the board, not a failure
        barred = supply.excluded(now) if barred is None else barred
        if vid in barred:
            settle_request(row, False)
            say(f"@{who} {title} cannot be fetched after all - points refunded")
            asked.pop(vid)
        elif now - row.get("at", 0) > REQUEST_DEADLINE:
            settle_request(row, False)
            say(f"@{who} {title} did not arrive within "
                f"{REQUEST_DEADLINE // 3600} h, the board is at its daily "
                f"ceiling - points refunded")
            asked.pop(vid)
    data["asked"] = asked


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
        wanted = [("chat.message.sent", 1), ("channel.reward.redemption.updated", 1)]
        missing = [e for e in wanted if e[0] not in names]
        if missing:
            if kickapi.subscribe(BROADCASTER, missing):
                chan.log(f"abonnements crees: {[e[0] for e in missing]}")
            else:
                chan.log(f"abonnements refuses: {[e[0] for e in missing]}")
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
            # Somebody opened the landing page instead of the door. Kick sends
            # people here, nobody starts here, and "no code" told them nothing:
            # hand them the link they actually needed.
            link = html.escape(kickapi.authorize_url(kickapi.APP.get(
                "KICK_REDIRECT_URI",
                "https://vodloop.kicknosubviewer.duckdns.org/kick/callback")))
            page = ("<html><body style='font-family:sans-serif;max-width:40em;margin:3em auto'>"
                    "<h3>Rien a faire sur cette page</h3><p>C'est l'adresse ou Kick renvoie "
                    "apres autorisation. Pour autoriser le bot, ouvrez ce lien, connecte sur "
                    "le compte de la chaine :</p>"
                    f"<p><a href='{link}'>Autoriser le bot sur Kick</a></p></body></html>")
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        ok, message = kickapi.exchange(code, state)
        chan.log(f"callback oauth: {message}")
        if ok:
            ensure_subscription()
            sync_rewards()
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
        kind = self.headers.get("Kick-Event-Type")
        if kind not in ("chat.message.sent", "channel.reward.redemption.updated"):
            return
        if BROADCASTER and int((payload.get("broadcaster") or {}).get("user_id") or 0) != BROADCASTER:
            return
        message_id = payload.get("message_id") or ""
        data = load()
        if message_id and message_id in data["seen"]:
            return
        data["seen"].append(message_id)
        save(data)
        if kind == "chat.message.sent":
            handle(payload)
        else:
            redeemed(payload)


def main(argv):
    chan.STATE.mkdir(parents=True, exist_ok=True)
    if "--authorize" in argv:
        redirect = kickapi.APP.get(
            "KICK_REDIRECT_URI", "https://vodloop.kicknosubviewer.duckdns.org/kick/callback")
        print(kickapi.authorize_url(redirect))
        return 0
    if "--rewards" in argv:
        sync_rewards(update=True)
        for r in kickapi.rewards():
            print(f"  {r.get('title')}  {r.get('cost')} points  "
                  f"{'actif' if r.get('is_enabled') else 'inactif'}")
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
        sync_rewards()
    else:
        chan.log("pas de jeton: lancer bot.py --authorize et ouvrir l'URL")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    chan.log(f"bot a l'ecoute sur 127.0.0.1:{PORT} pour {SLUG}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
