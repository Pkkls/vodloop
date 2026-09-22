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
    !now             what is on, which hour of it, how long is left
    !list            what is on the shelf, numbered
    !pick <n>        vote for what plays next
    !skip            vote to move on now
    !fetch           what is downloading, for whom, and how long it has left
    !link            where this video comes from
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
import urllib.request
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
# A skip does not cost one hour, it destroys whatever is left of the hour on
# air: that hour is spent in the ledger the moment its first chunk goes out,
# and nothing spent is ever shown again. Skipping ten minutes in throws away
# fifty. At three an hour that is 3.5 h of reserve consumed per hour of wall
# clock, against a board that delivers about one. The old limit was a count,
# which prices a skip at minute 55 the same as one at minute 10.
SKIP_FLOOR = int(chan.conf_num("SKIP_FLOOR_HOURS", 3)) * 3600
SKIP_WASTE_CAP = int(chan.conf_num("SKIP_WASTE_MINUTES_PER_HOUR", 60)) * 60
# over how many hours the reserve above the floor may be spent. A fixed budget
# is wrong at both ends: 30 min an hour forbade every skip before minute 30
# whatever the shelf held, and anything generous enough to be useful with 12 h
# in hand would empty a shelf holding 4.
SKIP_SPREAD_HOURS = int(chan.conf_num("SKIP_SPREAD_HOURS", 6))
USER_SKIP_COOLDOWN = int(chan.conf_num("USER_SKIP_COOLDOWN_SECONDS", 3600))
REQUEST_MAX_PENDING = int(chan.conf_num("REQUEST_MAX_PENDING", 3))
JUMP_SECONDS = int(chan.conf_num("JUMP_MINUTES", 10)) * 60
# kil, 2026-09-22, from the night it happened: one request for a twelve hour
# video held the board for three hours, starved its own beacon, raised the
# alarm that says the card is dead, and left the channel draining with three
# more requests queued behind it. The board fetches one video at a time and
# that is the whole supply line, so the length of what the chat may ask for is
# not a matter of taste. It is only affordable when the reserve is deep enough
# to spend hours on one delivery.
REQUEST_MAX_SECONDS = int(chan.conf_num("REQUEST_MAX_HOURS", 6)) * 3600
REQUEST_RICH_SECONDS = int(chan.conf_num("REQUEST_RICH_HOURS", 12)) * 3600
REQUEST_DEADLINE = int(chan.conf_num("REQUEST_DEADLINE_HOURS", 6)) * 3600
# Every path that settles a redemption runs inside one webhook call, so a
# crash, a dead socket or a restart in the wrong second leaves it pending in
# Kick's queue with the points already taken. Five minutes is far past any
# answer this bot takes to give, and well short of a viewer wondering.
STRAY_AFTER = int(chan.conf_num("STRAY_REDEMPTION_SECONDS", 300))
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
    # the title a viewer reads here is the one the channel carries, not the
    # uploader's file name: !now said "260203 nanatty - Day 17 IRL Ushuaia"
    return {"name": live["source"], "title": clean_title(live["source"], SLUG),
            "hour": number // cut.per_slice() + 1,
            "hours": cut.slices_in(seconds) if seconds else 1,
            "started": started,
            "elapsed": max(0.0, time.time() - started) if started else 0.0,
            "vid": chan.video_id(live["source"]) or ""}


def shelf():
    """Files that still hold time nobody has seen: queue, the one being cut, reserve.

    The second value is hours, rounded down, because that is what the chat
    counts in. The ledger counts in chunks, so a file holding forty unseen
    minutes reads as nought hours here and is still perfectly drawable.

    current/ was missing until 2026-09-22 and it is where a delivery sits for
    the whole time it is on air. Leaving it out told the chat there was less to
    watch than there was, refused skips against a floor the channel was above,
    and answered "this is the only stream left" while another one was being
    cut. The same hole was in supply's runway, where it also told the board to
    hurry.
    """
    book = cut.ledger()
    durations = chan.read_json(chan.STATE / "durations.json", {})
    held, out = cut.reserved(), []
    for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED):
        for path in chan.media(folder):
            free = cut.unaired(path, book, durations, held)
            if free:
                out.append((path, len(free) * cut.unit_seconds() / 3600.0))
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
    data["skips"] = [r for r in skips_since(data, cutoff)]
    data["users"] = {u: t for u, t in data["users"].items() if t > time.time() - 3600}
    chan.write_json(STATE, data)


def say(text, reply_to=None):
    """One message, never two within a second and a half of each other."""
    with _lock:
        wait = 1.5 - (time.time() - _last_said[0])
        if wait > 0:
            time.sleep(wait)
        _last_said[0] = time.time()
    relay(f"[bot] {text}")
    return kickapi.say(BROADCASTER, text, reply_to)


# --- the chat, mirrored on Telegram ----------------------------------------

# kil, 2026-09-21: "met les logs du chat dans le bot telegramme, et tu me mets
# la possibilite de repondre via telegram au chat". Every channel shares one
# Telegram bot, and Telegram hands its long poll to a single reader, so only
# the channel carrying TG_POLL=1 listens; the others only write.
TG_TOKEN = chan.CONF.get("TG_TOKEN", "")
TG_CHAT = str(chan.CONF.get("TG_CHAT", ""))
TG_POLL = chan.CONF.get("TG_POLL", "0") == "1"
TG_FLUSH = int(chan.conf_num("TG_RELAY_SECONDS", 15))
OFFSET = chan.STATE / "tg.offset"

_relay, _relay_lock = [], threading.Lock()


def relay(line):
    """Hold one chat line for Telegram, which is written to in batches.

    Telegram takes about twenty messages a minute into one chat and drops the
    rest; a busy minute of Kick chat is more than that, so the lines wait and
    leave together. Old lines fall off the end rather than pile up if Telegram
    is unreachable.
    """
    if not TG_TOKEN or not TG_CHAT:
        return
    with _relay_lock:
        _relay.append(re.sub(r"\s+", " ", line)[:300])
        del _relay[:-60]


def relay_loop():
    while True:
        time.sleep(TG_FLUSH)
        with _relay_lock:
            lines, _relay[:] = list(_relay), []
        if lines:
            chan.telegram("\n".join(lines))


def tg_updates(offset):
    query = urllib.parse.urlencode({"offset": offset, "timeout": 50,
                                    "allowed_updates": '["message"]'})
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?{query}"
    with urllib.request.urlopen(url, timeout=70) as response:
        return json.load(response).get("result") or []


def tg_reply(message):
    """One Telegram message out on Kick, under the channel's own name."""
    if str((message.get("chat") or {}).get("id")) != TG_CHAT:
        return  # somebody else found the bot; it answers to one room only
    text = (message.get("text") or "").strip()
    if text:
        say(text[:400])


def tg_loop():
    while True:
        try:
            offset = int(OFFSET.read_text().strip() or 0)
        except (OSError, ValueError):
            offset = 0
        try:
            for update in tg_updates(offset):
                offset = int(update.get("update_id", 0)) + 1
                # the offset is stored before the message is acted on: a crash
                # on one message must not replay it on every restart
                OFFSET.write_text(str(offset))
                tg_reply(update.get("message") or {})
        except Exception as problem:
            chan.log(f"telegram: {problem}")
            time.sleep(20)


# --- the guards ------------------------------------------------------------

def skip_cost(live):
    """Seconds of unseen material a skip right now would throw away.

    The hour on air is already spent, so what a skip destroys is the part of
    it nobody will ever see. Late in the hour that is nearly nothing, early in
    it that is nearly the whole hour, and pricing both the same is what let a
    handful of votes drain a day of supply.
    """
    if not live or not chan.PART_SECONDS:
        return 0.0
    return max(0.0, chan.PART_SECONDS - live.get("elapsed", 0))


def waste_allowance(reserve):
    """Seconds of unseen material the chat may destroy in an hour.

    Derived from what the channel is holding rather than fixed: nothing at the
    floor, and an hour's worth once the shelf is deep enough that losing it
    costs nobody a loading card. Between the two it is the spare reserve
    spread over SKIP_SPREAD_HOURS, so the closer the shelf gets to the floor
    the less a vote may take, and it reaches zero before the floor does.
    """
    spare = max(0.0, reserve - SKIP_FLOOR)
    return min(float(SKIP_WASTE_CAP), spare / max(1, SKIP_SPREAD_HOURS))


def skips_since(data, when):
    """The skips recorded after a moment, in the shape they are stored now.

    Older state holds bare timestamps, so those read as a skip of unknown cost
    rather than being dropped: forgetting them would hand out a fresh budget
    every time the bot is deployed.
    """
    out = []
    for row in data.get("skips", []):
        if isinstance(row, dict):
            if row.get("at", 0) > when:
                out.append(row)
        elif row > when:
            out.append({"at": row, "cost": float(chan.PART_SECONDS or 0), "who": ""})
    return out


def wasted_recently(data, now):
    return sum(float(r.get("cost", 0)) for r in skips_since(data, now - 3600))


def other_stream_hours(live):
    """Unseen hours that belong to a stream other than the one on air.

    kil, 2026-09-21: "on stuck en boucle si y'a du !force". Sliced by the hour,
    a skip with nothing else unseen lands on the next hour of the same
    recording, so the channel looks like it ignored the skip and the chat asks
    again. A skip that cannot change the stream is refused and said so.
    """
    if not live:
        return unseen_hours()
    mine = cut.recording_of(live["name"])
    return sum(hours for path, hours in shelf()
               if cut.recording_of(path) != mine)


def skip_blocked(data, now, live, who=None):
    """Why a skip cannot happen now, or None. Checked for votes and for !force.

    Three guards, in the order that matters. The floor is the promise: the
    reserve left after this skip must still carry the channel until the board
    delivers again, so no amount of voting can reach the standby clip. The
    waste budget shapes how that reserve is spent, in minutes destroyed rather
    than in skips counted, so a late skip is nearly free and an early one is
    not. The per viewer budget is the answer to one person carrying every vote,
    which is exactly what a threshold of one allows.
    """
    reserve = unseen_hours() * 3600
    cost = skip_cost(live)
    if reserve < 3600:
        return four("nothing_else")
    if other_stream_hours(live) < 0.5:
        return four("only_stream")
    if reserve - cost < SKIP_FLOOR:
        return four("not_enough")
    allowed = waste_allowance(reserve)
    spent = wasted_recently(data, now)
    if spent + cost > allowed:
        if spent:
            return four("too_much_skipped")
        # nothing was skipped this hour, so what is expensive is this skip:
        # most of the hour is still unseen and it would all be thrown away
        return four("too_early")
    recent = skips_since(data, now - 3600)
    if len(recent) >= SKIP_MAX_PER_HOUR:
        return four("skip_limit", n=len(recent))
    last = max((r["at"] for r in recent), default=0)
    if last and now - last < SKIP_COOLDOWN:
        left = int((SKIP_COOLDOWN - (now - last)) / 60) + 1
        return four("just_skipped", min=left)
    if live and live["elapsed"] < SKIP_MIN_AIRED:
        left = int((SKIP_MIN_AIRED - live["elapsed"]) / 60) + 1
        return four("just_started", min=left)
    if who:
        mine = [r for r in skips_since(data, now - USER_SKIP_COOLDOWN)
                if r.get("who") == who]
        if mine:
            left = int((USER_SKIP_COOLDOWN - (now - max(r["at"] for r in mine))) / 60) + 1
            return four("your_turn_over", min=left)
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


def do_skip(data, now, reason, live=None, who=""):
    """Move on, and write down what it cost and who asked, because both are
    what the guards read next time."""
    SKIP.write_text(json.dumps({"at": int(now), "reason": reason}))
    data["skips"].append({"at": now, "cost": skip_cost(live), "who": who or ""})
    data["vote"] = None


# --- what a viewer reads, in the four languages the channel is watched in ---

# kil, 2026-09-21: "les commandes on ne comprend pas, faut une reponse dans
# chaque langue". A title, a number and a command name read the same in every
# language, so the data in an answer is said once and the words around it four
# times: English, Spanish, Japanese, Turkish, the order the rewards use. Kick
# takes 500 characters in a chat line and a wall of text is its own kind of
# unreadable, so each phrase is written short enough that four of them and the
# data still fit inside one answer.
SAID = {
    "more_asked": ("{n} more asked", "{n} más pedidos",
                   "他{n}件", "{n} tane daha"),
    "landed_waiting": ("{n} landed, waiting its turn", "{n} llegó, esperando turno",
                       "{n}件到着、順番待ち", "{n} geldi, sırada"),
    "then_on_air": ("on air ~{min} min", "en directo ~{min} min",
                    "放送は約{min}分後", "yayında ~{min} dk"),
    "last_landed": ("last landed {min} min ago", "el último hace {min} min",
                    "最終到着{min}分前", "son {min} dk önce"),
    "help": ("playing, list, choose, skip, downloads, source", "sonando, lista, elegir, saltar, descargas, fuente",
            "再生中 / 一覧 / 選ぶ / スキップ / 取得中 / 元動画", "çalan, liste, seç, atla, indirilenler, kaynak"),
    "short_playing": ("a short while the next stream downloads",
                      "un short mientras baja el siguiente",
                      "次の配信の取得中、ショート再生", "sonraki yayın inerken bir short"),
    "nothing_on": ("nothing on air right now", "nada en directo ahora",
                  "今は配信なし", "şu anda yayın yok"),
    "nothing_ready": ("nothing ready yet, downloading more",
                      "nada listo aún, descargando más",
                      "まだ準備中です、取得しています", "henüz hazır yok, indiriliyor"),
    "no_link": ("no link for this one", "sin enlace para este",
                "この配信のリンクはありません", "bunun için bağlantı yok"),
    "own_vod": ("no link, this is one of the channel's own VODs",
                "sin enlace, es un VOD del propio canal",
                "リンクなし、チャンネル自身のVODです",
                "bağlantı yok, kanalın kendi VOD'u"),
    "moving_on": ("ok, moving on", "ok, pasamos a otro", "了解、次へ", "tamam, geçiyoruz"),
    "is_next": ("next", "siguiente", "次", "sırada"),
    "vote_running": ("a vote is already running", "ya hay una votación",
                     "すでに投票中です", "zaten bir oylama var"),
    "vote_failed": ("last vote failed, {min} min", "la votación falló, {min} min",
                   "不成立、{min}分", "oylama düştü, {min} dk"),
    "to_skip": ("to skip", "para saltar", "スキップに", "atlamak için"),
    "vote_needs": ("{need} votes to skip, type !skip", "{need} votos, escribe !skip",
                   "{need}票でスキップ、!skipと入力", "{need} oy gerek, !skip yaz"),
    "vote_play": ("{need} votes to play it, type !pick {n}",
                  "{need} votos, escribe !pick {n}",
                  "{need}票で再生、!pick {n}と入力", "{need} oy gerek, !pick {n} yaz"),
    "nothing_to_pick": ("nothing to pick from yet", "nada para elegir aún",
                        "まだ選べるものがありません", "henüz seçecek bir şey yok"),
    "pick_usage": ("!pick <number> from !list", "!pick <número> de !list",
                  "!list の番号で !pick", "!list numarasıyla !pick"),
    "pick_range": ("pick 1 to {max}, see !list", "elige de 1 a {max}, mira !list",
                   "1〜{max} から選択、!list参照", "1 ile {max} arası seç, !list"),
    "only_stream": ("the only stream left, a skip lands on it", "es el único que queda",
                   "残りはこれだけ", "kalan tek yayın bu"),
    "nothing_else": ("nothing else ready", "nada más listo", "他に準備なし", "başka hazır yok"),
    "not_enough": ("little left in the library", "queda poco", "残りが少ない", "az kaldı"),
    "too_much_skipped": ("too many skips this hour", "demasiados saltos esta hora",
                        "この1時間は多すぎ", "bu saatte çok atlandı"),
    "too_early": ("too early to skip this one", "muy pronto para saltar",
                 "始まったばかり", "atlamak için erken"),
    "skip_limit": ("{n} skips this hour, that is the limit",
                   "{n} saltos esta hora, es el límite",
                   "この1時間で{n}回、上限です", "bu saatte {n} atlama, sınır bu"),
    "just_skipped": ("just skipped, {min} min", "recién saltado, {min} min",
                    "さっきスキップ、{min}分", "az önce atlandı, {min} dk"),
    "just_started": ("just started, vote in {min} min", "acaba de empezar, {min} min",
                    "始まったばかり、{min}分", "yeni başladı, {min} dk"),
    "your_turn_over": ("your last skip, {min} min for someone else", "tu último salto, {min} min",
                      "前回はあなた、{min}分", "son atlama sendeydi, {min} dk"),
    "downloading": ("downloading", "descargando", "取得中", "indiriliyor"),
    "already_coming": ("already coming", "ya viene", "取得中", "zaten geliyor"),
    "one_each": ("you have one coming already", "ya tienes uno en camino",
                "すでに1件取得中", "zaten bir tane geliyor"),
    "queue_full": ("{n} downloading already, wait for one", "{n} descargando, espera",
                  "{n}件取得中、お待ちを", "{n} iniyor, bekle"),
    "too_long": ("{hours} h is too long while the channel is short, pick a shorter one",
                 "{hours} h es mucho ahora, elige uno más corto",
                 "{hours}時間は今は長すぎます、短いものを",
                 "{hours} saat şimdilik çok uzun, kısa birini seç"),
    "nothing_asked": ("nothing asked for right now", "nada pedido ahora mismo",
                      "今リクエストはありません", "şu anda istek yok"),
    "points_back": ("points back", "puntos devueltos", "ポイント返却", "puan iade"),
    "broke": ("that one broke, points back", "eso falló, puntos devueltos",
             "エラーです、ポイント返却", "hata oldu, puan iade"),
    "not_a_number": ("not a number from !list", "no es un número de !list",
                    "!list の番号では", "!list numarası değil"),
    "no_video_there": ("no video at that number", "no hay vídeo ahí",
                      "その番号はなし", "o numarada yok"),
    "already_shown": ("already been on", "ya emitido", "放送済み", "yayınlandı"),
    "name_a_place": ("name a place, like Thailand", "di un lugar, como Tailandia",
                    "場所を（例: タイ）", "bir yer yaz, Tayland gibi"),
    "nothing_there": ("nothing filmed there", "nada filmado allí",
                     "そこの配信はなし", "orada çekilmiş yok"),
    "place_coming": ("{place} is already coming", "{place} ya viene",
                    "{place}は取得中", "{place} zaten geliyor"),
    "one_more_hour": ("one more hour of it", "una hora más",
                      "もう1時間続けます", "bir saat daha"),
    "no_hours_left": ("no hours left on this one", "no quedan horas",
                     "残りはありません", "kalan saat yok"),
    "forward": ("{min} min forward", "{min} min adelante",
                "{min}分進みました", "{min} dk ileri"),
    "not_cut_ahead": ("less than {min} min ready ahead, try later", "menos de {min} min listos, prueba luego",
                     "先が{min}分未満、後で", "{min} dk'dan az hazır"),
    "ask_with": ("!list then !pick <n> to ask for one",
                 "!list y luego !pick <n> para pedir",
                 "!list のあと !pick <n> でリクエスト",
                 "!list sonra !pick <n> ile iste"),
    "ready_here": ("ready", "listos", "準備済み", "hazır"),
    "never_shown": ("never shown", "nunca emitido", "未放送", "hiç yayınlanmadı"),
    "aired": ("aired", "emitido", "放送済み", "yayınlandı"),
}


def four(key, **data):
    """One answer, said in the four languages the channel is watched in."""
    return " · ".join(part.format(**data) for part in SAID[key])


# --- commands --------------------------------------------------------------

def cmd_aide(*_):
    return "!now !list !pick <n> !skip !fetch !link · " + four("help")


def cmd_vod(*_):
    live = playing()
    if not live:
        # the wire is never silent: when it is not the channel's material it is
        # a short, and a viewer arriving then deserves better than "nothing on"
        if chan.read_json(feed.ONAIR, {}).get("short"):
            return four("short_playing")
        return four("nothing_on")
    left = max(0, chan.PART_SECONDS - live["elapsed"]) if chan.PART_SECONDS else 0
    # hours and minutes as numbers: a viewer reads 3/9 in any language
    piece = f" · {live['hour']}/{live['hours']}" if live["hours"] > 1 else ""
    tail = f" · {int(left / 60)} min" if left else ""
    return f"{live['title']}{piece}{tail}"


LIST_PAGE = int(chan.conf_num("LIST_PAGE", 8))


def pickable():
    """(ready, fetchable): what is on the disk, then what the board can bring.

    kil, 2026-09-21: "ca doit avoir des dizaines de videos disponibles, pas
    uniquement 2 ou 3". Under a 28 Go share the disk holds one or two videos at
    a time, so a list of the disk is a list of two, and it was. The catalogue
    holds five hundred and the board can put any of them on the wire within the
    hour, so the list is the catalogue with what is already here at its head.

    Both halves carry (id, file name or None, title, seconds), so one number
    from !list means one row here whichever half it falls in.
    """
    ready = [(chan.video_id(path), path.name, clean_title(path.name, SLUG), 0)
             for path, _ in shelf()]
    here = {vid for vid, _, _, _ in ready}
    # the catalogue carries the uploader's own file name, so it goes through the
    # same cleaning as the title on air rather than spending half a chat line on
    # a date prefix and the channel's own name
    return ready, [(vid, None, clean_title(title, SLUG) if title else vid, seconds)
                   for vid, seconds, title in supply.candidates() if vid not in here]


def cmd_liste(data, now, sender, args):
    ready, pool = pickable()
    rows = ready + pool
    if not rows:
        return four("nothing_ready")
    pages = (len(rows) - 1) // LIST_PAGE + 1
    try:
        page = min(max(1, int(args[0])), pages)
    except (IndexError, ValueError):
        page = 1
    start = (page - 1) * LIST_PAGE
    listing = " · ".join(f"{start + n + 1} {title[:34]}"
                         for n, (_, _, title, _) in enumerate(rows[start:start + LIST_PAGE]))
    more = f" · !list {page + 1} of {pages}" if page < pages else ""
    return f"{len(rows)} videos: {listing}{more} · !pick <n>"


def cmd_source(*_):
    live = playing()
    if not live:
        return four("nothing_on")
    if not live["vid"]:
        return four("no_link")
    if re.match(r"^k[0-9a-f]{8}\d{2}$", live["vid"]):
        return four("own_vod")
    return f"https://youtu.be/{live['vid']}"


def cmd_stats(*_):
    book = cut.ledger()
    hours = sum(len(v) for v in book.values())
    rows = shelf()
    return (f"{len(rows)} · " + four("ready_here") + f" | {unseen_hours():.1f}h · "
            + four("never_shown") + f" | {hours}h · " + four("aired"))


def last_arrival_minutes(now):
    """Minutes since the board last put a file on this disk, 0 if none has."""
    newest = 0
    for folder in (chan.QUEUE, chan.CURRENT, chan.AIRED):
        for path in chan.media(folder):
            head = path.name.split("-", 1)[0]
            if head.isdigit():
                newest = max(newest, int(head))
    return int((now - newest) / 60) if newest else 0


def cmd_fetch(data, now, sender, args):
    """What the board is bringing, whose it is, and how long it has to run.

    kil, 2026-09-21: "une commande !fetch qui montre la queue". The board is
    behind NAT and says nothing about what it is downloading this second, so
    what can honestly be shown is what was asked of it, what it has already
    landed, and how long ago the last delivery was. A wait quoted here is the
    same estimate the reward gives, from the board's own measured rate.
    """
    asked = data.get("asked") or {}
    coming = [(vid, row) for vid, row in asked.items() if row.get("state") != "here"]
    landed = [row for row in asked.values() if row.get("state") == "here"]
    # rows written before the chat picked from the catalogue hold the uploader's
    # own file name, so the cleaning happens here and covers those too
    shown = " · ".join(f"{clean_title(str(row.get('title') or vid), SLUG)[:26]} "
                       f"@{row.get('who', '?')} ~{fetch_eta(catalog_seconds(vid))} min"
                       for vid, row in coming[:3])
    more = " · " + four("more_asked", n=len(coming) - 3) if len(coming) > 3 else ""
    if landed:
        more += " · " + four("landed_waiting", n=len(landed))
    since = last_arrival_minutes(now)
    tail = f"{unseen_hours():.1f}h · " + four("ready_here")
    if since:
        tail += " · " + four("last_landed", min=since)
    if not coming:
        return four("nothing_asked") + f" · {tail} · " + four("ask_with")
    return f"{len(coming)} " + four("downloading") + f" · {shown}{more} · {tail}"


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
                return f"{pretty(vote['target'])} · " + four("is_next")
            do_skip(data, now, "vote", playing(), vote["voters"][0])
            return four("moving_on")
        return f"{len(vote['voters'])}/{need} · " + four("to_skip")
    if vote and now - vote.get("failed_at", 0) < 0:
        return None
    last_fail = data.get("vote_failed_at", 0)
    if now - last_fail < VOTE_FAIL_COOLDOWN:
        left = int((VOTE_FAIL_COOLDOWN - (now - last_fail)) / 60) + 1
        return four("vote_failed", min=left)
    blocked = skip_blocked(data, now, live, sender["user_id"])
    if blocked:
        return blocked
    need = threshold()
    data["vote"] = {"kind": "skip", "target": None, "voters": [sender["user_id"]],
                    "need": need, "closes": now + VOTE_WINDOW}
    if need <= 1:
        do_skip(data, now, "vote", live, sender["user_id"])
        return four("moving_on")
    return four("vote_needs", need=need)


def play_short():
    """Ask the feeder for a short at the next junction, if any are ready.

    kil, 2026-09-21: "lorsque ca vote pour une video, tu mets un short viral".
    A fetch takes the best part of an hour, and the chat that just paid for it
    has nothing to look at in the meantime. The marker is only written when
    there is something to play, so an empty shorts directory changes nothing.
    """
    if any(chan.SHORTS.glob("*.ts")):
        feed.SHORT.write_text(str(int(time.time())))
        return True
    return False


def ask_for(data, now, sender, row):
    """Put a video the chat picked, and that is not here, in the board's way.

    kil, 2026-09-21: "une nouvelle video doit etre ajoutee si on utilise un
    vote". Picking something the channel does not hold is what adds it, and it
    costs nothing. The guards are the ones the paid request already needed: the
    board fetches one video at a time under a ceiling the channel's own supply
    mostly spends, so three waiting is the cap, and one per viewer stops a
    single person holding all three.
    """
    vid, _, title, seconds = row
    if too_long_for_now(seconds):
        return four("too_long", hours=seconds // 3600)
    held = waiting_requests(data)
    asked = data.get("asked") or {}
    if vid in held:
        return f"{title[:40]} · " + four("already_coming") + f" {waiting_for(seconds)}"
    if any(asked.get(v, {}).get("who") == sender["name"] for v in held):
        return four("one_each")
    if len(held) >= REQUEST_MAX_PENDING:
        return four("queue_full", n=len(held))
    with supply.REQUESTS.open("a") as fh:
        fh.write(vid + "\n")
    data.setdefault("asked", {})[vid] = {"who": sender["name"], "title": title,
                                         "at": int(now), "state": "waiting",
                                         "redemption": None}
    play_short()
    return f"{title[:40]} · " + four("downloading") + f" {waiting_for(seconds)}"


def cmd_pick(data, now, sender, args):
    ready, pool = pickable()
    rows = ready + pool
    if not rows:
        return four("nothing_to_pick")
    try:
        index = int(args[0]) - 1
    except (IndexError, ValueError):
        return four("pick_usage")
    if not 0 <= index < len(rows):
        return four("pick_range", max=len(rows))
    if index >= len(ready):
        return ask_for(data, now, sender, rows[index])
    target, said = ready[index][1], ready[index][2]
    vote = data.get("vote")
    if vote and vote["closes"] > now and vote["kind"] == "pick" and vote["target"] == target:
        return cmd_vote(data, now, sender, args)
    if vote and vote["closes"] > now:
        return four("vote_running")
    need = threshold()
    data["vote"] = {"kind": "pick", "target": target, "voters": [sender["user_id"]],
                    "need": need, "closes": now + VOTE_WINDOW}
    if need <= 1:
        PICK.write_text(json.dumps({"name": target, "at": int(now)}))
        data["vote"] = None
        return f"{said} · " + four("is_next")
    return f"{said[:40]} · " + four("vote_play", need=need, n=index + 1)


def cmd_force(data, now, sender, args):
    if not sender["privileged"]:
        return None
    live = playing()
    reserve = unseen_hours() * 3600
    if other_stream_hours(live) < 0.5:
        return four("only_stream")
    if reserve - skip_cost(live) < SKIP_FLOOR:
        return four("not_enough")
    do_skip(data, now, f"forced by {sender['name']}", live, "")
    return four("moving_on")


COMMANDS = {
    "aide": cmd_aide, "help": cmd_aide, "commands": cmd_aide, "commandes": cmd_aide,
    "vod": cmd_vod, "now": cmd_vod, "np": cmd_vod,
    "liste": cmd_liste, "list": cmd_liste, "videos": cmd_liste,
    "link": cmd_source, "source": cmd_source, "stats": cmd_stats,
    "vote": cmd_vote, "skip": cmd_vote, "next": cmd_vote,
    "pick": cmd_pick, "choix": cmd_pick,
    "fetch": cmd_fetch, "queue": cmd_fetch, "dl": cmd_fetch,
    "force": cmd_force,
}


def note_use(data, word, args):
    """Count what the chat actually reaches for.

    The list runs to five hundred entries over sixty six pages and nothing knew
    whether anyone ever left the first one. Kept in the bot's own state rather
    than logged, because this answers a question asked now and then, not one
    asked at four in the morning, and a line per command would drown the log
    that is read then.
    """
    used = data.setdefault("used", {})
    used[word] = used.get(word, 0) + 1
    if word in ("pick", "choix") and args:
        try:
            used["pick_max"] = max(used.get("pick_max", 0), int(args[0]))
        except ValueError:
            pass


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
    note_use(data, word.lower(), rest.split())
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
    """The wait a viewer is quoted: minutes to the disk, minutes to the wire.

    The minutes are the answer and they read in every language, so they are
    said once. The words around them are not, so the hour on air carries its
    four, and the word for the first number, downloading, is said where this
    is used.
    """
    soon, then = fetch_eta(seconds), air_eta()
    if not then:
        return f"~{soon} min"
    return f"~{soon} min · " + four("then_on_air", min=soon + then)


# --- channel points --------------------------------------------------------

# What the channel sells, and what each one does. The cost is a starting point;
# change it in the dashboard and nothing here cares. A reward is matched on its
# title, so renaming one in the dashboard unhooks it, which is the honest
# failure: better a reward that does nothing and refunds than one that does
# something nobody expected.
# kil, 2026-09-21: the five at a hundred points each. They were priced against
# each other, a skip cheap and a fetch dear, which only made the dear ones
# never happen; one price says every lever is worth pulling.
# "was" carries every title a reward has had, which is what lets an existing one
# be renamed in place: a redemption is matched on its title, so creating the new
# one beside the old would leave the old taking points for something nothing
# answers any more.
# kil, 2026-09-21: "c'est tres geek, il faut quelque chose international", so
# each title and description carried English, Spanish, Japanese and Turkish.
# kil, 2026-09-22: "on sait meme pas ce que ca fait !! c'est ca le plus gros AI
# SLOP". The points panel shows the title and nothing else, in a tile two lines
# tall, so four languages there meant nobody read any of them: "Skip · Saltar ·
# スキップ · Geç" wrapped mid-word and said less than "Skip" would have. One
# language, one word in the tile, one plain sentence underneath that names what
# the button does. The chat still answers in four languages, because that is a
# line somebody reads once, not a label they scan.
REWARDS = [
    {"key": "skip", "title": "Skip", "cost": 100, "input": False,
     "was": ["Skip · Saltar · スキップ · Geç", "Skip this hour"],
     "description": "Plays a different stream right now"},
    {"key": "stay", "title": "Stay", "cost": 100, "input": False,
     "was": ["Stay · Seguir · 続ける · Devam", "Keep this one going"],
     "description": "Keeps this stream on for one more hour"},
    {"key": "pick", "title": "Pick", "cost": 100, "input": True,
     "was": ["Pick · Elegir · 選ぶ · Seç", "Pick what plays next"],
     "description": "Type !list in chat, then put that number here"},
    {"key": "jump", "title": "+10 min", "cost": 100, "input": False,
     "was": ["+10 min · Avanzar · 10分進む · İleri sar",
             "+30 min · Avanzar · 30分進む · İleri sar"],
     "description": "Jumps 10 minutes forward in this stream"},
    {"key": "place", "title": "Travel", "cost": 100, "input": True,
     "was": ["Travel · Viajar · 旅先 · Gezi", "Take me somewhere"],
     "description": "Type a country or a city, a stream from there plays next"},
]
# the old spellings answer too: a redemption made in the seconds before the
# rename lands carries the title the viewer saw, and it was paid for all the same
BY_TITLE = {title.lower(): r for r in REWARDS
            for title in [r["title"], *r.get("was", ())]}


def reward_skip(data, now, who, text):
    """(honoured, what to say). Points come back when the answer is no."""
    live = playing()
    blocked = skip_blocked(data, now, live, data.get("redeemer_id") or who)
    if blocked:
        return False, f"@{who} {blocked} · " + four("points_back")
    do_skip(data, now, f"points from {who}", live, data.get("redeemer_id") or who)
    return True, f"@{who} " + four("moving_on")


def reward_jump(data, now, who, text):
    """Thirty minutes forward inside the stream on air, not off it.

    The guard is the buffer itself: with less than the jump waiting, dropping
    it all would end the hour, which is a skip, and a skip has floors and
    cooldowns this one is not allowed to walk past for the same hundred points.
    """
    if not playing():
        return False, f"@{who} " + four("nothing_on")
    if cut.ahead_seconds() < JUMP_SECONDS:
        return False, (f"@{who} " + four("not_cut_ahead", min=JUMP_SECONDS // 60)
                      )
    cut.JUMP.write_text(str(JUMP_SECONDS))
    return True, f"@{who} " + four("forward", min=JUMP_SECONDS // 60)


def reward_pick(data, now, who, text):
    ready, pool = pickable()
    rows = ready + pool
    try:
        index = int(re.sub(r"\D", "", text or "")) - 1
    except ValueError:
        return False, f"@{who} " + four("not_a_number")
    if not rows or not 0 <= index < len(rows):
        return False, f"@{who} " + four("no_video_there")
    if index >= len(ready):
        # the numbers in !list run on into the catalogue, so a paid pick can
        # land on something that has to be fetched first. That is the request
        # reward under another name, and it settles the same way
        vid, _, title, seconds = rows[index]
        ok, refusal = queue_request(data, now, who, vid, title)
        if not ok:
            return False, refusal
        return True, (f"@{who} {title[:44]} · " + four("downloading")
                      + f" {waiting_for(seconds)}")
    PICK.write_text(json.dumps({"name": ready[index][1], "at": int(now)}))
    return True, f"@{who} {ready[index][2][:48]} · " + four("is_next")


def reward_stay(data, now, who, text):
    """Another hour of what is on, which is the opposite of a skip.

    Nothing new is needed: the chat's pick lever names a file, and the draw
    takes an unaired hour from whatever it names. Naming the file already
    playing is how a viewer buys more of it.
    """
    live = playing()
    if not live:
        return False, f"@{who} " + four("nothing_on")
    book, durations = cut.ledger(), chan.read_json(chan.STATE / "durations.json", {})
    # current/ first: the file on air lives there for as long as its cutting job
    # is alive, which is most of the hour it is playing. Without it this reward
    # refunded a viewer for asking to keep watching what was on, which is the
    # one moment they are certain to ask
    for folder in (chan.CURRENT, chan.QUEUE, chan.AIRED):
        path = folder / live["name"]
        if path.exists() and cut.unaired(path, book, durations, cut.reserved()):
            PICK.write_text(json.dumps({"name": live["name"], "at": int(now)}))
            return True, f"@{who} {live['title'][:44]} · " + four("one_more_hour")
    return False, f"@{who} " + four("no_hours_left")


# Titles name cities, not countries: "japan" matched six videos while Osaka
# alone had a hundred and three. Measured against the real catalogue on
# 2026-09-19, so a country asked for finds the streams shot in it. Ask for a
# city and you get that city; ask for a country and you get all of it.
# the table lives in chan.py: supply.py orders the candidate list with it too
PLACES = chan.PLACES


def place_terms(wanted):
    """Every spelling worth looking for, given what somebody typed."""
    return PLACES.get(wanted, (wanted,))


def too_long_for_now(seconds):
    """Whether this length is more than the supply line can spare right now.

    Both halves matter. A long video is not refused on principle: with a deep
    reserve the board can spend an afternoon on one and nothing suffers. It is
    refused when the channel is already short, which is exactly when the hours
    it costs are the hours the channel does not have.
    """
    if not seconds or seconds <= REQUEST_MAX_SECONDS:
        return False
    runway = chan.read_json(chan.STATE / "want.json", {}).get("runway_seconds") or 0
    return runway < REQUEST_RICH_SECONDS


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
    if too_long_for_now(catalog_seconds(vid)):
        return False, (f"@{who} " + four("too_long", hours=catalog_seconds(vid) // 3600))
    if vid in held:
        # overwriting the row would strand the first viewer's redemption in
        # Kick's queue for good, with their points gone and nobody to settle it
        return False, (f"@{who} " + four("already_coming"))
    if len(held) >= REQUEST_MAX_PENDING:
        return False, (f"@{who} " + four("queue_full", n=len(held)))
    with supply.REQUESTS.open("a") as fh:
        fh.write(vid + "\n")
    data.setdefault("asked", {})[vid] = {"who": who, "title": title, "at": int(now),
                                         "state": "waiting", "redemption": None}
    # hand the id back to redeemed(), which alone knows it. Comparing the keys
    # of asked before and after cannot: a video already waiting is not a new
    # key, and the redemption behind it was then settled as if it were done.
    data["queued_now"] = vid
    return True, None


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
        return False, f"@{who} " + four("name_a_place")
    terms = place_terms(wanted)
    for path, _ in shelf():
        low = clean_title(path.name, SLUG).lower()
        if any(t in low for t in terms):
            PICK.write_text(json.dumps({"name": path.name, "at": int(now)}))
            return True, f"@{who} {clean_title(path.name, SLUG)[:52]} · " + four("is_next")
    # kil, 2026-09-21: two "peru" and a "vietnam" came back refunded while the
    # catalogue held plenty of both. The first match was one somebody had
    # already asked for, and that refusal was the whole answer. A place is not
    # one video, so the ones already on their way are stepped over. The board's
    # own cap is different: it refuses every candidate alike, so once it speaks
    # there is nothing left to try.
    skip, held = supply.excluded(now), set(waiting_requests(data))
    already = False
    for vid, title in supply.catalog_titles().items():
        if vid in skip or not any(t in title.lower() for t in terms):
            continue
        if vid in held:
            already = True
            continue
        queued, refusal = queue_request(data, now, who, vid, title)
        if not queued:
            return False, refusal
        play_short()
        return True, (f"@{who} {clean_title(title, SLUG)[:38]} · "
                      + four("downloading") + f" {waiting_for(catalog_seconds(vid))}")
    if already:
        return False, (f"@{who} " + four("place_coming", place=wanted))
    return False, f"@{who} " + four("nothing_there")


ACTIONS = {"skip": reward_skip, "stay": reward_stay, "pick": reward_pick,
           "place": reward_place, "jump": reward_jump}


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
    # the guards count skips per viewer, and a redemption names its redeemer
    # where a chat command names a sender: same person, two shapes
    data["redeemer_id"] = person.get("user_id")
    try:
        honoured, answer = ACTIONS[known["key"]](
            data, time.time(), who, payload.get("user_input") or "")
    except Exception as problem:
        # anything thrown in there used to walk out of this function with the
        # redemption still pending and the points already taken. A crash is a
        # refusal like any other. The state is read again rather than saved
        # half-written by whatever stopped halfway through it.
        chan.log(f"recompense {known['key']} par {who} a casse: {problem}")
        data = load()
        honoured, answer = False, f"@{who} " + four("broke")
    # A video the board has not brought back yet: accepting now would take the
    # points for a delivery the daily ceiling may still swallow, so the
    # redemption stays in Kick's queue and announce_arrivals settles it either
    # way, which is the only path that can still refund.
    data.pop("redeemer_id", None)
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
        for old in spec.get("was", ()):
            found = found or have.get(old.lower())
        if not found:
            done = kickapi.create_reward(spec["title"], spec["cost"],
                                         spec["description"], spec["input"])
            chan.log(f"recompense {'creee' if done else 'refusee'}: "
                     f"{spec['title']} ({spec['cost']} points)")
        elif update and (found.get("cost") != spec["cost"]
                         or (found.get("description") or "") != spec["description"]
                         or (found.get("title") or "") != spec["title"]):
            done = kickapi.update_reward(found["id"], spec["cost"],
                                         spec["description"], spec["title"])
            chan.log(f"recompense {'ajustee' if done else 'inchangee'}: "
                     f"{found.get('title')} -> {spec['title']} ({spec['cost']} points)")


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
    tail = f"{piece} · !skip to move on"
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
    checked = swept = 0
    while True:
        try:
            data = load()
            want = wanted_title()
            now = time.time()
            if now - swept > STRAY_AFTER:
                swept = now
                sweep_redemptions(data, now)
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
        say("nothing new to play, the next stream is on its way")
        return
    piece = f" (hour {live['hour']}/{live['hours']})" if live["hours"] > 1 else ""
    say(f"now playing: {clean_title(live['name'], SLUG)}{piece} · !now !list !skip")


def settle_request(row, honoured):
    """Take the points, or give them back. Silent for a request made by hand."""
    if row.get("redemption"):
        kickapi.settle_redemption(row["redemption"], honoured)


def sweep_redemptions(data, now):
    """Hand back the points for anything pending that nothing here is waiting on.

    The only thing in this file that reads Kick's queue instead of trusting
    what we remember doing to it. A paid fetch is deliberately left pending
    until the video lands, so those ids are stepped over; everything else that
    is still sitting there five minutes on was dropped by something, and the
    viewer is owed their points back rather than an explanation.
    """
    held = {row.get("redemption") for row in (data.get("asked") or {}).values()}
    for row in kickapi.pending_redemptions():
        if not row["id"] or row["id"] in held or now - row["at"] < STRAY_AFTER:
            continue
        if kickapi.settle_redemption(row["id"], False):
            chan.log(f"redemption oubliee remboursee: {row['title']} ({row['id']})")


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
                f", on next in ~{air_eta()} min")
            row["state"], row["at"] = "here", int(now)
            continue
        if vid in landing:
            continue  # mid-transfer from the board, not a failure
        barred = supply.excluded(now) if barred is None else barred
        # a request from the chat costs nothing, so telling that viewer their
        # points are back names points they never spent
        back = ", points back" if row.get("redemption") else ""
        if vid in barred:
            settle_request(row, False)
            say(f"@{who} {title} cannot be fetched after all{back}")
            asked.pop(vid)
        elif now - row.get("at", 0) > REQUEST_DEADLINE:
            settle_request(row, False)
            say(f"@{who} {title} did not arrive within "
                f"{REQUEST_DEADLINE // 3600} h, the board is at its daily "
                f"ceiling{back}")
            asked.pop(vid)
    data["asked"] = asked


def close_stale_vote(data, now):
    vote = data.get("vote")
    if vote and vote["closes"] <= now:
        data["vote"] = None
        data["vote_failed_at"] = now
        save(data)
        say(f"not enough votes ({len(vote['voters'])}/{vote['need']}), it stays on")


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
        # kil, 2026-09-22: "wtf tu les manges juste". A redemption webhook whose
        # client had already hung up died right here, on the 200 that do_POST
        # sends before the work, so the work never ran: no skip, no refund, no
        # word in chat, and the points gone into Kick's pending queue for good.
        # Answering is a courtesy to Kick. Doing the thing is not.
        try:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError as problem:
            chan.log(f"reponse {code} non remise: {problem}")

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
            who = str(((payload.get("sender") or {}).get("username")) or "?")[:25]
            relay(f"{who}: {payload.get('content') or ''}")
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
        print(f"inedit en reserve: {unseen_hours():.1f} h sur {len(shelf())} videos")
        print(f"titre vise: {wanted_title()}")
        print(f"jeton: {'present' if kickapi.token() else 'absent, --authorize'}")
        used = load().get("used") or {}
        if used:
            counts = ", ".join(f"{k} {v}" for k, v in sorted(used.items())
                               if k != "pick_max")
            print(f"commandes: {counts}")
            print(f"numero le plus loin choisi: {used.get('pick_max', 0)}")
        return 0
    if not BROADCASTER:
        chan.log("KICK_USER_ID absent de channel.env")
        return 1
    threading.Thread(target=keep_title, daemon=True).start()
    if TG_TOKEN and TG_CHAT:
        threading.Thread(target=relay_loop, daemon=True).start()
        # kil, 2026-09-21: "logs tout ce qui se passe dans le bot telegram".
        # Everything this process writes down goes out with the chat, in the
        # same fifteen second batch, so a redemption, a refund and the answer
        # a viewer saw read in one place and in order.
        chan.on_log = lambda line: relay(f"[log] {line}")
        if TG_POLL:
            threading.Thread(target=tg_loop, daemon=True).start()
        chan.log(f"telegram: chat relaye, reponse depuis telegram "
                 f"{'active' if TG_POLL else 'inactive (TG_POLL=0)'}")
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
