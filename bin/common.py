"""Shared paths, config and state helpers for vodloop."""
import json
import os
import pathlib
import re

ROOT = pathlib.Path(os.environ.get("VODLOOP_ROOT", pathlib.Path.home() / "vodloop"))
SEGMENTS = ROOT / "segments"
# drop a video file in here and it joins the queue, no downloader involved
INCOMING = ROOT / "incoming"
STATE = ROOT / "state"
QUEUE = STATE / "queue.json"
VERDICTS = STATE / "verdicts.json"
BOTCONFIG = STATE / "botconfig.json"
OFFSET = STATE / "offset"
FIFO = ROOT / "pipe"
ALLOWLIST = ROOT / "allowed_channels.json"

# every segment must share these exactly, or concatenation breaks at the junction.
# 50 is not a taste: it is what the library already is, and this box encodes at
# 0.77x realtime, so anything that forces a re-encode loses to the clock forever.
WIDTH, HEIGHT, FPS = 1280, 720, 50
VFILTER = (
    f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p"
)
# What this costs is paid once and what it buys is kept forever, because
# normalise_in_place overwrites the library file and the original is gone.
# ultrafast disables most of what h264 compresses with, so it needed roughly
# twice the bitrate of a normal preset to look the same, and it did not get it:
# 47 files were written at 2500k ultrafast before this was noticed and cannot be
# recovered without downloading them again. veryfast is the point where the
# curve flattens on this box, and the bitrate rise matters more at 50fps than it
# would at 30 because every frame gets half the bits.
# The change is safe for what is already done: matches_target compares codec,
# size and rate, never bitrate, so the 47 stay conformant and keep being
# remuxed rather than being dragged through this again.
ENCODE = [
    "-vf", VFILTER,
    "-c:v", "libx264", "-preset", "veryfast", "-b:v", "3800k",
    # a live stream is judged on its worst moment, not its average, so the peak
    # is capped rather than left to the bitrate target alone
    "-maxrate", "4500k", "-bufsize", "9000k",
    "-g", str(FPS * 2), "-keyint_min", str(FPS * 2), "-sc_threshold", "0",
    "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
]
# A source already in exactly that shape needs no picture work at all. Only the
# audio is touched, because the sources carry opus and MPEG-TS will not take it,
# and transcoding sound costs almost nothing next to transcoding a picture.
REMUX = ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
CHUNK_SECONDS = 300
# stop preparing once this much unplayed video is on disk
AHEAD_LIMIT_SECONDS = 2 * 3600

# --- limits that bound what chat can do to the machine -------------------
MAX_QUEUE = 200            # total items retained, oldest finished ones pruned
MAX_PENDING_PER_USER = 3   # stops one person filling the queue alone
ADD_COOLDOWN_SECONDS = 60  # per user, between two accepted additions
MAX_DURATION_SECONDS = 4 * 3600
MAX_MESSAGE_CHARS = 500    # anything longer is dropped unread
SKIP_MIN_VOTERS = 3
SKIP_WINDOW_SECONDS = 180
SKIP_COOLDOWN_SECONDS = 120
# Every accepted item costs prep one yt-dlp metadata call before it can be
# refused, and a refused item frees its pending slot at once, so the per-user
# cap alone never runs out. These three bound the work strangers can order.
MAX_UNRESOLVED = 12          # items still waiting for prep, caps the backlog
REJECT_WINDOW_SECONDS = 300
MAX_REJECTS_IN_WINDOW = 5    # past this, the user stops getting answers
REJECT_SILENCE_SECONDS = 600
MAX_BANNED = 500             # the ban list grows with distinct chatters
MAX_VERDICTS = 2000
# a burst of chat must not turn into a burst of disk writes
FLUSH_INTERVAL_SECONDS = 1.0
# refuse to prepare more video when the disk gets this low
MIN_FREE_BYTES = 4 * 1024 ** 3
# a wall clock on one encode, so a single hostile input cannot pin the machine
# and stall everything behind it. Scaled by the video's own length.
ENCODE_TIMEOUT_FACTOR = 4
ENCODE_TIMEOUT_FLOOR = 30 * 60
ENCODE_TIMEOUT_CEILING = 8 * 3600

# A YouTube id is exactly these 11 characters. Anything else never reaches the
# downloader: the id is extracted and a canonical URL is rebuilt from scratch,
# so no part of a chat message is ever passed through as a URL.
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# a channel id is stable and cannot be reassigned, unlike a handle or a name
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_URL_PATTERNS = (
    re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/watch\?(?:.*&)?v=([A-Za-z0-9_-]{11})(?:&|$)"),
    re.compile(r"^https?://(?:www\.)?youtu\.be/([A-Za-z0-9_-]{11})(?:\?|$)"),
    re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/shorts/([A-Za-z0-9_-]{11})(?:\?|$)"),
    re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/live/([A-Za-z0-9_-]{11})(?:\?|$)"),
)


def canonical_youtube_url(raw):
    """Return (video_id, canonical_url) or (None, reason).

    Deliberately strict. A playlist, a channel, a shortened link, a file:// path
    or any other host is refused rather than normalised: the downloader supports
    a thousand sites and several URL schemes, and none of them belong here.
    """
    if not isinstance(raw, str):
        return None, "invalid"
    raw = raw.strip()
    if len(raw) > 300 or any(ord(c) < 0x20 for c in raw):
        return None, "invalid"
    if VIDEO_ID.match(raw):  # a bare id is convenient and just as safe
        return raw, f"https://www.youtube.com/watch?v={raw}"
    for pattern in _URL_PATTERNS:
        found = pattern.match(raw)
        if found:
            return found.group(1), f"https://www.youtube.com/watch?v={found.group(1)}"
    return None, "only single YouTube videos are accepted"


def clean_text(raw, limit=120):
    """Strip control characters from anything that will be displayed or drawn."""
    if not isinstance(raw, str):
        return ""
    return "".join(c for c in raw if ord(c) >= 0x20 and c != "\x7f")[:limit].strip()


def load_allowlist():
    """Channel ids allowed on air, as {id: note}.

    Returns an empty mapping when the file is missing or unreadable, and an
    empty mapping allows nothing. That is the point: a broken or absent list
    must take the channel off the air, never open it up. Deciding a video is
    acceptable from its title or its words is not possible, so the only workable
    control is who published it.
    """
    try:
        data = json.loads(ALLOWLIST.read_text())
    except (OSError, ValueError):
        return {}
    channels = data.get("channels")
    if not isinstance(channels, dict):
        return {}
    return {str(k): str(v)[:120] for k, v in channels.items()
            if isinstance(k, str) and CHANNEL_ID.match(k)}


def channel_allowed(channel_id):
    return bool(channel_id) and channel_id in load_allowlist()


def env(filename=".env"):
    """Read one ~/vodloop env file into a dict. Values never get logged.

    The name is a parameter because the settings are split across two files:
    .env holds the channel and ingest configuration, bus.env holds the OAuth
    application credentials. Callers that need both read both.
    """
    out = {}
    path = ROOT / filename
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def load_queue():
    if QUEUE.exists():
        return json.loads(QUEUE.read_text())
    return {"items": [], "seq": 0}


def save_queue(q):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=1))
    tmp.replace(QUEUE)  # atomic, so a reader never sees a half-written queue


def load_botconfig():
    """Settings the panel wrote, or {} if there are none or the file is broken.

    Nothing here is trusted to be sane: chatlogic.setting() falls back to the
    compiled constant for every value it does not recognise, so a corrupt file
    leaves the bot running exactly as it was rather than half-configured.
    """
    try:
        data = json.loads(BOTCONFIG.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_verdicts():
    """What prep already learned about a video id: {"ok": bool, "reason": str}.

    prep is the only writer, chat only reads. That way a video the allowlist
    already refused is refused again for free, instead of buying another
    yt-dlp call every time someone pastes it.
    """
    try:
        data = json.loads(VERDICTS.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_verdict(video_id, ok, reason=""):
    if not VIDEO_ID.match(video_id or ""):
        return
    verdicts = load_verdicts()
    verdicts[video_id] = {"ok": bool(ok), "reason": str(reason)[:80]}
    if len(verdicts) > MAX_VERDICTS:
        # plain insertion order: the oldest learned verdicts go first
        for stale in list(verdicts)[: len(verdicts) - MAX_VERDICTS]:
            verdicts.pop(stale, None)
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = VERDICTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(verdicts))
    tmp.replace(VERDICTS)


def read_offset():
    try:
        return float(OFFSET.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def write_offset(value):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = OFFSET.with_suffix(".tmp")
    tmp.write_text(f"{value:.3f}")
    tmp.replace(OFFSET)


def ready_segments():
    """Prepared chunks, in playback order."""
    if not SEGMENTS.exists():
        return []
    return sorted(SEGMENTS.glob("*.ts"))

# One definition, used by prep to decide what it may queue and by the chat to
# decide what it may offer. Two copies would drift, and the drift shows up as a
# file that plays but cannot be asked for, or the reverse.
MEDIA_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".ts", ".m4v", ".avi"}


def library():
    """What the chat may ask for, numbered.

    The number is the position in the sorted listing, so it is stable between
    two messages and survives a restart. It is not an id: adding a file shifts
    the ones after it, which is why the listing is what people read from.

    Only files under VODLOOP_LIBRARY are ever offered. That is what keeps the
    channel to one group's material: not a rule about what people may type, but
    the absence of any way to name anything else.
    """
    root = os.environ.get("VODLOOP_LIBRARY")
    if not root:
        return []
    folder = pathlib.Path(root)
    if not folder.is_dir():
        return []
    files = sorted(p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES)
    return [{"n": n, "title": pretty_title(p.stem), "path": str(p)}
            for n, p in enumerate(files, 1)]

# What a downloader leaves on a filename and a reader does not want: the video
# id it appends to keep names unique, the uploader handle, and the underscores
# it uses because a space is awkward in a shell.
_TRAILING_ID = re.compile(r"-[A-Za-z0-9_-]{11}$")
_TRAILING_HANDLE = re.compile(r"[-_]@[A-Za-z0-9_.-]+$")


def pretty_title(stem):
    """A filename turned into something worth reading in chat.

    Everything stripped here is an artefact of how the file arrived, never part
    of what the video is. The result is also what the search matches on, so a
    person can type the words they can see.
    """
    text = _TRAILING_ID.sub("", str(stem))
    text = _TRAILING_HANDLE.sub("", text)
    text = text.replace("_", " ").replace(".", " ")
    text = " ".join(text.split()).strip(" -")
    return clean_text(text or str(stem), 120)
