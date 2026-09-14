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
import subprocess
import time
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
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 :root{--bg:#0f1216;--card:#171b21;--line:#242a33;--txt:#e8eaed;--dim:#8b95a3;
       --ok:#5fbf7f;--warn:#d8a657;--bad:#e06c75;--accent:#4a8fd4}
 *{box-sizing:border-box}
 body{font:14px/1.5 system-ui,-apple-system,sans-serif;margin:0;background:var(--bg);color:var(--txt)}
 main{max-width:960px;margin:0 auto;padding:20px 16px 48px}
 header{display:flex;align-items:center;gap:10px;margin-bottom:18px;flex-wrap:wrap}
 h1{font-size:17px;font-weight:600;margin:0;letter-spacing:.2px}
 .badge{font-size:12px;font-weight:600;padding:3px 9px;border-radius:999px;
        border:1px solid currentColor}
 .grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
 .card h2{font-size:12px;font-weight:600;color:var(--dim);margin:0 0 10px;
          text-transform:uppercase;letter-spacing:.6px;display:flex;align-items:center;gap:7px}
 .big{font-size:26px;font-weight:600;line-height:1.1;font-variant-numeric:tabular-nums}
 .sub{color:var(--dim);font-size:12px;margin-top:2px}
 dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:5px 12px;font-size:13px}
 dt{color:var(--dim)}
 dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
 .ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.dim{color:var(--dim)}
 .bar{height:5px;border-radius:3px;background:#222831;margin-top:9px;overflow:hidden}
 .bar>i{display:block;height:100%;background:var(--accent)}
 .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 input{flex:1;min-width:180px;background:#0c0f13;border:1px solid #2c333c;color:var(--txt);
       padding:9px 10px;border-radius:7px;font:inherit}
 button{background:var(--accent);border:0;color:#fff;padding:9px 15px;border-radius:7px;
        cursor:pointer;font:inherit;font-weight:500}
 button.ghost{background:#272e38}
 button:disabled{opacity:.5;cursor:default}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line)}
 th{color:var(--dim);font-weight:500;font-size:12px}
 td.num{text-align:right;font-variant-numeric:tabular-nums}
 .wrap{overflow-x:auto}
 svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2;
     stroke-linecap:round;stroke-linejoin:round;flex:none}
 #gate{position:fixed;inset:0;background:#0c0f13;display:none;align-items:center;
       justify-content:center;padding:16px;z-index:9}
 #gate .card{max-width:420px;width:100%}
 @media(max-width:520px){.big{font-size:22px}main{padding:16px 12px 40px}}
</style>
<div id=gate><div class=card>
  <div style="margin-bottom:12px;font-weight:500">Ce panneau pilote un direct.</div>
  <div class=row><input id=tokin type=password placeholder="jeton d acces"><button onclick=unlock()>Entrer</button></div>
  <div id=gatemsg class=bad style=margin-top:9px></div>
  <div class=dim style="margin-top:11px;font-size:12px">Garde dans ce navigateur, jamais dans l URL.</div>
</div></div>
<main>
<header><h1 id=titre>vodloop</h1><span id=live class=badge></span><span id=age class=dim style=font-size:12px></span></header>
<div class=grid id=cartes></div>
<div class=card style=margin-top:12px>
 <h2><span data-ico=play></span>a l antenne</h2>
 <div id=now></div>
</div>
<div class=card style=margin-top:12px>
 <h2><span data-ico=plus></span>ajouter, ou passer</h2>
 <div class=row>
  <input id=url placeholder="lien YouTube">
  <button onclick=add()>Ajouter</button>
  <button class=ghost onclick=skip()>Passer</button>
 </div>
 <div id=msg class=dim style=margin-top:9px></div>
</div>
<div class=card style=margin-top:12px>
 <h2><span data-ico=list></span>file</h2>
 <div class=wrap><table id=q></table></div>
</div>
</main>
<script>
__COMMON__
// lucide, inlined: the box serves this page itself and a control panel that
// needs a CDN to draw is a control panel that goes blank when the CDN does.
const ICONES = {
  play:'M6 3l14 9-14 9V3z',
  film:'M4 3h16v18H4zM4 9h16M4 15h16M9 3v18M15 3v18',
  books:'M3 4h4v16H3zM9 4h4v16H9zM15 6l4-1 3 15-4 1z',
  dl:'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3',
  disk:'M3 5h18v6H3zM3 13h18v6H3zM7 8h.01M7 16h.01',
  gear:'M4 6h16M4 12h16M4 18h16M8 4v4M16 10v4M11 16v4',
  list:'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  plus:'M12 5v14M5 12h14'
};
function dessine(hote, nom) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  (ICONES[nom] || '').split('M').filter(Boolean).forEach(d => {
    const p = document.createElementNS(ns, 'path');
    p.setAttribute('d', 'M' + d); svg.append(p);
  });
  hote.append(svg);
}
document.querySelectorAll('[data-ico]').forEach(n => dessine(n, n.dataset.ico));

const carte = (titre, ico) => {
  const c = el('div', undefined, 'card');
  const h = el('h2'); const sp = el('span'); dessine(sp, ico);
  h.append(sp, el('span', titre)); c.append(h); return c;
};
const ligne = (dl, k, v, cls) => { dl.append(el('dt', k), el('dd', v, cls)); };
const seuil = (v, bon, moyen) => v >= bon ? 'ok' : (v >= moyen ? 'warn' : 'bad');

function cartes(s) {
  const box = document.getElementById('cartes'); box.textContent = '';
  const u = s.units || {}, lib = s.library || {}, sup = s.supply || {},
        sh = s.share || {}, st = s.settings || {};

  // 1. l antenne: l avance est le seul chiffre qui dit si ca va tenir
  let c = carte('antenne', 'film');
  c.append(el('div', s.ahead_minutes + ' min', 'big ' + seuil(s.ahead_minutes, 60, 20)));
  c.append(el('div', s.segments + ' morceaux prets, plafond ' + st.ahead_minutes + ' min', 'sub'));
  let dl = el('dl');
  ['push', 'feed', 'prep'].forEach(r => {
    const d = u[r] || {};
    ligne(dl, r, d.active ? 'actif' : 'ARRETE', d.active ? 'ok' : 'bad');
  });
  ligne(dl, 'relances pousseur', (u.push || {}).restarts,
        (u.push || {}).restarts === '0' ? 'ok' : 'warn');
  c.append(dl); box.append(c);

  // 2. la bibliotheque: inedit, pas total. C est la duree avant une redite.
  c = carte('bibliotheque', 'books');
  c.append(el('div', lib.unseen_hours + ' h', 'big ' + seuil(lib.unseen_hours, 10, 4)));
  c.append(el('div', 'inedit, avant toute redite', 'sub'));
  dl = el('dl');
  ligne(dl, 'fichiers', lib.files + (lib.parts ? ' dont ' + lib.parts + ' tranches' : ''));
  ligne(dl, 'jamais vus', lib.unseen_files);
  ligne(dl, 'duree totale', lib.hours + ' h');
  ligne(dl, 'passages par video', st.max_plays);
  c.append(dl); box.append(c);

  // 3. l approvisionnement: l age du rapport compte autant que son contenu
  c = carte('approvisionnement', 'dl');
  const age = sup.age_min;
  c.append(el('div', age === null ? '?' : age + ' min', 'big ' +
    (age === null ? 'bad' : age <= 15 ? 'ok' : age <= 60 ? 'warn' : 'bad')));
  c.append(el('div', 'depuis le dernier rapport de la machine de tirage', 'sub'));
  dl = el('dl');
  ligne(dl, 'en file', sup.queue === null ? '?' : sup.queue);
  ligne(dl, 'inbox', sup.inbox === null ? '-' : sup.inbox);
  ligne(dl, 'livrees', sup.done === null ? '?' : sup.done, 'ok');
  ligne(dl, 'echecs cumules', sup.failed === null ? '?' : sup.failed, 'dim');
  c.append(dl); box.append(c);

  // 4. le disque: sous le plancher, tout le reste s arrete
  c = carte('disque', 'disk');
  c.append(el('div', sh.free_gb + ' Go', 'big ' +
    (sh.free_gb > sh.floor_gb * 2 ? 'ok' : sh.free_gb > sh.floor_gb ? 'warn' : 'bad')));
  c.append(el('div', 'libres, plancher ' + sh.floor_gb + ' Go', 'sub'));
  if (sh.budget_gb) {
    const b = el('div', undefined, 'bar'); const i = el('i');
    i.style.width = Math.min(100, 100 * sh.used_gb / sh.budget_gb) + '%';
    b.append(i); c.append(b);
  }
  dl = el('dl');
  ligne(dl, 'part utilisee', sh.used_gb + ' / ' + (sh.budget_gb || '?') + ' Go');
  ligne(dl, 'disque', sh.disk_gb + ' Go');
  c.append(dl); box.append(c);

  // 5. les reglages, pour lire la rotation sans ouvrir le crontab
  c = carte('reglages', 'gear');
  dl = el('dl');
  ligne(dl, 'duree acceptee', (st.min_minutes || '?') + ' a ' + (st.max_minutes || '?') + ' min');
  ligne(dl, 'hauteur max', st.max_height ? st.max_height + 'p' : '-');
  ligne(dl, 'morceau', st.chunk_seconds + ' s');
  ligne(dl, 'tranches reparties', st.spread_parts ? 'oui' : 'non',
        st.spread_parts ? 'ok' : 'warn');
  c.append(dl); box.append(c);
}

function antenne(s) {
  const box = document.getElementById('now'); box.textContent = '';
  const n = s.now;
  if (!n) { box.append(el('div', 'rien en cours, le clip d attente est a l ecran', 'bad')); return; }
  box.append(el('div', n.title || '-', 'big'));
  const bouts = [];
  if (n.of) bouts.push('tranche ' + n.part + ' sur ' + n.of);
  if (n.minutes) bouts.push(n.minutes + ' min');
  if (s.skip_votes) bouts.push(s.skip_votes + ' vote(s) pour passer');
  box.append(el('div', bouts.join('  ·  '), 'sub'));
}

function table(s) {
  const t = document.getElementById('q'); t.textContent = '';
  const head = t.insertRow();
  ['#', 'titre', 'par', 'votes', 'etat', ''].forEach(h => head.append(el('th', h)));
  s.items.slice().reverse().forEach(i => {
    const r = t.insertRow();
    r.append(el('td', String(i.id), 'num'), el('td', i.title || i.url),
             el('td', i.by_name || ''),
             el('td', String((i.votes || []).length), 'num'),
             el('td', i.status + (i.error ? ': ' + i.error : ''),
                i.status === 'error' ? 'bad' : i.status === 'ready' ? 'ok' : 'dim'));
    const cell = el('td'); const b = el('button', 'retirer', 'ghost');
    b.onclick = () => call('/api/remove', {id: i.id}).then(refresh);
    cell.append(b); r.append(cell);
  });
}

function health(s) {
  document.getElementById('titre').textContent = (s.settings || {}).label || 'vodloop';
  const b = document.getElementById('live');
  b.textContent = s.live ? 'EN DIRECT' : 'HORS LIGNE';
  b.className = 'badge ' + (s.live ? 'ok' : 'bad');
  document.getElementById('age').textContent = 'maj ' + new Date().toLocaleTimeString();
  cartes(s); antenne(s);
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
    // the panel has no single status line any more, so a refusal is reported
    // where the operator is already looking rather than into a removed node
    if (!r.ok) { document.getElementById('msg').textContent =
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


# Library durations cost one ffprobe per file, so they are measured at most
# every couple of minutes and held. The page polls far more often than that, and
# a panel that reprobes the whole library on every refresh is a panel that makes
# the box it is watching slower.
_DUREES = {"at": 0.0, "par": {}}
DUREES_TTL = 120


def library_durations():
    now = time.time()
    if now - _DUREES["at"] < DUREES_TTL:
        return _DUREES["par"]
    par = {}
    lib = common.LIBRARY_DIR
    if lib and lib.is_dir():
        for path in lib.iterdir():
            if not path.is_file() or path.suffix.lower() not in common.MEDIA_SUFFIXES:
                continue
            got = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True).stdout.strip()
            try:
                par[path.name] = float(got)
            except ValueError:
                par[path.name] = 0.0
    _DUREES.update(at=now, par=par)
    return par


def library_state():
    """Files, parts, and the two figures that say how long the channel can run.

    "unseen" is the one that matters: with one play per file, that is the time
    before a viewer meets something twice. Total hours only says when the disk
    would have to give something up.
    """
    import prep
    par = library_durations()
    hist = prep.load_history()
    lib = common.LIBRARY_DIR
    files = parts = 0
    total = unseen_s = 0.0
    unseen = 0
    if lib and lib.is_dir():
        for path in sorted(lib.iterdir()):
            if not path.is_file() or path.suffix.lower() not in common.MEDIA_SUFFIXES:
                continue
            files += 1
            if prep.PART.search(path.name):
                parts += 1
            secs = par.get(path.name, 0.0)
            total += secs
            if prep.played_count(hist, path) < common.MAX_PLAYS:
                unseen += 1
                unseen_s += secs
    return {"files": files, "parts": parts,
            "hours": round(total / 3600, 1),
            "unseen_files": unseen, "unseen_hours": round(unseen_s / 3600, 1)}


def supply_state():
    """What the fetching machine last said. It cannot be reached from here, so
    this is its own report, and its age is as important as its contents."""
    out = {"queue": None, "done": None, "failed": None, "age_min": None,
           "inbox": None}
    board = os.environ.get("VODLOOP_BOARD_STATUS") or "/home/ubuntu/yt2oracle/status.json"
    try:
        with open(board) as fh:
            d = json.load(fh)
        out.update(queue=d.get("queue"), done=d.get("done"), failed=d.get("failed"),
                   age_min=round((time.time() - (d.get("at") or 0)) / 60))
    except (OSError, ValueError):
        pass
    inbox = os.environ.get("VODLOOP_INBOX")
    if inbox:
        try:
            with open(inbox) as fh:
                out["inbox"] = sum(1 for line in fh if line.strip())
        except OSError:
            out["inbox"] = 0
    return out


def unit_state():
    """Each moving part, and how many times it has had to be restarted. A
    pusher that has restarted is a stream that was interrupted, so the count is
    the number worth watching, not whether it is up right now."""
    out = {}
    for role in ("push", "feed", "prep"):
        name = common.unit(role)
        try:
            actif = subprocess.run(["systemctl", "is-active", name],
                                   capture_output=True, text=True).stdout.strip()
            n = subprocess.run(["systemctl", "show", "-p", "NRestarts", "--value", name],
                               capture_output=True, text=True).stdout.strip()
        except OSError:
            actif, n = "?", "?"
        out[role] = {"active": actif == "active", "restarts": n}
    return out


def share_state():
    used = common.bytes_used()
    budget = common.BUDGET_BYTES
    total, _, free = shutil.disk_usage(common.ROOT)
    return {"used_gb": round(used / 1e9, 1),
            "budget_gb": round(budget / 1e9, 1) if budget else None,
            "free_gb": round(free / 1e9, 1),
            "disk_gb": round(total / 1e9, 1),
            "floor_gb": round(common.MIN_FREE_BYTES / 1e9, 1)}


def settings_state():
    import prep
    def env(name, default=None):
        v = os.environ.get(name)
        return int(v) if v and v.isdigit() else default
    return {
        "max_plays": common.MAX_PLAYS,
        "min_minutes": (env("VODLOOP_MIN_SECONDS", 0) or 0) // 60,
        "max_minutes": (env("VODLOOP_MAX_SECONDS", 0) or 0) // 60,
        "max_height": env("VODLOOP_MAX_HEIGHT"),
        "chunk_seconds": common.CHUNK_SECONDS,
        "ahead_minutes": round(common.AHEAD_LIMIT_SECONDS / 60),
        "spread_parts": bool(prep.SPREAD_PARTS),
        "label": common.LABEL or "vodloop",
    }


def now_state(queue):
    """What is on the wire, and where inside its video."""
    import prep
    import pathlib
    cur = now_playing(queue)
    if not cur:
        return None
    out = {"title": cur.get("title"), "id": cur.get("id"), "part": 0, "of": 0,
           "minutes": None}
    path = cur.get("path")
    if path:
        p = pathlib.Path(path)
        found = prep.PART.search(p.name)
        if found:
            out["part"] = int(found.group(2))
            tail = p.name.rsplit(".p", 1)[-1]
            out["of"] = int(tail.split("of")[1].split(".")[0]) if "of" in tail else 0
        secs = library_durations().get(p.name)
        if secs:
            out["minutes"] = round(secs / 60)
    return out


def state():
    queue = common.load_queue()
    segments = common.ready_segments()
    return {
        "live": channel_is_live(),
        "segments": len(segments),
        "ahead_minutes": round(len(segments) * common.CHUNK_SECONDS / 60),
        "free_gb": round(shutil.disk_usage(common.ROOT).free / 1e9, 1),
        "offset": round(common.read_offset()),
        "now_playing": (now_playing(queue) or {}).get("title"),
        "now": now_state(queue),
        "skip_votes": len(chat_state().get("skip_votes") or {}),
        "library": library_state(),
        "supply": supply_state(),
        "units": unit_state(),
        "share": share_state(),
        "settings": settings_state(),
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
