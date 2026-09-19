"""vodloop v2: one channel is one directory and one config file.

    board (home IP, 128 GB card)             Oracle (datacenter, one share of disk)
    hangar fill  YouTube -> hangar/     <-   state/candidates.tsv      supply.py
    hangar ship  hangar/ -> upload/     <-   state/want.json           supply.py
                 upload/ -> queue/           (one atomic mv)
                                             queue/ -> current/ -> aired/   cut.py
                                             current/ -> chunks/            cut.py, copy only
                                             chunks/ -> pipe                feed.py
                                             pipe -> Kick                   push.sh, never restarted

Material only moves forward. Nothing waiting to air is ever deleted. The one
thing removed to make room is a file that has already aired, oldest first, and
only to honour the room supply.py has offered the board. That single rule
replaces v1's janitor and collector, whose two thresholds deadlocked on
2026-09-17 and left the channel replaying 33 hours of the same twelve videos.

Everything a channel needs to know is in CHAN_ROOT/channel.env. There is no
default root: the code names no channel.
"""
import json
import os
import pathlib
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request


def load_env(path):
    """KEY=value lines, # comments. Values are never printed."""
    out = {}
    try:
        for line in pathlib.Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


ROOT = pathlib.Path(os.environ["CHAN_ROOT"])
NAME = ROOT.name
CONF = load_env(ROOT / "channel.env")


def conf_num(key, default):
    try:
        return float(CONF.get(key, default))
    except ValueError:
        return float(default)


QUEUE = ROOT / "queue"
CURRENT = ROOT / "current"
AIRED = ROOT / "aired"
CHUNKS = ROOT / "chunks"
WORK = CHUNKS / ".work"
UPLOAD = ROOT / "upload"
STATE = ROOT / "state"
FIFO = ROOT / "pipe"
FILLER = ROOT / "filler.ts"

GIB = 1024 ** 3
MAXH = int(conf_num("MAXH", 720))
# The shortest picture the channel will put on the wire. Kick's ladder is fixed
# on the session, so a chunk under it is served upscaled: that is the whole cost
# of a low floor, and it is kil's call per channel, not a constant.
MINH = int(conf_num("MINH", 480))
MIN_SECONDS = int(conf_num("MIN_SECONDS", 3600))
MAX_SECONDS = int(conf_num("MAX_SECONDS", 43200))
BUDGET_BYTES = int(conf_num("BUDGET_GB", 28) * GIB)
WINDOW_SECONDS = int(conf_num("WINDOW_HOURS", 16) * 3600)
AHEAD_SECONDS = int(conf_num("AHEAD_SECONDS", 3600))
REFETCH_SECONDS = int(conf_num("REFETCH_DAYS", 21) * 86400)
MAX_FILE_BYTES = int(conf_num("MAX_FILE_GB", 10) * GIB)
# 0: a file airs whole, in the order it arrived. Above 0: it airs one slice at
# a time and goes back in the queue, where the next file is drawn at random, so
# a five hour stream is spread over the day instead of owning five hours of it.
PART_SECONDS = int(conf_num("PART_SECONDS", 0))
# kil has asked for this more times than I have acted on it: a Kick VOD is
# gone four weeks after it aired, so a channel built on them shows the same
# month for ever. With this set, nothing fetches Kick, nothing catalogues it,
# and nothing already on the disk is drawn. One switch, because a channel that
# needs three of them is a channel I will get wrong again.
NO_KICK = str(CONF.get("NO_KICK", "")).strip() not in ("", "0", "no", "false")

# Where a stream was shot, read off its title. Lives here because two things
# need it and for opposite reasons: the chat answers "have you got anything
# from Peru", and supply.py orders the candidate list so the board is not
# offered the same country forty times in a row, which on 2026-09-19 was
# Turkey for most of the head of nanatty247's list.
PLACES = {
    "japan": ("japan", "tokyo", "osaka", "kyoto", "hokkaido", "okinawa",
              "sapporo", "nara", "kobe", "hiroshima", "fukuoka", "kabuki",
              "roppongi", "shibuya", "shinjuku", "akihabara", "harajuku"),
    "turkey": ("turkey", "turkiye", "istanbul", "cappadocia", "bursa",
               "izmir", "pamukkale", "ankara", "antalya"),
    "peru": ("peru", "lima", "cusco", "arequipa", "machu"),
    "india": ("india", "jaipur", "agra", "delhi", "varanasi", "goa", "mumbai"),
    "korea": ("korea", "seoul", "busan"),
    "chile": ("chile", "chilie", "santiago", "coyhaique", "valparaiso",
              "atacama", "patagonia"),
    "argentina": ("argentina", "ushuaia", "buenos", "bariloche", "mendoza"),
    "thailand": ("thailand", "bangkok", "phuket", "chiang", "pattaya"),
    "vietnam": ("vietnam", "hanoi", "saigon", "danang", "hoi an"),
    "taiwan": ("taiwan", "taipei", "kaohsiung"),
    "mexico": ("mexico", "cancun", "oaxaca"),
    "brazil": ("brazil", "rio", "sao paulo"),
    "bolivia": ("bolivia", "la paz", "uyuni"),
    "indonesia": ("indonesia", "bali", "jakarta"),
    "philippines": ("philippines", "manila", "cebu"),
}
# a word boundary in front only: "Peru" must not match "Peruvian"'s neighbours
# but "Turkiye" inside a sentence must still count
_PLACE_HINTS = tuple((name, re.compile(r"\b(?:%s)" % "|".join(terms)))
                     for name, terms in PLACES.items())


def country_of(title):
    """The country a title names, or "" when it names none.

    Empty is a real answer and not a failure: about a third of this catalogue
    is home streams whose titles say nothing about where they are, and they
    are their own group in the draw rather than being forced into one.
    """
    low = str(title or "").lower()
    for name, hint in _PLACE_HINTS:
        if hint.search(low):
            return name
    return ""
# prep in v1 stopped below 4 GiB free; the disk is shared with the rest of the box
FLOOR_BYTES = int(conf_num("FLOOR_GB", 5) * GIB)
CHUNK_SECONDS = 300
UNIT = "vodloop-v2-%s@" + NAME
MEDIA = (".mkv", ".mp4")
VIDEO_ID = re.compile(r"-([A-Za-z0-9_-]{11})\.(?:mkv|mp4)$")
# SEI units made the flv muxer refuse copied packets with no PTS: 1298 pusher
# restarts, black throughout (2026-09-05). Removing them fixed it, pixels intact.
DROP_SEI = ["-bsf:v", "filter_units=remove_types=6"]


def log(message):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def media(folder):
    folder = pathlib.Path(folder)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in MEDIA)


def size_of(path):
    try:
        return pathlib.Path(path).stat().st_size
    except OSError:
        return 0


def tree_bytes(folder):
    total = 0
    for path in pathlib.Path(folder).rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def video_id(path):
    found = VIDEO_ID.search(pathlib.Path(path).name)
    return found.group(1) if found else None


def read_json(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def fps_supported(fps):
    """Kick relays 30 or 60 and silently re-encodes anything else (50 cost a week)."""
    return fps <= 30.5 or 59 <= fps <= 61


def probe(path):
    """The shape of a file, or None when ffprobe could not answer."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels"
             ":format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120)
        data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return parse_probe(data)


def parse_probe(data):
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        return None
    try:
        top, _, bottom = str(video.get("r_frame_rate", "0/1")).partition("/")
        fps = float(top) / float(bottom or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    try:
        seconds = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        seconds = 0.0
    return {
        "vcodec": video.get("codec_name"), "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0), "fps": round(fps, 2),
        "acodec": (audio or {}).get("codec_name"),
        "rate": int((audio or {}).get("sample_rate") or 0),
        "channels": int((audio or {}).get("channels") or 0),
        "seconds": seconds,
    }


def shape_problem(info):
    """None when a file can be copied to the wire as it is, else why not."""
    if info["vcodec"] != "h264":
        return f"video {info['vcodec']}"
    if not 0 < info["width"] <= 1920 or not MINH <= info["height"] <= 1080:
        return f"taille {info['width']}x{info['height']}"
    if not fps_supported(info["fps"]):
        return f"{info['fps']} i/s"
    if not info["acodec"]:
        return "pas de son"
    return None


def audio_copies(info):
    """AAC 44.1 kHz stereo goes through untouched; anything else is transcoded."""
    return (info["acodec"], info["rate"], info["channels"]) == ("aac", 44100, 2)


def remux_is_safe(path, seconds=10):
    """Copy ten seconds the way cut.py will and look for packets without PTS.

    True, False, or None when the probe itself could not run: a busy box is not
    a broken file, and v1 lost 22 good files by writing that down as a refusal.
    """
    probe_ts = pathlib.Path(tempfile.gettempdir()) / f"v2probe_{os.getpid()}.ts"
    try:
        done = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-t", str(seconds),
             "-i", str(path), "-map", "0:v:0", "-c:v", "copy", "-an"] + DROP_SEI
            + ["-f", "mpegts", "-y", str(probe_ts)],
            capture_output=True, timeout=180)
        if done.returncode != 0:
            return None
        packets = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
             "packet=pts_time", "-of", "csv=p=0", str(probe_ts)],
            capture_output=True, text=True, timeout=120).stdout
        if not packets.strip():
            return None
        return "N/A" not in packets
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        probe_ts.unlink(missing_ok=True)


def duration(path, cache=None):
    """Seconds of a media file, remembered by name and size."""
    path = pathlib.Path(path)
    key = f"{path.name}:{size_of(path)}"
    store = cache if cache is not None else read_json(STATE / "durations.json", {})
    if key not in store:
        info = probe(path)
        if info is None or not info["seconds"]:
            return 0.0
        store[key] = info["seconds"]
        if cache is None:
            write_json(STATE / "durations.json", store)
    return float(store[key])


def unit(role):
    return UNIT % role


def systemctl(*args):
    return subprocess.run(["systemctl", *args], capture_output=True, text=True).stdout.strip()


def telegram(text):
    token, chat = CONF.get("TG_TOKEN"), CONF.get("TG_CHAT")
    if not token or not chat:
        return False
    body = urllib.parse.urlencode({"chat_id": chat, "text": f"{CONF.get('LABEL', NAME)} {text}"[:3900],
                                   "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                    body, timeout=30) as response:
            return json.load(response).get("ok", False)
    except (OSError, ValueError):
        return False
