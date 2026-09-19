#!/usr/bin/env python3
"""Everything that talks to Kick's public API, and nothing that decides anything.

Three things live here because all three are about trust, not about the channel:

  - the token. One authorization by hand, then a refresh token this keeps alive.
    Written with mode 600 and never logged, because it can write in the chat and
    change the channel.
  - the signature. Kick signs every webhook with its private key, so a POST that
    does not verify against the published public key is somebody else's. That is
    the whole security of an endpoint whose URL is in a form on a public site.
  - the calls themselves, each one narrow enough to read.

The bot holds no HTTP of its own; this module holds no opinion about chat.
"""
import base64
import hashlib
import json
import os
import pathlib
import secrets
import sys
import time
import urllib.parse

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chan  # noqa: E402

ID_BASE = "https://id.kick.com"
API_BASE = "https://api.kick.com/public/v1"
TOKENS = chan.STATE / "kick_tokens.json"
PENDING = chan.STATE / "kick_pending.json"
PUBKEY = chan.STATE / "kick_pubkey.pem"
APP = chan.load_env(chan.ROOT / "kickapp.env")
SCOPES = ("user:read channel:read channel:write chat:write events:subscribe "
          "channel:rewards:read channel:rewards:write")
# a token is swapped this long before it expires, so a call never races the clock
REFRESH_MARGIN = 300
# Kick's own clock against ours: a webhook older than this is a replay
SIGNATURE_WINDOW = 300
# how long a link stays the same link, so one handed out is one that still works
PENDING_LIFE = 12 * 3600


def _write_private(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    old = os.umask(0o077)
    try:
        tmp.write_text(json.dumps(data, indent=1))
        tmp.chmod(0o600)
        tmp.replace(path)
    finally:
        os.umask(old)


# --- authorization ---------------------------------------------------------

def _challenge(verifier):
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def authorize_url(redirect_uri):
    """The URL to open once, and the verifier kept for the exchange.

    PKCE, because the code comes back over a redirect this server does not
    control: without the verifier, anyone who sees the code in a log or a
    referrer can spend it.
    """
    # A link already handed out has to keep working. Minting a new verifier on
    # every call quietly killed the one in somebody's chat window, and the
    # callback page calls this on every visit, so opening it to read the link
    # was enough to break the link it had just given.
    held = chan.read_json(PENDING, {})
    if (held.get("state") and held.get("verifier")
            and held.get("redirect_uri") == redirect_uri
            and time.time() - held.get("at", 0) < PENDING_LIFE):
        # the challenge is the verifier's own hash, so it never has to be stored
        return f"{ID_BASE}/oauth/authorize?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": APP.get("KICK_CLIENT_ID", ""),
            "redirect_uri": redirect_uri, "scope": SCOPES, "state": held["state"],
            "code_challenge": _challenge(held["verifier"]), "code_challenge_method": "S256"})
    verifier = secrets.token_urlsafe(64)[:86]
    challenge = _challenge(verifier)
    state = secrets.token_urlsafe(24)
    _write_private(PENDING, {"verifier": verifier, "state": state,
                             "redirect_uri": redirect_uri, "at": int(time.time())})
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": APP.get("KICK_CLIENT_ID", ""),
        "redirect_uri": redirect_uri, "scope": SCOPES, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{ID_BASE}/oauth/authorize?{query}"


def exchange(code, state):
    """Turn the code Kick redirected with into a token pair. (ok, message)."""
    pending = chan.read_json(PENDING, {})
    if not pending or pending.get("state") != state:
        return False, "state inconnu ou perime"
    answer = requests.post(f"{ID_BASE}/oauth/token", timeout=30, data={
        "grant_type": "authorization_code", "code": code,
        "client_id": APP.get("KICK_CLIENT_ID", ""),
        "client_secret": APP.get("KICK_CLIENT_SECRET", ""),
        "redirect_uri": pending["redirect_uri"], "code_verifier": pending["verifier"]})
    if answer.status_code != 200:
        return False, f"echange refuse ({answer.status_code})"
    _store(answer.json())
    PENDING.unlink(missing_ok=True)
    return True, "autorise"


def _store(payload):
    _write_private(TOKENS, {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "expires_at": int(time.time()) + int(payload.get("expires_in") or 3600)})


def token():
    """A valid access token, refreshed if it is about to expire, or None."""
    data = chan.read_json(TOKENS, {})
    if not data.get("access_token"):
        return None
    if data.get("expires_at", 0) - time.time() > REFRESH_MARGIN:
        return data["access_token"]
    if not data.get("refresh_token"):
        return None
    answer = requests.post(f"{ID_BASE}/oauth/token", timeout=30, data={
        "grant_type": "refresh_token", "refresh_token": data["refresh_token"],
        "client_id": APP.get("KICK_CLIENT_ID", ""),
        "client_secret": APP.get("KICK_CLIENT_SECRET", "")})
    if answer.status_code != 200:
        chan.log(f"refresh refuse ({answer.status_code}): autorisation a refaire")
        return None
    _store(answer.json())
    return chan.read_json(TOKENS, {}).get("access_token")


# --- signature -------------------------------------------------------------

def public_key():
    """Kick's signing key, fetched once and kept on disk."""
    try:
        if time.time() - PUBKEY.stat().st_mtime < 7 * 86400:
            return PUBKEY.read_bytes()
    except OSError:
        pass
    try:
        got = requests.get(f"{API_BASE}/public-key", timeout=20).json()
        pem = got["data"]["public_key"].encode()
        PUBKEY.parent.mkdir(parents=True, exist_ok=True)
        PUBKEY.write_bytes(pem)
        return pem
    except (requests.RequestException, KeyError, ValueError, OSError):
        try:
            return PUBKEY.read_bytes()
        except OSError:
            return None


def verify(headers, body, now=None):
    """True when this POST really came from Kick and is not a replay.

    The signed message is the id, the timestamp and the raw body joined by dots,
    exactly as received: re-encoding the JSON first would change a byte and the
    signature would never match again.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.exceptions import InvalidSignature

    message_id = headers.get("Kick-Event-Message-Id") or ""
    stamp = headers.get("Kick-Event-Message-Timestamp") or ""
    signature = headers.get("Kick-Event-Signature") or ""
    if not (message_id and stamp and signature):
        return False
    try:
        sent = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - sent) > SIGNATURE_WINDOW:
        return False
    pem = public_key()
    if not pem:
        return False
    try:
        key = serialization.load_pem_public_key(pem)
        key.verify(base64.b64decode(signature),
                   b".".join([message_id.encode(), stamp.encode(), body]),
                   padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# --- calls -----------------------------------------------------------------

def _call(method, path, **kwargs):
    access = token()
    if not access:
        return None
    try:
        answer = requests.request(
            method, f"{API_BASE}{path}", timeout=30,
            headers={"Authorization": f"Bearer {access}",
                     "Accept": "application/json"}, **kwargs)
    except requests.RequestException as problem:
        chan.log(f"appel {method} {path} injoignable: {problem}")
        return None
    if answer.status_code >= 400:
        chan.log(f"appel {method} {path} -> {answer.status_code} {answer.text[:120]}")
        return None
    try:
        return answer.json()
    except ValueError:
        return {}


def say(broadcaster_id, text, reply_to=None):
    """One chat message, as the bot. Kick cuts at 500 characters, so we do."""
    body = {"broadcaster_user_id": int(broadcaster_id), "content": text[:480], "type": "bot"}
    if reply_to:
        body["reply_to_message_id"] = reply_to
    return _call("POST", "/chat", json=body) is not None


def set_title(title):
    return _call("PATCH", "/channels", json={"stream_title": title[:140]}) is not None


def subscriptions():
    got = _call("GET", "/events/subscriptions")
    return (got or {}).get("data") or []


def subscribe(broadcaster_id, events=(("chat.message.sent", 1),)):
    return _call("POST", "/events/subscriptions", json={
        "broadcaster_user_id": int(broadcaster_id), "method": "webhook",
        "events": [{"name": name, "version": version} for name, version in events]})


def rewards():
    return (_call("GET", "/channels/rewards") or {}).get("data") or []


def create_reward(title, cost, description="", user_input=False):
    """A reward whose redemptions wait for us, so points can be given back.

    should_redemptions_skip_request_queue stays false on purpose: a redemption
    that lands already spent cannot be refunded when the channel is unable to
    honour it, and being told no while paying for it is the one outcome worth
    engineering against.
    """
    return _call("POST", "/channels/rewards", json={
        "title": title, "cost": int(cost), "description": description[:255],
        "is_enabled": True, "is_user_input_required": bool(user_input),
        "should_redemptions_skip_request_queue": False})


def delete_reward(reward_id):
    return _call("DELETE", f"/channels/rewards/{reward_id}") is not None


def settle_redemption(redemption_id, honoured):
    """Spend the points, or hand them back. One call, one redemption."""
    path = "/channels/rewards/redemptions/" + ("accept" if honoured else "reject")
    return _call("POST", path, json={"ids": [redemption_id]}) is not None


def viewers(slug):
    """Viewer count from the public endpoint, which needs no token at all."""
    try:
        got = requests.get(f"https://kick.com/api/v2/channels/{slug}", timeout=20,
                           headers={"User-Agent": "Mozilla/5.0"}).json()
        live = got.get("livestream")
        return int(live["viewer_count"]) if live else 0
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return 0
