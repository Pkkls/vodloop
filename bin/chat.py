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
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import chatlogic
import common

SSE_URL = "http://127.0.0.1:8787/events?type=chat.message.sent"
USER_AGENT = "vodloop/0.1"
STATE_FILE = common.STATE / "chat.json"
RECONNECT_SECONDS = 5
MAX_EVENT_BYTES = 64 * 1024


class Replier:
    """Posts answers back into Kick chat, on a budget.

    The budget is the point. Every refused command produces an answer, so a
    viewer spamming rubbish would otherwise turn the bot into the flooder and
    get it timed out by Kick. Over budget, answers are dropped rather than
    queued: a late answer to a command nobody remembers is worse than silence.
    """

    TOKEN_URL = "https://id.kick.com/oauth/token"
    CHAT_URL = "https://api.kick.com/public/v1/chat"
    MIN_GAP_SECONDS = 3
    PER_USER_GAP_SECONDS = 30

    def __init__(self, client_id, client_secret, broadcaster_id):
        self.client_id = client_id
        self.client_secret = client_secret
        self.broadcaster_id = broadcaster_id
        self.token = None
        self.token_expires = 0.0
        self.last_sent = 0.0
        self.last_per_user = {}

    @property
    def enabled(self):
        return bool(self.client_id and self.client_secret and self.broadcaster_id)

    def _access_token(self):
        # A user token is the only one that carries chat:write. The app token
        # below is kept as a fallback, but Kick refuses a chat post with it:
        # introspection shows an app token holds no scopes at all.
        import oauth
        user_token = oauth.access_token()
        if user_token:
            return user_token

        now = time.time()
        if self.token and now < self.token_expires:
            return self.token
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }).encode()
        request = urllib.request.Request(self.TOKEN_URL, data=body, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        # Kick answers 403 to the default Python agent. A neutral name passes;
        # claiming to be a browser is what actually gets refused.
        request.add_header("User-Agent", USER_AGENT)
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
        self.token = payload["access_token"]
        # renew a minute early rather than discover expiry mid-answer
        self.token_expires = now + max(60, int(payload.get("expires_in", 3600)) - 60)
        return self.token

    def send(self, text, user_id=None):
        if not self.enabled:
            return False
        now = time.time()
        if now - self.last_sent < self.MIN_GAP_SECONDS:
            return False
        if user_id and now - self.last_per_user.get(user_id, 0) < self.PER_USER_GAP_SECONDS:
            return False
        try:
            # Posting as "bot" is what the docs describe, but Kick answers 500
            # to it today, with or without a broadcaster id. Sending as "user"
            # works, at the cost of the answers appearing under the channel
            # owner's name rather than a separate bot identity. Worth retrying
            # the bot path once Kick stops erroring on it.
            body = json.dumps({"broadcaster_user_id": int(self.broadcaster_id),
                               "content": text[:480], "type": "user"}).encode()
            request = urllib.request.Request(self.CHAT_URL, data=body, method="POST")
            request.add_header("Authorization", f"Bearer {self._access_token()}")
            request.add_header("Content-Type", "application/json")
            request.add_header("User-Agent", USER_AGENT)
            with urllib.request.urlopen(request, timeout=15):
                pass
        except Exception as problem:
            print(f"reply failed: {problem}", flush=True)
            return False
        self.last_sent = now
        if user_id:
            self.last_per_user[user_id] = now
            if len(self.last_per_user) > 2000:
                cutoff = now - self.PER_USER_GAP_SECONDS
                self.last_per_user = {k: v for k, v in self.last_per_user.items() if v > cutoff}
        return True


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


def unwrap(event):
    """Pull the chat payload out of the bus envelope.

    kickbus wraps each event as {id, type, version, broadcaster, data}. Reading
    the envelope as if it were the payload finds no sender, which the command
    handler then rejects without a word: the chat looks connected and simply
    never answers.
    """
    if not isinstance(event, dict):
        return None
    for key in ("data", "payload"):
        inner = event.get(key)
        if isinstance(inner, dict):
            return inner
    return event


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


def apply(event, mods, store, replier=None):
    message = as_message(unwrap(event))
    if message is None:
        return
    now = time.time()
    store.refresh()
    reply, changed = chatlogic.handle(message, store.queue, store.state, mods, now)
    if changed:
        store.dirty = True
    store.flush(now)
    if reply:
        print(f"[{message['user_id']}] {reply}", flush=True)
        if replier is not None:
            replier.send(reply, str(message["user_id"]))


def stream(mods, store, replier=None):
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
                    apply(event, mods, store, replier)
        except (urllib.error.URLError, OSError, TimeoutError):
            # the feed went quiet, so nothing is waiting on the pacing any more
            store.flush(time.time(), force=True)
            time.sleep(RECONNECT_SECONDS)


def main():
    mods = tuple(
        m.strip() for m in common.env().get("VODLOOP_MODS", "").split(",") if m.strip()
    )
    config = common.env()

    def setting(name):
        # credentials come from the unit's EnvironmentFile, never from a file
        # this process reads itself, so they stay out of .env and out of argv
        return os.environ.get(name) or config.get(name, "")

    replier = Replier(setting("KICK_CLIENT_ID"), setting("KICK_CLIENT_SECRET"),
                      setting("KICK_USER_ID"))
    if replier.enabled:
        print("answering in chat is on", flush=True)

    store = Store()
    if "--stdin" in sys.argv:
        # replay mode never writes to the real chat
        for line in sys.stdin:
            line = line.strip()
            if line:
                try:
                    apply(json.loads(line), mods, store)
                except ValueError:
                    continue
        store.flush(time.time(), force=True)
        return
    stream(mods, store, replier)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
