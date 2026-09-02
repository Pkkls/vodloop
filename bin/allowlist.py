#!/usr/bin/env python3
"""Manage the channels allowed on air.

    allowlist.py list
    allowlist.py add <channel url, @handle, video url or UC id>
    allowlist.py remove <UC id>

Chat can queue anything YouTube hosts, and nothing about a video's title or its
words reliably says whether it will get the Kick channel taken down. Who
published it does. So the queue is closed by default and opens one channel at a
time, by hand.

Ids are stored, not handles: a handle can be changed or reassigned, a channel id
cannot.
"""
import json
import shutil
import subprocess
import sys

import common

YTDLP = shutil.which("yt-dlp") or str(common.ROOT.parent / ".local/bin/yt-dlp")


def resolve(reference):
    """Turn a channel URL, a handle or a video URL into (channel_id, name)."""
    reference = reference.strip()
    if common.CHANNEL_ID.match(reference):
        return reference, ""
    if reference.startswith("@"):
        reference = f"https://www.youtube.com/{reference}"
    if not reference.startswith(("http://", "https://")):
        return None, "give a channel URL, an @handle, a video URL or a UC id"

    out = subprocess.run(
        [YTDLP, "--no-warnings", "--simulate", "--playlist-items", "1",
         "--print", "%(channel_id)s\t%(channel)s", "--", reference],
        capture_output=True, text=True, timeout=180,
    )
    if out.returncode != 0:
        return None, (out.stderr.strip().splitlines() or ["lookup failed"])[-1][:160]
    line = (out.stdout.strip().splitlines() or [""])[0]
    channel_id, _, name = line.partition("\t")
    if not common.CHANNEL_ID.match(channel_id.strip()):
        return None, f"no channel id found (got {channel_id[:40]!r})"
    return channel_id.strip(), common.clean_text(name, 60)


def save(channels):
    common.ALLOWLIST.parent.mkdir(parents=True, exist_ok=True)
    tmp = common.ALLOWLIST.with_suffix(".tmp")
    tmp.write_text(json.dumps({"channels": channels}, indent=1, ensure_ascii=False))
    tmp.replace(common.ALLOWLIST)


def main(argv):
    channels = common.load_allowlist()
    action = argv[0] if argv else "list"

    if action == "list":
        if not channels:
            print("empty: nothing can be queued until a channel is added")
            return 0
        for channel_id, note in sorted(channels.items(), key=lambda kv: kv[1].lower()):
            print(f"{channel_id}  {note}")
        return 0

    if action == "add" and len(argv) > 1:
        channel_id, name = resolve(argv[1])
        if channel_id is None:
            print(f"refused: {name}")
            return 1
        if channel_id in channels:
            print(f"already allowed: {channel_id}  {channels[channel_id]}")
            return 0
        channels[channel_id] = name or channel_id
        save(channels)
        print(f"allowed: {channel_id}  {channels[channel_id]}")
        return 0

    if action == "remove" and len(argv) > 1:
        target = argv[1].strip()
        if channels.pop(target, None) is None:
            print(f"not in the list: {target}")
            return 1
        save(channels)
        print(f"removed: {target}")
        return 0

    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
