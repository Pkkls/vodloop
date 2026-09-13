#!/usr/bin/env python3
"""One-time authorisation so the bot can write in chat.

An app token, the kind kickbus uses for webhooks, carries no scopes: introspection
returns only {active, client_id, token_type: app}. Posting a chat message needs a
user token, which only exists once the channel owner has approved the app in a
browser. That approval happens once; the refresh token then keeps it alive.

    python3 bin/oauth.py url      print the link to open
    python3 bin/oauth.py show     report what is stored, without printing secrets
    python3 bin/oauth.py refresh  renew the token now, for the timer to call
    python3 bin/oauth.py exchange <code> <state>   finish it by hand

The last one is for a channel that is not the one serving the redirect URI.
There is one registered URI, the site root, and the first channel's dashboard
answers it. Another channel's callback lands there, fails the state check, and
is NOT consumed, so its code can be read out of the nginx access log and
finished here, against that channel's own pending file. The code is good for
fifteen minutes.
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import common

AUTHORIZE = "https://id.kick.com/oauth/authorize"
TOKEN = "https://id.kick.com/oauth/token"
# channel:write is what lets the stream title be set. Asking for it in the
# same grant means the owner approves once instead of twice, and the stored
# token keeps chat:write, which the bot already relies on.
SCOPES = "chat:write channel:write"
USER_AGENT = "vodloop/0.1"
TOKEN_FILE = common.STATE / "kick_user_token.json"
PENDING_FILE = common.STATE / "kick_oauth_pending.json"


def setting(name):
    """A setting from the environment, then .env, then bus.env.

    The client id and secret live in bus.env, which systemd hands to the chat
    service as an EnvironmentFile. That made the services work while every
    command in the docstring above, run by hand, built a URL with an empty
    client_id and reported no error at all. Reading both files is what makes
    the manual path behave like the service path.
    """
    return (os.environ.get(name)
            or common.env().get(name)
            or common.env("bus.env").get(name, ""))


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
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as failure:
        # the server's own words are the whole diagnosis; swallowing them for a
        # tidy message is how the first attempt at this told us nothing
        detail = failure.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {failure.code}: {detail}") from None


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

    # An empty client id is sent as an empty field rather than refused, and Kick
    # answers 400 with no body, which names nothing. Say it here instead.
    if not setting("KICK_CLIENT_ID") or not setting("KICK_CLIENT_SECRET"):
        return False, ("app credentials missing from this process: "
                       "bus.env is not in its environment")

    base = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "client_id": setting("KICK_CLIENT_ID"),
        "code_verifier": pending["verifier"],
    }
    # Some servers reject a client secret sent alongside a PKCE verifier and
    # some require it, so try both rather than guess which kind this is.
    attempts = [dict(base, client_secret=setting("KICK_CLIENT_SECRET")), base]
    problems = []
    payload = None
    for fields in attempts:
        try:
            payload = _post(fields)
            break
        except Exception as problem:
            problems.append(str(problem))
    if payload is None:
        return False, " | ".join(problems)[:500]

    payload["obtained_at"] = time.time()
    _write_private(TOKEN_FILE, payload)
    PENDING_FILE.unlink(missing_ok=True)
    return True, "authorised"


def access_token(force=False):
    """A valid user access token, refreshing it when it is close to expiry.

    force skips that judgement and renews anyway, which is what the timer wants:
    the thirty day clock on the refresh token only resets when it is spent, so
    waiting for the two hour access token to lapse is beside the point.
    """
    try:
        stored = json.loads(TOKEN_FILE.read_text())
    except (OSError, ValueError):
        return None
    age = time.time() - stored.get("obtained_at", 0)
    if not force and age < max(60, int(stored.get("expires_in", 3600)) - 120):
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
    if action == "exchange":
        if len(argv) != 3:
            print("usage: oauth.py exchange <code> <state>")
            return 2
        ok, message = exchange(argv[1], argv[2])
        print("autorisation enregistree" if ok else f"echec: {message}")
        return 0 if ok else 1
    if action == "refresh":
        # Nothing else ever renews this on its own. access_token() is called
        # only when the bot has a reply to send, so a channel nobody talks in
        # lets the refresh token reach its thirty days unused and die, and the
        # bot loses the right to write without a single error anywhere.
        if not TOKEN_FILE.exists():
            print("no user token stored: nothing to refresh")
            return 1
        was = json.loads(TOKEN_FILE.read_text()).get("obtained_at", 0)
        if access_token(force=True) is None:
            print("refresh failed: the authorisation has to be granted again")
            return 1
        stored = json.loads(TOKEN_FILE.read_text())
        # saying "renewed" without checking would hide the one failure that
        # matters, a call that quietly changed nothing
        if stored.get("obtained_at", 0) <= was:
            print("refresh did nothing: the stored token was not replaced")
            return 1
        print(f"user token renewed, scope={stored.get('scope', '?')}, "
              f"{int(stored.get('expires_in', 0))}s on the new one")
        return 0
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
