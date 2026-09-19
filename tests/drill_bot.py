#!/usr/bin/env python3
"""Drive every chat command through the real bot against the real channel.

Reads the live pipeline (what is on air, what is on the shelf) so the answers
are the ones viewers would get, but writes nowhere near it: the bot's state, its
skip lever and its pick lever are all pointed at a scratch directory, so a test
vote cannot move the wire.
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, "/home/ubuntu/v2/bin")

import chan  # noqa: E402
import bot  # noqa: E402
import kickapi  # noqa: E402

scratch = pathlib.Path(tempfile.mkdtemp(prefix="botdrill-"))
bot.STATE = scratch / "bot.json"
bot.SKIP = scratch / "skip"
bot.PICK = scratch / "pick"

posted = []
kickapi.say = lambda broadcaster, text, reply=None: posted.append(text) or True
bot.kickapi.viewers = lambda slug: 8          # a plausible audience
bot._last_said[0] = 0                         # no pacing sleep in the drill

VIEWER = 1001


def send(text, user=VIEWER, badges=(), quiet=False):
    posted.clear()
    bot.handle({
        "message_id": f"m{time.time()}{user}{text}",
        "broadcaster": {"user_id": bot.BROADCASTER},
        "sender": {"user_id": user, "username": f"viewer{user}", "is_anonymous": False,
                   "identity": {"badges": [{"type": b} for b in badges]}},
        "content": text,
    })
    answer = posted[0] if posted else None
    if not quiet:
        print(f"  {text:<14} -> {answer}")
    return answer


def clear_cooldown():
    data = bot.load()
    data["users"] = {}
    bot.save(data)


print("== what a viewer sees")
for command in ("!help", "!vod", "!list", "!source", "!stats"):
    send(command)
    clear_cooldown()

print()
print("== an unknown command and a plain message are ignored")
print(f"  !banana        -> {send('!banana', quiet=True)}")
clear_cooldown()
print(f"  hello          -> {send('hello', quiet=True)}")
clear_cooldown()

print()
print("== the per-viewer cooldown")
send("!vod")
second = send("!vod", quiet=True)
print(f"  !vod again     -> {second}  (silence expected inside {bot.USER_COOLDOWN}s)")
clear_cooldown()

print()
print("== an anonymous account carries no voice")
posted.clear()
bot.handle({"message_id": "anon1", "broadcaster": {"user_id": bot.BROADCASTER},
            "sender": {"is_anonymous": True, "user_id": 0}, "content": "!vote"})
print(f"  !vote anon     -> {posted[0] if posted else None}")

print()
print("== the vote, with the real guards against the real shelf")
print(f"  unseen on the shelf: {bot.unseen_hours()} h over {len(bot.shelf())} files")
live = bot.playing()
print(f"  on air: {live['title'][:44] if live else None}"
      f"  elapsed {int(live['elapsed'] / 60) if live else 0} min")
print(f"  threshold at 8 viewers: {bot.threshold()} votes")
send("!vote")
clear_cooldown()

print()
print("== the same guards, forced past the shelf being empty")
real_unseen = bot.unseen_hours
bot.unseen_hours = lambda: 6
try:
    live = bot.playing()
    if live and live["elapsed"] < bot.SKIP_MIN_AIRED:
        send("!vote")
        clear_cooldown()
        print("  (the hour is young, so the guard above is the one that answered)")
    data = bot.load()
    data["users"] = {}
    bot.save(data)
    # age the current hour so the vote may open
    real_playing = bot.playing
    bot.playing = lambda: {"title": "t", "hour": 1, "hours": 4, "vid": "x",
                           "elapsed": bot.SKIP_MIN_AIRED + 60, "started": 0, "name": "t.mkv"}
    print(f"  !vote          -> {send('!vote', user=2001, quiet=True)}")
    print(f"  !vote same     -> {send('!vote', user=2001, quiet=True)}   (one voice per account)")
    print(f"  !vote u2       -> {send('!vote', user=2002, quiet=True)}")
    print(f"  !vote u3       -> {send('!vote', user=2003, quiet=True)}")
    print(f"  skip lever written: {bot.SKIP.exists()}")
    print(f"  !vote again    -> {send('!vote', user=2004, quiet=True)}   (cooldown after a skip)")
    bot.SKIP.unlink(missing_ok=True)
    print()
    print("== a moderator carries !force, a viewer does not")
    print(f"  !force viewer  -> {send('!force', user=2005, quiet=True)}  lever={bot.SKIP.exists()}")
    print(f"  !force mod     -> {send('!force', user=2006, badges=('moderator',), quiet=True)}"
          f"  lever={bot.SKIP.exists()}")
    bot.SKIP.unlink(missing_ok=True)
    bot.playing = real_playing
finally:
    bot.unseen_hours = real_unseen

print()
print("== !pick chooses what follows")
rows = bot.shelf()
if rows:
    print(f"  !pick 1        -> {send('!pick 1', user=3001, quiet=True)}")
    print(f"  !pick 99       -> {send('!pick 99', user=3002, quiet=True)}")
    print(f"  !pick abc      -> {send('!pick abc', user=3003, quiet=True)}")
else:
    print(f"  !pick 1        -> {send('!pick 1', user=3001, quiet=True)}  (shelf empty)")

print()
print("== the title the channel would carry")
print(f"  {bot.wanted_title()}")
print()
print(f"nothing written outside {scratch}:")
print(f"  live skip lever present: {(chan.STATE / 'skip').exists()}")
print(f"  live pick lever present: {(chan.STATE / 'pick').exists()}")
