#!/usr/bin/env python3
"""Read Kick chat events and apply them to the queue.

Transport only: every decision lives in chatlogic, which is tested separately.

Events arrive from kickbus on loopback. kickbus is what verifies Kick's RSA
signature and drops replays, so this process trusts its local socket and nothing
else. It never opens a listening port of its own.

With --stdin it reads newline-delimited JSON instead, which is how the command
handling gets exercised without a live webhook.
"""
import json
import sys
import time
import urllib.error
import urllib.request

import chatlogic
import common

SSE_URL = "http://127.0.0.1:8787/events?type=chat.message.sent"
STATE_FILE = common.STATE / "chat.json"
RECONNECT_SECONDS = 5
MAX_EVENT_BYTES = 64 * 1024


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return chatlogic.new_state()
    base = chatlogic.new_state()
    base.update({k: v for k, v in state.items() if k in base})
    return base


def save_state(state):
    common.STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def as_message(payload):
    """Map a Kick chat event onto what chatlogic expects, tolerating shape drift."""
    if not isinstance(payload, dict):
        return None
    sender = payload.get("sender") or {}
    if not isinstance(sender, dict):
        return None
    return {
        "user_id": sender.get("user_id") or sender.get("id"),
        "username": sender.get("username") or sender.get("slug") or "",
        "text": payload.get("content") or payload.get("message") or "",
    }


class Store:
    """Queue and chat state held in memory, written back at a bounded rate.

    Reloading and rewriting two files for every chat line turns a message flood
    into a disk flood, which is a cheaper way to hurt this box than anything the
    commands themselves allow. Messages are still all processed in order; only
    the writing is paced, and a pending change is always flushed before the
    process can sit idle.
    """

    def __init__(self):
        self.queue = common.load_queue()
        self.state = load_state()
        self.dirty = False
        self.flushed_at = 0.0
        self.stamp = self._mtime()

    def _mtime(self):
        try:
            return common.QUEUE.stat().st_mtime
        except OSError:
            return 0.0

    def refresh(self):
        """Pick up edits made by prep or the panel, without losing our own."""
        if not self.dirty and self._mtime() != self.stamp:
            self.queue = common.load_queue()
            self.stamp = self._mtime()

    def flush(self, now, force=False):
        if not self.dirty:
            return
        if not force and now - self.flushed_at < common.FLUSH_INTERVAL_SECONDS:
            return
        common.save_queue(self.queue)
        save_state(self.state)
        self.stamp = self._mtime()
        self.flushed_at = now
        self.dirty = False


def apply(payload, mods, store):
    message = as_message(payload)
    if message is None:
        return
    now = time.time()
    store.refresh()
    reply, changed = chatlogic.handle(message, store.queue, store.state, mods, now)
    if changed:
        store.dirty = True
    store.flush(now)
    if reply:
        # nothing writes back to chat yet; the overlay is the feedback channel
        print(f"[{message['user_id']}] {reply}", flush=True)


def stream(mods, store):
    """Follow the SSE feed, reconnecting for as long as this process lives."""
    while True:
        try:
            with urllib.request.urlopen(SSE_URL, timeout=60) as response:
                for raw in response:
                    line = raw[:MAX_EVENT_BYTES].decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except ValueError:
                        continue  # a malformed frame is not worth dying over
                    apply(event.get("payload", event), mods, store)
        except (urllib.error.URLError, OSError, TimeoutError):
            # the feed went quiet, so nothing is waiting on the pacing any more
            store.flush(time.time(), force=True)
            time.sleep(RECONNECT_SECONDS)


def main():
    mods = tuple(
        m.strip() for m in common.env().get("VODLOOP_MODS", "").split(",") if m.strip()
    )
    store = Store()
    if "--stdin" in sys.argv:
        for line in sys.stdin:
            line = line.strip()
            if line:
                try:
                    apply(json.loads(line), mods, store)
                except ValueError:
                    continue
        store.flush(time.time(), force=True)
        return
    stream(mods, store)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
