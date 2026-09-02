"""Shared paths, config and state helpers for vodloop."""
import json
import os
import pathlib
import re

ROOT = pathlib.Path(os.environ.get("VODLOOP_ROOT", pathlib.Path.home() / "vodloop"))
SEGMENTS = ROOT / "segments"
STATE = ROOT / "state"
QUEUE = STATE / "queue.json"
OFFSET = STATE / "offset"
FIFO = ROOT / "pipe"
ALLOWLIST = ROOT / "allowed_channels.json"

# every segment must share these exactly, or concatenation breaks at the junction
WIDTH, HEIGHT, FPS = 1280, 720, 30
VFILTER = (
    f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p"
)
ENCODE = [
    "-vf", VFILTER,
    "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "2500k",
    "-g", str(FPS * 2), "-keyint_min", str(FPS * 2), "-sc_threshold", "0",
    "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
]
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
# a burst of chat must not turn into a burst of disk writes
FLUSH_INTERVAL_SECONDS = 1.0
# refuse to prepare more video when the disk gets this low
MIN_FREE_BYTES = 4 * 1024 ** 3

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


def env():
    """Read ~/vodloop/.env into a dict. Values never get logged."""
    out = {}
    path = ROOT / ".env"
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
