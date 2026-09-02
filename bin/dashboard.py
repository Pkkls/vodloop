#!/usr/bin/env python3
"""Control panel and stream overlay.

Auth is a single shared token in VODLOOP_TOKEN, sent as X-Token. That is
deliberate: this controls a stream, not user accounts, and it matches how the
other service on this box already gates its stats endpoint.

Titles and chat names reach these pages from outside, so nothing is ever put
into the document as markup: every value is written with textContent. An
attacker who queues a video whose title is a <script> tag gets that text drawn
on screen, which is the point.
"""
import hmac
import http.server
import json
import os
import shutil
import urllib.parse
import urllib.request

import common

TOKEN = os.environ.get("VODLOOP_TOKEN", "")
PORT = int(os.environ.get("VODLOOP_PORT", "8770"))

COMMON_JS = """
// The token is kept in this browser, not in the address bar: a query string
// ends up in history, in the proxy's access log and in outgoing Referer
// headers. A token passed in the URL once is stored and then removed from it.
let tok = '';
try {
  const fromUrl = new URLSearchParams(location.search).get('token');
  if (fromUrl) {
    localStorage.setItem('vodloop_token', fromUrl);
    history.replaceState(null, '', location.pathname);
  }
  tok = localStorage.getItem('vodloop_token') || '';
} catch (e) { tok = new URLSearchParams(location.search).get('token') || ''; }

const setToken = t => { tok = t; try { localStorage.setItem('vodloop_token', t); } catch (e) {} };
const forgetToken = () => { tok = ''; try { localStorage.removeItem('vodloop_token'); } catch (e) {} };

// resolves to {ok, data} so a caller can tell a refusal from a value
const call = (p, body) => fetch(p, {method: body ? 'POST' : 'GET',
  headers: {'X-Token': tok, 'Content-Type': 'application/json'},
  body: body ? JSON.stringify(body) : null})
  .then(r => r.json().then(d => ({ok: r.ok, status: r.status, data: d})))
  .catch(() => ({ok: false, status: 0, data: {error: 'serveur injoignable'}}));

// user-supplied strings are only ever set as text, never parsed as markup
const el = (tag, text, cls) => { const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
"""

PAGE = """<!doctype html><meta charset=utf-8><title>vodloop</title>
<style>
 body{font:14px system-ui;margin:0;background:#111418;color:#e8eaed}
 main{max-width:860px;margin:0 auto;padding:24px}
 h1{font-size:18px;font-weight:600;margin:0 0 16px}
 .card{background:#191d23;border:1px solid #262c34;border-radius:8px;padding:14px;margin-bottom:14px}
 .row{display:flex;gap:8px;align-items:center}
 input{flex:1;background:#0e1116;border:1px solid #2c333c;color:#e8eaed;padding:8px;border-radius:6px}
 button{background:#2b6cb0;border:0;color:#fff;padding:8px 14px;border-radius:6px;cursor:pointer}
 button.ghost{background:#2c333c}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{text-align:left;padding:6px 4px;border-bottom:1px solid #232830}
 .k{color:#9aa4b2}.ok{color:#5fbf7f}.bad{color:#e06c75}.v{color:#d8a657}
 #gate{position:fixed;inset:0;background:#0e1116;display:none;align-items:center;justify-content:center}
 #gate .card{max-width:420px;width:90%}
</style>
<div id=gate><div class=card>
  <div style=margin-bottom:10px>Ce panneau pilote un direct. Colle le jeton d'acces.</div>
  <div class=row><input id=tokin type=password placeholder="jeton"><button onclick=unlock()>Entrer</button></div>
  <div id=gatemsg class=bad style=margin-top:8px></div>
  <div class=k style=margin-top:10px>Il est garde dans ce navigateur, jamais dans l'URL.</div>
</div></div>
<main>
<h1>vodloop</h1>
<div class=card id=health></div>
<div class=card>
 <div class=row>
  <input id=url placeholder="lien YouTube a ajouter">
  <button onclick=add()>Ajouter</button>
  <button class=ghost onclick=skip()>Passer</button>
 </div>
 <div id=msg class=k style=margin-top:8px></div>
</div>
<div class=card><table id=q></table></div>
</main>
<script>
__COMMON__
function health(s) {
  const box = document.getElementById('health'); box.textContent = '';
  const put = (label, value, cls) => { box.append(el('span', label + ' ', 'k'),
    el('b', value, cls), document.createTextNode('\\u00a0\\u00a0')); };
  put('direct', s.live ? 'OUI' : 'non', s.live ? 'ok' : 'bad');
  put('en cours', s.now_playing || '-');
  put('avance', s.ahead_minutes + ' min');
  put('chunks', String(s.segments));
  put('disque', s.free_gb + ' Go');
  put('votes skip', String(s.skip_votes));
}
function table(s) {
  const t = document.getElementById('q'); t.textContent = '';
  const head = t.insertRow();
  ['#', 'titre', 'par', 'votes', 'etat', ''].forEach(h => head.append(el('th', h)));
  s.items.slice().reverse().forEach(i => {
    const r = t.insertRow();
    r.append(el('td', String(i.id)), el('td', i.title || i.url),
             el('td', i.by_name || ''), el('td', String((i.votes || []).length), 'v'),
             el('td', i.status + (i.error ? ': ' + i.error : ''),
                i.status === 'error' ? 'bad' : ''));
    const cell = el('td'); const b = el('button', 'x', 'ghost');
    b.onclick = () => call('/api/remove', {id: i.id}).then(refresh);
    cell.append(b); r.append(cell);
  });
}
const gate = document.getElementById('gate');
// Called with no reason, this leaves any existing message alone. The polling
// loop calls it every few seconds, and it used to wipe "jeton refuse" a moment
// after it appeared.
const showGate = why => { gate.style.display = 'flex';
  if (why !== undefined) document.getElementById('gatemsg').textContent = why; };
function unlock() {
  const v = document.getElementById('tokin').value.trim();
  if (!v) return;
  setToken(v);
  showGate('verification...');
  call('/api/state').then(r => {
    if (r.ok) { gate.style.display = 'none'; document.getElementById('gatemsg').textContent = '';
                document.getElementById('tokin').value = ''; health(r.data); table(r.data); }
    else if (r.status === 401) { forgetToken(); showGate('jeton refuse'); }
    else { showGate(r.data.error || 'le service ne repond pas'); }
  });
}
document.getElementById('tokin').addEventListener('keydown',
  e => { if (e.key === 'Enter') unlock(); });

function refresh() {
  if (!tok) { showGate(); return Promise.resolve(); }
  return call('/api/state').then(r => {
    if (r.status === 401) { forgetToken(); showGate('jeton refuse'); return; }
    if (!r.ok) { document.getElementById('health').textContent =
      r.data.error || 'le service ne repond pas'; return; }
    gate.style.display = 'none';
    health(r.data); table(r.data);
  });
}
const note = r => { document.getElementById('msg').textContent =
  r.data.error || r.data.note || ''; };
const add = () => call('/api/add', {url: document.getElementById('url').value})
  .then(r => { note(r); if (r.ok) document.getElementById('url').value = ''; refresh(); });
const skip = () => call('/api/skip', {}).then(r => { note(r); refresh(); });
refresh(); setInterval(refresh, 5000);
</script>""".replace("__COMMON__", COMMON_JS)

OVERLAY = """<!doctype html><meta charset=utf-8><title>vodloop overlay</title>
<style>
 html,body{margin:0;background:transparent;font:16px system-ui;color:#fff}
 #wrap{position:fixed;left:32px;bottom:32px;max-width:640px}
 .now{font-size:22px;font-weight:700;text-shadow:0 2px 6px #000;line-height:1.3}
 .by{font-size:14px;opacity:.8;text-shadow:0 2px 6px #000;margin-top:2px}
 .next{margin-top:12px;font-size:14px;opacity:.85;text-shadow:0 2px 6px #000}
 .next b{color:#d8a657;font-weight:600}
 .skip{margin-top:8px;font-size:13px;color:#e06c75;text-shadow:0 2px 6px #000}
</style>
<div id=wrap>
 <div class=now id=now></div>
 <div class=by id=by></div>
 <div class=next id=next></div>
 <div class=skip id=skip></div>
</div>
<script>
__COMMON__
function draw(s) {
  document.getElementById('now').textContent = s.now_title || 'nothing playing';
  document.getElementById('by').textContent = s.now_by ? 'requested by ' + s.now_by : '';
  const next = document.getElementById('next'); next.textContent = '';
  if (s.next.length) {
    next.append(el('span', 'up next  '));
    s.next.forEach(n => { next.append(el('b', n.title || n.url), el('span',
      '  (' + n.votes + ')  ')); });
  }
  document.getElementById('skip').textContent =
    s.skip_votes ? s.skip_votes + ' vote(s) to skip' : '';
}
// the overlay stays silent when it cannot read: a compositor source showing an
// error string on stream is worse than one showing nothing
const tick = () => call('/api/overlay').then(r => { if (r.ok) draw(r.data); });
tick(); setInterval(tick, 4000);
</script>""".replace("__COMMON__", COMMON_JS)


def channel_is_live():
    slug = common.env().get("KICK_SLUG", "")
    if not slug:
        return False
    request = urllib.request.Request(
        f"https://kick.com/api/v2/channels/{slug}",
        # a neutral agent: Kick rejects an empty one and a spoofed browser alike
        headers={"User-Agent": "vodloop/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return bool(json.load(response).get("livestream"))
    except Exception:
        return False


def chat_state():
    try:
        return json.loads((common.STATE / "chat.json").read_text())
    except (OSError, ValueError):
        return {}


def now_playing(queue):
    """The item whose chunks are at the head of the queue on disk."""
    segments = common.ready_segments()
    if not segments:
        return None
    try:
        wanted = int(segments[0].name.split("_")[0])
    except ValueError:
        return None
    return next((i for i in queue["items"] if i["id"] == wanted), None)


def state():
    queue = common.load_queue()
    segments = common.ready_segments()
    current = now_playing(queue)
    return {
        "live": channel_is_live(),
        "segments": len(segments),
        "ahead_minutes": round(len(segments) * common.CHUNK_SECONDS / 60),
        "free_gb": round(shutil.disk_usage(common.ROOT).free / 1e9, 1),
        "offset": round(common.read_offset()),
        "now_playing": current["title"] if current and current.get("title") else None,
        "skip_votes": len(chat_state().get("skip_votes") or {}),
        "items": queue["items"][-40:],
    }


def overlay_state():
    import chatlogic
    queue = common.load_queue()
    current = now_playing(queue)
    upcoming = chatlogic.playback_order(queue)[:3]
    return {
        "now_title": (current or {}).get("title"),
        "now_by": (current or {}).get("by_name"),
        "next": [{"title": i.get("title"), "url": i["url"],
                  "votes": len(i.get("votes") or [])} for i in upcoming],
        "skip_votes": len(chat_state().get("skip_votes") or {}),
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # the journal already records what matters

    def authorised(self):
        return bool(TOKEN) and hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def reply(self, code, payload, kind="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/":
            # the registered redirect URI is the site root, so the OAuth return
            # lands here rather than on a route of its own
            query = urllib.parse.parse_qs(parsed.query)
            if "code" in query:
                import oauth
                ok, message = oauth.exchange(query["code"][0],
                                             (query.get("state") or [""])[0])
                page = ("<!doctype html><meta charset=utf-8>"
                        f"<body style='font:15px system-ui;background:#111418;color:#e8eaed;padding:40px'>"
                        f"{'Autorisation enregistree. Le bot peut ecrire dans le chat.' if ok else 'Echec : ' + message}"
                        "</body>")
                return self.reply(200 if ok else 400, page.encode(),
                                  "text/html; charset=utf-8")
            return self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/overlay":
            return self.reply(200, OVERLAY.encode(), "text/html; charset=utf-8")
        if not self.authorised():
            return self.reply(401, {"error": "bad token"})
        if path == "/api/state":
            return self.reply(200, state())
        if path == "/api/overlay":
            return self.reply(200, overlay_state())
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if not self.authorised():
            return self.reply(401, {"error": "bad token"})
        length = int(self.headers.get("Content-Length", "0"))
        if length > 4096:
            return self.reply(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.reply(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self.reply(400, {"error": "bad json"})

        queue = common.load_queue()
        if self.path == "/api/add":
            # the panel goes through the same validation as chat, not around it
            video_id, result = common.canonical_youtube_url(body.get("url", ""))
            if video_id is None:
                return self.reply(400, {"error": result})
            queue["seq"] += 1
            queue["items"].append({"id": queue["seq"], "url": result, "video_id": video_id,
                                   "status": "pending", "by": "panel",
                                   "by_name": "panel", "votes": [], "added_at": 0})
            common.save_queue(queue)
            return self.reply(200, {"note": "ajoute", "id": queue["seq"]})

        if self.path == "/api/remove":
            wanted = body.get("id")
            if not isinstance(wanted, int):
                return self.reply(400, {"error": "id required"})
            queue["items"] = [i for i in queue["items"] if i["id"] != wanted]
            common.save_queue(queue)
            for chunk in common.SEGMENTS.glob(f"{wanted:05d}_*.ts"):
                chunk.unlink(missing_ok=True)
            return self.reply(200, {"note": "retire"})

        if self.path == "/api/skip":
            segments = common.ready_segments()
            if not segments:
                return self.reply(200, {"note": "rien a passer"})
            prefix = segments[0].name.split("_")[0]
            for chunk in segments:
                if chunk.name.startswith(prefix):
                    chunk.unlink(missing_ok=True)
            return self.reply(200, {"note": "passe"})

        self.reply(404, {"error": "not found"})


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("VODLOOP_TOKEN is required")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
