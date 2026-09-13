#!/usr/bin/env python3
"""Telegram control and, more to the point, Telegram alarms.

    python3 bin/tgbot.py            answer commands and watch, forever
    python3 bin/tgbot.py --once     one health pass, alert if needed, exit

The commands are the smaller half. On 2026-09-05 the channel went to the standby
clip four times and every single time the way it was discovered was the owner
looking at it and saying so. Nothing watched the two numbers that predict it:
how much prepared video is on disk, and how much of the library prep can
actually copy. This watches both and speaks first.

Alarms are edge triggered. A message every poll while a condition holds is a
message nobody reads by the second day, so each condition fires once when it
starts and once more when it clears.

The token lives in tg.env, which is not in the repository. Anyone holding it can
post as this bot, so it is chmod 600 and stays out of git.

Only one chat is served. The first /start binds it, and every later command from
anywhere else is ignored: without that, whoever finds the bot gets /skip on a
channel that is not theirs.
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import common
import prep

ENVFILE = common.ROOT / "tg.env"
STATEFILE = common.STATE / "tg.json"
LIBRARY = common.LIBRARY_DIR
# One code tree serves every channel, so the units it talks about are the
# ones its own environment names, never a fixed set.
PUSH, FEED, PREP = common.unit("push"), common.unit("feed"), common.unit("prep")

POLL_SECONDS = 25          # long poll, so this costs nothing while idle
HEALTH_EVERY = 120         # a health pass at most this often

# The two numbers that actually predict the standby clip, plus the two that
# explain it after the fact. Measured thresholds, not round ones: prep abandons
# an encode at two chunks, so warning there would be warning too late.
LOW_BACKLOG_SECONDS = 4 * common.CHUNK_SECONDS
LOW_PLAYABLE_FILES = 8
# Free space is shared by every channel on the box. At 6 Go this alarmed for
# ever as soon as two shares were full, which is the normal state of a full
# disk and not a fault. The floor the janitor frees to is the line that
# actually means something is wrong.
LOW_FREE_BYTES = common.SHARED_FREE_FLOOR_BYTES
# A channel eats twenty four hours of video a day. Gaining none for this long
# is broken whatever the cause, and it is the assertion that would have caught
# the one fault nothing else did: every YouTube ranged fetch had been answered
# with HTTP 403 since parts were introduced, for about a day, and the first
# thing to notice was a viewer.
STALE_ARRIVAL_SECONDS = 6 * 3600
# How often a fault that is still there says so again. Short enough that a
# night of it is several messages, long enough that a slow repair is not
# drowned in its own alarm.
REMIND_SECONDS = 3 * 3600
# The other channels this one answers for, by name. One token, one poller: a
# second bot polling the same token would take half the updates and each would
# see half the commands, so the single poller speaks for everybody.
OTHERS = [c.strip() for c in os.environ.get("VODLOOP_CHANNELS", "").split(",")
          if c.strip()]


def env():
    out = {}
    try:
        for line in ENVFILE.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip():
                out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def set_env(key, value):
    data = env()
    data[key] = value
    ENVFILE.write_text("".join(f"{k}={v}\n" for k, v in data.items()))
    ENVFILE.chmod(0o600)


def api(method, **params):
    token = env().get("TG_TOKEN")
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(url, body, timeout=POLL_SECONDS + 15) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def say(text):
    chat = env().get("TG_CHAT")
    if not chat:
        return False
    # Several channels report into one chat, so a message says which one it
    # is from. Unset, the label is empty and the first channel reads exactly
    # as it always did.
    text = f"{common.LABEL} {text}" if common.LABEL else text
    return bool(api("sendMessage", chat_id=chat, text=text[:3900],
                    disable_web_page_preview="true"))


def state():
    try:
        return json.loads(STATEFILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(data):
    common.STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATEFILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(STATEFILE)


def sh(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def unit(name):
    return sh(["systemctl", "is-active", name]) or "inconnu"


def restarts(name):
    return sh(["systemctl", "show", "-p", "NRestarts", "--value", name]) or "?"


def library():
    """Total files, and how many prep will copy. The second number is the
    channel: files it cannot copy are not in the rotation at all."""
    if not LIBRARY.is_dir():
        return 0, 0
    files = [p for p in LIBRARY.iterdir()
             if p.is_file() and p.suffix.lower() in (".mp4", ".mkv")]
    return len(files), sum(1 for p in files if prep.remux_verdict(p))


def free_bytes():
    try:
        import shutil
        return shutil.disk_usage(LIBRARY).free
    except OSError:
        return 0


def emitting():
    """Bytes the pusher put on the wire in two seconds.

    Kick's API answers 404 often enough that asking it is not a liveness test.
    What leaves the socket is.
    """
    # Scoped to this channel's own pipe. "ffmpeg.*rtmps" takes the first
    # pusher on the box, and medic restarts whatever this reports silent:
    # with two channels that is how one channel's medic ends the other's
    # live.
    pids = sh(["pgrep", "-f", f"ffmpeg.*{common.FIFO}"]).split()
    if not pids:
        return None

    # ss prints the socket line then its counters, so match the pid on one and
    # read the number from the next. Taking the first bytes_sent in the output
    # reads whichever connection was listed first and calls a live pusher mute.
    def sent_for_pid():
        lines = sh(["ss", "-tnip"], 20).splitlines()
        for n, line in enumerate(lines):
            if f"pid={pids[0]}," in line:
                for follow in lines[n:n + 3]:
                    for token in follow.split():
                        if token.startswith("bytes_sent:"):
                            return int(token.split(":")[1])
        return None

    first = sent_for_pid()
    if first is None:
        return None
    time.sleep(2)
    second = sent_for_pid()
    return None if second is None else max(0, second - first)


def viewer_frame():
    """Pull the stream a viewer actually receives and measure one frame.

    Everything else here reports on our side of the wire. That answers "are we
    sending", not "is the picture black", and on 2026-09-06 those two came apart:
    the chunks held real video, the pusher was emitting 3390 kbps, Kick said
    live, and a black screen was reported anyway. The only way to settle it was
    to fetch what the player fetches, which is what this does.

    Returns (verdict, detail). A frame that cannot be fetched is unknown, not
    black: calling those the same is how a probe invents an outage.
    """
    try:
        from curl_cffi import requests
    except ImportError:
        return None, "curl_cffi absent"
    try:
        r = requests.get(
            f"https://kick.com/api/v2/channels/{common.channel_slug()}",
            impersonate="chrome", timeout=25)
        if r.status_code != 200:
            return None, f"API {r.status_code}"
        data = r.json() or {}
        url = data.get("playback_url") or (data.get("livestream") or {}).get("playback_url")
        if not url:
            return None, "pas d'url de lecture (hors ligne ?)"
    except Exception as exc:                                   # noqa: BLE001
        return None, type(exc).__name__

    raw = pathlib.Path("/tmp/vodloop_viewer.gray")
    raw.unlink(missing_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", url,
             "-frames:v", "1", "-vf", "scale=48:27", "-f", "rawvideo",
             "-pix_fmt", "gray", "-y", str(raw)],
            capture_output=True, timeout=90)
        pixels = raw.read_bytes()
    except (OSError, subprocess.SubprocessError):
        return None, "frame non recuperee"
    finally:
        raw.unlink(missing_ok=True)
    if not pixels:
        return None, "frame vide"
    lo, hi = min(pixels), max(pixels)
    avg = sum(pixels) / len(pixels)
    # the standby clip is a flat 0x101014 fill, so its pixels are all but
    # identical; a real picture spreads. Brightness alone would call a dark
    # scene an outage.
    flat = hi - lo < 6
    return (not flat), f"min={lo} max={hi} moy={avg:.0f}"


def status_text():
    backlog = prep.seconds_on_disk()
    total, playable = library()
    moved = emitting()
    wire = ("pas de pusher" if moved is None
            else f"{moved * 8 / 2 / 1000:.0f} kbps" if moved else "MUET")
    return (
        f"vodloop\n"
        f"sur le fil   {wire}\n"
        f"tampon       {backlog}s ({backlog // common.CHUNK_SECONDS} chunks)\n"
        f"bibliotheque {playable} jouables / {total}\n"
        f"disque       {free_bytes() / 1024 ** 3:.1f} Go libres\n"
        f"push {unit(PUSH)} ({restarts(PUSH)} relances)  "
        f"feed {unit(FEED)}  prep {unit(PREP)}"
    )


def now_text():
    segments = common.ready_segments()
    if not segments:
        return "rien de pret: la chaine est sur le clip d'attente"
    queue = common.load_queue()
    names = {f"{i['id']:05d}": (i.get("title") or i.get("url") or "?")
             for i in queue["items"]}
    playing = names.get(segments[0].name.split("_")[0], "?")
    following = ""
    for chunk in segments[1:]:
        other = names.get(chunk.name.split("_")[0], "?")
        if other != playing:
            following = other
            break
    out = f"en cours  {playing[:70]}\n{len(segments)} chunks prets"
    return out + (f"\na suivre  {following[:70]}" if following else "")


def conv_text():
    total, playable = library()
    todo = total - playable
    try:
        failed = len(json.loads((common.STATE / "normalise_failed.json").read_text()))
    except (OSError, ValueError):
        failed = 0
    return (f"a convertir  {todo} fichier(s)\n"
            f"jouables     {playable} / {total}\n"
            f"echecs notes {failed}\n"
            f"(un fichier non convertible n'entre pas dans la file, "
            f"il ne peut donc pas provoquer d'ecran gris)")


def log_text():
    out = sh(["journalctl", "-u", PUSH, "-u", PREP,
              "-n", "400", "--no-pager"], 60)
    bad = [l for l in out.splitlines()
           if any(w in l.lower() for w in
                  ("error", "echec", "missing pts", "abandon", "failed"))]
    return "\n".join(bad[-12:])[:3500] or "rien d'anormal dans les journaux recents"


def skip():
    """Grant a skip the way the chat does, by stamping the file the feeder
    watches. Writing to the queue instead would race with prep."""
    try:
        chat = json.loads((common.STATE / "chat.json").read_text())
    except (OSError, ValueError):
        chat = {}
    chat["last_skip"] = time.time()
    tmp = (common.STATE / "chat.json").with_suffix(".tmp")
    tmp.write_text(json.dumps(chat))
    tmp.replace(common.STATE / "chat.json")
    return "saut demande, la video en cours est coupee"


HELP = """commandes
/status  le fil, le tampon, la bibliotheque, le disque
/now     ce qui passe et ce qui suit
/lib     etat de la bibliotheque jouable
/conv    ce qu'il reste a convertir
/log     les lignes anormales des journaux
/black   l'image est-elle vraiment noire, vue d'un spectateur
/skip    couper la video en cours
/mute    couper les alertes 8h

n'importe laquelle suivie d'un nom de chaine repond pour elle,
par exemple: /status nanatty247
/help    ceci

les alertes partent toutes seules quand le tampon tombe, quand le
pusher se met a boucler, ou quand la bibliotheque jouable s'epuise"""


def ask_channel(name, cmd):
    """The same command, answered with another channel's environment.

    One Telegram token cannot have two pollers: they would take each other's
    updates, and each would see half the commands. So one bot answers for every
    channel, and it does it by running this same file with the other channel's
    environment, because the environment is the only thing that differs between
    them. A subprocess with that environment IS the other channel.
    """
    # Ecrit a la main plutot que par pathlib: ce chemin decrit le serveur, pas
    # la machine qui execute, et pathlib rendrait des antislashs ailleurs.
    child = dict(os.environ,
                 VODLOOP_ROOT=f"/home/ubuntu/{name}",
                 VODLOOP_LIBRARY=f"/home/ubuntu/videos-{name}",
                 VODLOOP_UNIT="vodloop-%s@" + name,
                 VODLOOP_LABEL=f"[{name}]")
    # cleared, or the child would think it too answers for other channels and
    # a typo could bounce between them
    child.pop("VODLOOP_CHANNELS", None)
    try:
        out = subprocess.run(
            [sys.executable, str(pathlib.Path(__file__).resolve()), "--answer", cmd],
            capture_output=True, text=True, timeout=120, env=child)
    except (OSError, subprocess.SubprocessError) as err:
        return f"[{name}] injoignable: {err}"
    answer = (out.stdout or "").strip()
    return f"[{name}]\n{answer}" if answer else f"[{name}] n'a rien a dire sur {cmd}"


def handle(text):
    parts = (text or "").strip().split()
    cmd = (parts[0].split("@")[0].lower() if parts else "")
    # "/status nanatty247" is the same question asked of the other channel. The
    # bare form stays this channel's, so nothing anyone already types changes.
    target = parts[1].lower() if len(parts) > 1 else ""
    if target and target in OTHERS:
        return ask_channel(target, cmd)
    if cmd in ("/status", "/etat"):
        return status_text()
    if cmd == "/now":
        return now_text()
    if cmd == "/lib":
        total, playable = library()
        return (f"{playable} fichiers jouables sur {total}\n"
                f"les autres attendent normalise.py")
    if cmd == "/conv":
        return conv_text()
    if cmd == "/log":
        return log_text()
    if cmd in ("/black", "/noir"):
        ok, detail = viewer_frame()
        if ok is None:
            # unknown is not black, and saying so is the whole point
            return f"indetermine: {detail}\n(indetermine n'est pas 'noir')"
        since = sh(["systemctl", "show", "-p", "ActiveEnterTimestamp",
                    "--value", PUSH])
        verdict = ("l'image que recoit un spectateur est REELLE" if ok
                   else "l'image que recoit un spectateur est NOIRE")
        tail = ("si ton lecteur est noir, il tient une session morte: recharge"
                if ok else "c'est une vraie panne")
        lines = [verdict, detail]
        if since:
            lines.append(f"pusher connecte depuis {since}")
        lines.append(tail)
        return "\n".join(lines)
    if cmd == "/skip":
        return skip()
    if cmd == "/mute":
        data = state()
        data["quiet_until"] = time.time() + 8 * 3600
        save_state(data)
        return "alertes coupees 8h"
    if cmd in ("/help", "/start"):
        return HELP
    return None


def newest_arrival():
    """Seconds since the library last gained a file, or None if it is empty.

    Nothing arriving is the fault that hides every other one. The board can be
    refusing, the collector can be blocking itself, a whole fetch path can have
    stopped working: the symptom is the same and no component reports it,
    because no component owns the chain end to end. This is the one number that
    does, and it costs one stat per file.
    """
    if not LIBRARY.is_dir():
        return None
    seen = []
    for path in LIBRARY.iterdir():
        try:
            if path.is_file() and path.suffix.lower() in (".mp4", ".mkv"):
                seen.append(path.stat().st_mtime)
        except OSError:
            continue
    return time.time() - max(seen) if seen else None


def health():
    """Say what is broken, and keep saying it while it still is."""
    data = state()
    if time.time() < data.get("quiet_until", 0):
        return
    was = data.get("alarms", {})
    said_at = data.get("alarm_said", {})
    backlog = prep.seconds_on_disk()
    total, playable = library()
    moved = emitting()

    now = {}
    stale = newest_arrival()
    if stale is not None and stale > STALE_ARRIVAL_SECONDS:
        now["arrivees"] = (f"rien de neuf en bibliotheque depuis {stale / 3600:.0f} h, "
                           f"la chaine consomme sans etre reapprovisionnee")
    if backlog <= LOW_BACKLOG_SECONDS:
        now["tampon"] = f"tampon a {backlog}s, la chaine va tomber sur le clip d'attente"
    if playable <= LOW_PLAYABLE_FILES:
        now["bibliotheque"] = (f"{playable} fichiers jouables seulement: "
                               f"la rotation s'epuise")
    if free_bytes() <= LOW_FREE_BYTES:
        now["disque"] = f"{free_bytes() / 1024 ** 3:.1f} Go libres, prep va s'arreter"
    if moved == 0:
        now["fil"] = "le pusher n'envoie plus rien"
    try:
        if int(restarts(PUSH)) > int(data.get("push_restarts", -1) or -1) >= 0:
            now["pusher"] = "le pusher vient de redemarrer"
    except (TypeError, ValueError):
        pass

    # A fault that lasts has to keep saying so. Saying it only on the edge
    # means one message for a twenty four hour outage, and one message in a
    # chat two channels share is one message nobody finds again. Measured
    # 2026-09-14: the second channel's library alarm had been latched at three
    # playable files since the night before, and the only thing that noticed
    # was a viewer seeing the same video come round twice.
    fresh = {}
    for key, message in now.items():
        last = said_at.get(key, 0)
        if key not in was:
            say("ALERTE " + message)
            fresh[key] = time.time()
        elif time.time() - last >= REMIND_SECONDS:
            say(f"TOUJOURS EN PANNE depuis {(time.time() - last) / 3600:.0f} h: {message}")
            fresh[key] = time.time()
        else:
            fresh[key] = last
    for key in was:
        if key not in now:
            say(f"revenu a la normale: {key}")

    data["alarms"] = now
    data["alarm_said"] = fresh
    data["push_restarts"] = restarts(PUSH)
    save_state(data)


def main(argv):
    if "--once" in argv:
        health()
        return 0
    if "--answer" in argv:
        # Answers one command for whatever channel this process's environment
        # names, and prints it. This is how the one bot speaks for the others.
        rest = argv[argv.index("--answer") + 1:]
        print(handle(" ".join(rest)) or "")
        return 0
    data = state()
    offset = data.get("offset", 0)
    last_health = 0.0
    while True:
        got = api("getUpdates", offset=offset, timeout=POLL_SECONDS)
        for update in (got or {}).get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat = str((message.get("chat") or {}).get("id") or "")
            text = message.get("text") or ""
            bound = env().get("TG_CHAT")
            if not bound and text.strip().startswith("/start"):
                # trust on first use: this bot is private, and the first person
                # to say hello is the person who made it
                set_env("TG_CHAT", chat)
                bound = chat
                say("bot lie a ce salon\n\n" + HELP)
                continue
            if chat != bound:
                continue  # not this channel's owner: say nothing at all
            reply = handle(text)
            if reply:
                say(reply)
        data = state()
        data["offset"] = offset
        save_state(data)
        if time.time() - last_health > HEALTH_EVERY:
            health()
            last_health = time.time()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(0)
