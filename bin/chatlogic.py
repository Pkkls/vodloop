#!/usr/bin/env python3
"""Command handling for the Kick chat, with no I/O so it can be tested directly.

Threat model, because this is the one surface strangers can reach:

  URLs        a chat message never reaches the downloader. The video id is
              extracted and a canonical URL is rebuilt, so file://, other hosts,
              internal addresses, playlists and channels are structurally
              impossible rather than merely filtered.
  arguments   the rebuilt URL always starts with "https://", so it can never be
              read as a command-line flag by the downloader.
  flooding    per-user cooldown, per-user pending cap, global queue cap.
  monopoly    one person cannot hold more than a few pending items, and an
              over-long video is refused when its duration is known.
  votes       one vote per user per item, kept as a set of ids, so repeating a
              command does not stack.
  skip abuse  a skip needs several distinct voters inside a window, and a
              cooldown follows each skip so a small group cannot chain them.
  display     titles and names are stripped of control characters here, and
              escaped again wherever they are rendered or drawn.

Anything unrecognised is ignored in silence: a chat is not a shell prompt.
"""
import time

import common

# Every string returned from here can land in chat, so they are all written in
# the channel's language rather than the one the code is discussed in.
HELP = "!play <youtube link>  |  !vote <n>  |  !skip"


def new_state():
    return {"last_add": {}, "skip_votes": {}, "last_skip": 0.0, "banned": [],
            "rejects": {}, "muted": {}}


def _refuse(state, user_id, now, reply, config=None):
    """Record a refusal and answer it, or fall silent if the user keeps earning them.

    Answering is the thing being farmed: every refusal that carries a message is
    a free reply, and a reply is what makes flooding worth the trouble. So past
    the budget the user stops existing for a while, with no message announcing it.
    """
    window = setting(config, "REJECT_WINDOW_SECONDS")
    stamps = [t for t in state["rejects"].get(user_id, []) if now - t < window]
    stamps.append(now)
    state["rejects"][user_id] = stamps
    if len(state["rejects"]) > 5000:
        state["rejects"] = {
            u: s for u, s in state["rejects"].items()
            if s and now - s[-1] < window
        }
    if len(stamps) >= setting(config, "MAX_REJECTS_IN_WINDOW"):
        state["muted"][user_id] = now + setting(config, "REJECT_SILENCE_SECONDS")
        return None, True
    return reply, True


def setting(config, name):
    """A tunable, from the panel's config if it set one, else the compiled default.

    The panel validates every value before writing the file, and this falls back
    to the constant for anything missing or unreadable, so a config that is
    absent, empty or corrupt changes nothing about how the bot behaves.
    """
    value = (config or {}).get(name)
    return getattr(common, name) if not isinstance(value, int) or isinstance(value, bool) \
        else value


def _pending_of(queue, user_id):
    live = ("pending", "preparing", "ready")
    return [i for i in queue["items"] if i.get("by") == user_id and i["status"] in live]


def _already_queued(queue, video_id):
    live = ("pending", "preparing", "ready")
    return any(i.get("video_id") == video_id and i["status"] in live for i in queue["items"])


def _prune(queue):
    """Keep the queue file bounded: drop the oldest finished entries."""
    if len(queue["items"]) <= common.MAX_QUEUE:
        return
    finished = [i for i in queue["items"] if i["status"] in ("played", "error")]
    excess = len(queue["items"]) - common.MAX_QUEUE
    drop = {id(i) for i in finished[:excess]}
    queue["items"] = [i for i in queue["items"] if id(i) not in drop]


def handle(message, queue, state, mods=(), now=None, verdicts=None, config=None):
    """Apply one chat message. Returns (reply or None, whether state changed)."""
    now = time.time() if now is None else now

    user_id = str(message.get("user_id") or "").strip()
    text = message.get("text")
    if not user_id or not isinstance(text, str):
        return None, False
    if len(text) > setting(config, "MAX_MESSAGE_CHARS"):
        return None, False  # dropped unread, not truncated and parsed

    text = common.clean_text(text, setting(config, "MAX_MESSAGE_CHARS"))
    if not text.startswith("!"):
        return None, False
    if user_id in state["banned"]:
        return None, False
    state.setdefault("rejects", {})
    muted = state.setdefault("muted", {})
    if muted.get(user_id, 0) > now:
        return None, False  # silence, not an error: an error is still an answer
    if muted:
        expired = [u for u, until in muted.items() if until <= now]
        for u in expired:
            muted.pop(u, None)

    command, _, argument = text.partition(" ")
    command = command.lower()
    argument = argument.strip()
    is_mod = user_id in mods

    if command in ("!play", "!add"):
        return _add(argument, queue, state, user_id, message, now, is_mod, verdicts, config)
    if command in ("!vote", "!v"):
        return _vote(argument, queue, user_id)
    if command == "!skip":
        return _skip(queue, state, user_id, now, is_mod, config)
    if command == "!ban" and is_mod:
        target = common.clean_text(argument, 64)
        if target and target not in state["banned"]:
            state["banned"].append(target)
            # the list grows with distinct chatters and is never emptied
            del state["banned"][: max(0, len(state["banned"]) - setting(config, "MAX_BANNED"))]
            return f"{target} can no longer use the commands", True
        return None, False
    if command == "!help":
        custom = (config or {}).get("HELP")
        return (custom if isinstance(custom, str) and custom.strip() else HELP), False
    # Commands the panel added. A name maps to a fixed sentence and nothing
    # else: it is looked up after every built-in, so nothing here can shadow
    # !play or !ban, and the value is sent to chat as text, never executed.
    commands = (config or {}).get("commands")
    if isinstance(commands, dict):
        reply = commands.get(command)
        if isinstance(reply, str) and reply.strip():
            return common.clean_text(reply, 200), False
    return None, False


def _add(argument, queue, state, user_id, message, now, is_mod, verdicts=None, config=None):
    video_id, result = common.canonical_youtube_url(argument)
    if video_id is None:
        return _refuse(state, user_id, now, result, config)

    # What prep already decided about this video, for free. Without this, a
    # video the allowlist refuses costs another yt-dlp call every single time
    # somebody pastes it, and the answer is still "added".
    known = (verdicts or {}).get(video_id)
    if isinstance(known, dict) and not known.get("ok", True):
        return _refuse(state, user_id, now, known.get("reason") or "refused", config)

    if not is_mod:
        waited = now - state["last_add"].get(user_id, 0)
        if waited < setting(config, "ADD_COOLDOWN_SECONDS"):
            return _refuse(state, user_id, now,
                           f"wait {int(setting(config, 'ADD_COOLDOWN_SECONDS') - waited)}s", config)
        if len(_pending_of(queue, user_id)) >= setting(config, "MAX_PENDING_PER_USER"):
            return _refuse(state, user_id, now, "you already have enough waiting", config)

    if _already_queued(queue, video_id):
        return _refuse(state, user_id, now, "already in the queue", config)
    live = [i for i in queue["items"] if i["status"] in ("pending", "preparing", "ready")]
    if len(live) >= common.MAX_QUEUE:
        return "the queue is full", False
    # unresolved items are the ones prep still owes a metadata call to. The
    # queue cap is far too high to bound that work on its own.
    unresolved = [i for i in queue["items"] if i["status"] == "pending"]
    if not is_mod and len(unresolved) >= setting(config, "MAX_UNRESOLVED"):
        return "hold on, too many waiting to be checked", False

    queue["seq"] += 1
    queue["items"].append({
        "id": queue["seq"],
        "url": result,
        "video_id": video_id,
        "status": "pending",
        "by": user_id,
        "by_name": common.clean_text(message.get("username"), 40),
        "votes": [],
        "added_at": now,
    })
    state["last_add"][user_id] = now
    # the cooldown table would otherwise grow with every distinct chatter
    if len(state["last_add"]) > 5000:
        cutoff = now - setting(config, "ADD_COOLDOWN_SECONDS")
        state["last_add"] = {k: v for k, v in state["last_add"].items() if v > cutoff}
    _prune(queue)
    return "added", True


def _vote(argument, queue, user_id):
    if not argument.isdigit() or len(argument) > 9:
        return None, False
    wanted = int(argument)
    for item in queue["items"]:
        if item["id"] == wanted and item["status"] in ("pending", "preparing", "ready"):
            voters = item.setdefault("votes", [])
            if user_id in voters:
                return None, False  # repeating the command changes nothing
            voters.append(user_id)
            return None, True
    return None, False


def _skip(queue, state, user_id, now, is_mod, config=None):
    if is_mod:
        state["skip_votes"] = {}
        state["last_skip"] = now
        return "skipping", True

    since_last = now - state["last_skip"]
    if since_last < setting(config, "SKIP_COOLDOWN_SECONDS"):
        return None, False

    # keep only votes inside the window, so old ones cannot be accumulated
    state["skip_votes"] = {
        voter: when for voter, when in state["skip_votes"].items()
        if now - when < setting(config, "SKIP_WINDOW_SECONDS")
    }
    state["skip_votes"][user_id] = now

    if len(state["skip_votes"]) >= setting(config, "SKIP_MIN_VOTERS"):
        state["skip_votes"] = {}
        state["last_skip"] = now
        return "skipping", True
    remaining = setting(config, "SKIP_MIN_VOTERS") - len(state["skip_votes"])
    return f"{remaining} more vote(s) to skip", True


def playback_order(queue):
    """Prepared first, then most voted, then oldest. Used by prep to pick next."""
    pending = [i for i in queue["items"] if i["status"] == "pending"]
    return sorted(pending, key=lambda i: (-len(i.get("votes") or []), i.get("added_at", 0)))
