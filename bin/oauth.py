#!/usr/bin/env python3
"""One-time authorisation so the bot can write in chat.

An app token, the kind kickbus uses for webhooks, carries no scopes: introspection
returns only {active, client_id, token_type: app}. Posting a chat message needs a
user token, which only exists once the channel owner has approved the app in a
browser. That approval happens once; the refresh token then keeps it alive.

    python3 bin/oauth.py url     print the link to open
    python3 bin/oauth.py show    report what is stored, without printing secrets
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request

import common

AUTHORIZE = "https://id.kick.com/oauth/authorize"
TOKEN = "https://id.kick.com/oauth/token"
SCOPES = "chat:write"
USER_AGENT = "vodloop/0.1"
TOKEN_FILE = common.STATE / "kick_user_token.json"
PENDING_FILE = common.STATE / "kick_oauth_pending.json"


def setting(name):
    return os.environ.get(name) or common.env().get(name, "")


def redirect_uri():
    return setting("KICK_REDIRECT_URI") or "https://vodloop.kicknosubviewer.duckdns.org"


def _write_private(path, payload):
    common.STATE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def build_url():
    """Authorisation link, with the PKCE verifier stashed for the callback."""
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    _write_private(PENDING_FILE, {"verifier": verifier, "state": state, "at": time.time()})
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": setting("KICK_CLIENT_ID"),
        "redirect_uri": redirect_uri(),
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    return f"{AUTHORIZE}?{query}"


def _post(fields):
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(TOKEN, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    # Kick answers 403 to the default Python agent; a neutral name passes
    request.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def exchange(code, state):
    """Turn the callback's code into a stored user token. Returns (ok, message)."""
    try:
        pending = json.loads(PENDING_FILE.read_text())
    except (OSError, ValueError):
        return False, "no authorisation in progress"
    # the state parameter is what stops a stranger's code being planted here
    if not secrets.compare_digest(str(state or ""), str(pending.get("state", ""))):
        return False, "state mismatch"
    if time.time() - pending.get("at", 0) > 900:
        return False, "authorisation expired, start again"

    try:
        payload = _post({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "client_id": setting("KICK_CLIENT_ID"),
            "client_secret": setting("KICK_CLIENT_SECRET"),
            "code_verifier": pending["verifier"],
        })
    except Exception as problem:
        return False, f"exchange failed: {type(problem).__name__}"

    payload["obtained_at"] = time.time()
    _write_private(TOKEN_FILE, payload)
    PENDING_FILE.unlink(missing_ok=True)
    return True, "authorised"


def access_token():
    """A valid user access token, refreshing it when it is close to expiry."""
    try:
        stored = json.loads(TOKEN_FILE.read_text())
    except (OSError, ValueError):
        return None
    age = time.time() - stored.get("obtained_at", 0)
    if age < max(60, int(stored.get("expires_in", 3600)) - 120):
        return stored.get("access_token")
    if not stored.get("refresh_token"):
        return None
    try:
        payload = _post({
            "grant_type": "refresh_token",
            "refresh_token": stored["refresh_token"],
            "client_id": setting("KICK_CLIENT_ID"),
            "client_secret": setting("KICK_CLIENT_SECRET"),
        })
    except Exception:
        return None
    payload["obtained_at"] = time.time()
    payload.setdefault("refresh_token", stored["refresh_token"])
    _write_private(TOKEN_FILE, payload)
    return payload.get("access_token")


def main(argv):
    action = argv[0] if argv else "url"
    if action == "url":
        print(build_url())
        return 0
    if action == "show":
        if not TOKEN_FILE.exists():
            print("no user token stored: the bot cannot write in chat yet")
            return 1
        stored = json.loads(TOKEN_FILE.read_text())
        left = int(stored.get("expires_in", 0)) - int(time.time() - stored.get("obtained_at", 0))
        print(f"user token stored, scope={stored.get('scope', '?')}, "
              f"{'refreshable' if stored.get('refresh_token') else 'no refresh token'}, "
              f"{left}s left on the current one")
        return 0
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
