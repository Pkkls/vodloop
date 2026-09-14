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
BOTCONFIG = STATE / "botconfig.json"
OFFSET = STATE / "offset"
FIFO = ROOT / "pipe"
ALLOWLIST = ROOT / "allowed_channels.json"

# One code tree serves every channel on this server. A unit or a crontab line
# says which channel it runs for through these, and left unset they name the
# first channel, so nothing that predates a second one reads anything new.
LIBRARY_DIR = pathlib.Path(os.environ.get("VODLOOP_LIBRARY") or "/home/ubuntu/videos")
_UNIT = os.environ.get("VODLOOP_UNIT") or "vodloop-%s"
# prefixed to what the Telegram bot says, since several channels share one chat
LABEL = os.environ.get("VODLOOP_LABEL", "")
# This channel's share of the disk, library and prepared chunks together. With
# two channels on one filesystem, free space stops being a channel's own
# measure: the neighbour's arrivals would have this one retire its files. 0 is
# no share, and then the free-space rules decide alone, as they always did.
BUDGET_BYTES = int(float(os.environ.get("VODLOOP_BUDGET_GB") or 0) * 1024 ** 3)


def unit(role):
    """This channel's systemd unit for a role: push, feed, prep, chat."""
    return _UNIT % role


def bytes_used(library=None):
    """What this channel holds against its share: every file under its library,
    arrivals still staged inside it included, and its prepared chunks."""
    total = 0
    for folder in (pathlib.Path(library or LIBRARY_DIR), SEGMENTS):
        for path in folder.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue  # removed between the listing and the stat
    return total

# These three used to be a promise every segment had to keep, and keeping it
# meant re-encoding almost everything that ever arrived. As of 2026-09-08 they
# are what an encode produces when there is no way around one, not what a file
# has to prove before it can be copied: matches_target asks for h264, a size
# within this, and a frame rate Kick will actually pass through.
#
# FPS went from 50 to 60 the same day, and it is the only one of the three that
# had been actively wrong. See fps_supported below: 50 is not a rung Kick has,
# so it re-encoded everything we sent and the picture a viewer got was its work,
# not ours. Nothing downloaded is ever 50; only what a PC encoded was.
#
# A frame rate change at a junction was measured going through the real feeder
# and pusher chain cleanly, as was a resolution change, though the ingest reads
# resolution from the sequence header and no test here can ask it: that one is
# kil's decision, taken with the risk stated, and quality.py watches for it.
# 50 is not a taste: it is what the library already is, and this box encodes at
# 0.77x realtime, so anything that forces a re-encode loses to the clock forever.
# 1080p since 2026-09-06. The sources are 1920x1080 at 2400-5400 kbps and were
# being reduced to 720p, which threw the resolution away and under-filled Kick's
# own 720p rendition: it advertises 3423 kbps and was being fed 1425, while the
# 480p rendition next to it advertises 1428, so a player choosing by bandwidth
# had every reason to pick 480p. The whole library was rebuilt from the sources
# on a machine that can encode, because the files here had already been reduced
# and re-encoding those would have been upscaling.
#
# 45 Go of disk does not hold 25.6h at this bitrate, so the rotation is shorter
# on purpose. Measured 2026-09-08: 38 files, 28.1 Go, 21.3h, all of them h264
# 1920x1080 at 50, video 2661 to 2819 kbps with a median of 2778, audio aac LC
# 44100 stereo at 141 to 160 kbps. The "median 4890 kbps, 37 files, about 14h"
# this used to claim was what the rebuild aimed at and never what landed on the
# disk, so every later reading of the library looked like a regression against
# a number that had never been true.
WIDTH, HEIGHT, FPS = 1920, 1080, 60
# The floor of the four profiles, 480p30 being the lowest. The ceiling alone let
# a 640x360 file through, because YouTube keeps long VODs in avc1 at 360p only
# and the downloader, pinned to avc1, took that. Measured 2026-09-12 21:17: the
# channel pushing 640x360 at 30 fps and 534 kbps, Kick serving the viewer an
# upscale of it in its 720p60 rung, while the monitor, reading the newest cut
# chunk instead of the one on air, reported 1920x1080 at 4781 kbps.
MIN_HEIGHT = 480


def fps_supported(fps):
    """Whether Kick will pass a source at this frame rate through untouched.

    Its ingest has two rungs, 30 and 60, and its own dashboard says so: "MAL
    CONFIGURE, passer votre frequence d'images d'entree a 30 ou 60 FPS". Fed 50
    it does not refuse the stream, it re-encodes it, and the result is what a
    viewer sees rather than what we sent.

    Measured 2026-09-08 while the library was still 50: the master playlist
    advertised the source rung as 1920x1080 at 3070272 bps, and pulling that
    exact rung returned 1280x720. The rung below it, advertised as 720p60 at
    3422999, returned 852x480 at 25. The whole ladder had collapsed one step,
    and a player picking by bandwidth had every reason to take the 720p60 entry
    because it advertised more than the source did.

    50 was never chosen. It is what a PC encoder produced, and it is the last
    thing in this system that came from that PC. YouTube serves 30 and 60 and
    never 50, so a file downloaded rather than made is already right.

    23.976, 25, 29.97 and 30 all sit on the 30 rung; 59.94 and 60 on the other.
    """
    if fps is None:
        return False
    return fps <= 30.5 or 59.0 <= fps <= 61.0
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
    # Quality-targeted, not bitrate-targeted, and that distinction cost a day.
    # A fixed 3800k on sources that are themselves 670-2400 kbps produced files
    # 4.11x the size of what they came from, inventing weight where there was no
    # detail to keep: 14 Go of library would have become 58 on a 45 Go disk. CRF
    # spends what the picture needs and no more. 21 measured at 0.9935 SSIM
    # against the source, which is as close to transparent as matters.
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
    # a live stream is judged on its worst moment, not its average, so the peak
    # is capped rather than left to the quality target alone
    "-maxrate", "9000k", "-bufsize", "18000k",
    "-g", str(FPS * 2), "-keyint_min", str(FPS * 2), "-sc_threshold", "0",
    "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
]
# A source already in exactly that shape needs no picture work at all. Only the
# audio is touched, because the sources carry opus and MPEG-TS will not take it,
# and transcoding sound costs almost nothing next to transcoding a picture.
#
# 160k, not the 128k this said until 2026-09-08. A library file already carries
# 160k aac, so every pass through the rotation transcoded it down to 128 and the
# loss stacked: segments/00360_00002.ts probed at 131025 while its own source
# probed at 159507. prepare() now copies aac outright and this is only reached
# by a source carrying something else, where matching ENCODE is what stops one
# file sounding different depending on which path prepared it.
# YouTube hands out 1080p60 AVC with an SEI unit on every keyframe, and the
# transport stream muxer emits that unit as a packet of its own carrying no
# timestamp at all. The flv muxer at the far end refuses the first one and the
# pusher exits, which is the 1298 restarts of 2026-09-05. Dropping SEI removes
# that packet and nothing else. Measured 2026-09-14 on a refused file: a five
# minute chunk went from 50 timestamp-less packets to 0 and the pusher from
# exit 1 to exit 0, while the framemd5 over 959 decoded frames is identical
# with and without. Re-encoding also fixes it, at 0.06x realtime this box
# cannot pay for.
DROP_SEI = ["-bsf:v", "filter_units=remove_types=6"]

REMUX = ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"] + DROP_SEI
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
REJECT_WINDOW_SECONDS = 300
MAX_REJECTS_IN_WINDOW = 5    # past this, the user stops getting answers
REJECT_SILENCE_SECONDS = 600
MAX_BANNED = 500             # the ban list grows with distinct chatters
# a burst of chat must not turn into a burst of disk writes
FLUSH_INTERVAL_SECONDS = 1.0
# refuse to prepare more video when the disk gets this low
MIN_FREE_BYTES = 4 * 1024 ** 3
# Where the janitor stops freeing and the collector stops filling once a
# channel has a share of the disk. Free space belongs to every channel on the
# server, so with a share it is a floor and nothing more: aiming higher would
# have one channel retire its own files to make room for its neighbour's
# arrivals. One definition, because the two sides only compose if they agree.
SHARED_FREE_FLOOR_BYTES = MIN_FREE_BYTES + 1024 ** 3
# How much unplayed video must remain after anything deletes anything. This is
# the only thing standing between a self-emptying library and dead air, and it
# is measured in time because time is what the channel actually consumes: one
# second of video per second, forever, whatever the file count says.
#
# A count was the wrong instrument and it froze the rotation. The floor was 18
# files, so a library of fewer than 18 could never retire anything at all: every
# file stayed for ever and the channel replayed the same handful. Measured
# 2026-09-08 with one file left after the 50fps purge, that one file was pinned
# in place by a floor meant to protect it.
#
# An hour, because acquiring a replacement takes about ten to fifteen minutes:
# the board fetches one video per five minute tick and moves it to Oracle at the
# 32 Mbps measured between them. An hour is three or four times what the slowest
# replacement needs, which is the margin a 24/7 channel is worth.
MIN_RUNWAY_SECONDS = 60 * 60
# How many plays a library file gets before the rotation stops preferring it.
# This no longer deletes anything: playing a file counts the play and nothing
# else, and eviction belongs to the janitor, which answers to disk pressure and
# refuses to go below MIN_PLAYABLE_FILES.
#
# It was 1 for a day, and that day is why the split exists. A rerun channel that
# retires each video after one play only works while the supply never stops, and
# on 2026-09-08 YouTube closed its player API at 08:41: every fetch failed for
# the rest of the day, the library was deleted one file at a time until a single
# unplayable one was left, and the channel sat on the standby clip. Nothing here
# was wrong except the assumption that something would always arrive.
#
# The disk is a working set, but a working set of tens of hours: at about
# 450 Mo an hour of 720p, what the janitor keeps is a day or two of rotation.
# That is the difference between a supply outage costing repeats and costing
# silence.
# One play per file. The channel is a rerun channel, not a loop: a viewer who
# comes back should not meet the same video again while anything unseen is on
# the disk. Set to 1 on 2026-09-14 at the owner's request.
#
# This is a preference in the draw, not a rule about deletion. The selector
# falls back to the whole pool when every file has had its play, so a library
# that has been fully seen replays rather than showing the standby clip, and
# nothing here deletes anything: that belongs to the janitor and to disk
# pressure alone. Retiring on play is what emptied the library on 2026-09-08
# when the supply stopped for a day.
MAX_PLAYS = 1
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


def channel_slug():
    """The channel's name in a Kick URL, which is not a constant.

    It was cx247-cx and became cx247vods on 2026-09-06. Everything that asked
    Kick anything kept asking about the old name and got 404 back, which the
    probes reported as "no answer" for hours while the channel was live and
    fine. Hardcoding this once was the mistake; hardcoding it in three files
    would have been three.
    """
    return (os.environ.get("KICK_SLUG")
            or env().get("KICK_SLUG")
            or "cx247vods")


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
# a part keeps its place in the video (".p02of04"), only the id goes
_TRAILING_ID = re.compile(r"-[A-Za-z0-9_-]{11}(?=(?:\.p\d+of\d+)?$)")
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
