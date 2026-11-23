#!/usr/bin/env python3
"""n1-lab - Carl's N=1 research lab, self-hosted on his OpenHost zone (cloud in a bottle).

An archive of the experiments he runs on himself, plus phone-first review tasks (tick the
labels a model got wrong, blinded reads). Everything except /healthz sits behind the zone's
owner login (see openhost.toml). The owner pages have write endpoints and serve private notes, so none of
them may ever be made public. The one exception is the reviewer area under /r/ (see "reviewer area" below):
read-only, behind its own shared password, off unless reviewer_auth.json exists in the data dir, and showing
only the reviewer copies of entries that were explicitly marked for reviewers.

Content lives OUTSIDE this repo, in the app's data dir, and is re-read on every request:
    $OPENHOST_APP_DATA_DIR/experiments.json     the archive (one entry per experiment)
    $OPENHOST_APP_DATA_DIR/tasks/<slug>.json    review tasks
    $OPENHOST_APP_DATA_DIR/files/*              attachments (rendered notebooks, write-ups)
    $OPENHOST_APP_DATA_DIR/responses/<slug>.jsonl   Carl's answers, appended by POST, pulled home by the Mac
    $OPENHOST_APP_DATA_DIR/reads/views.jsonl        reading log: time on screen and scroll coverage per page view
    $OPENHOST_APP_DATA_DIR/voice/samples/<uuid>.{wav,json}   explicitly labelled voice samples (voice_store.py);
                                                collection only, nothing here trains or verifies
    $OPENHOST_APP_DATA_DIR/reviewer/experiments.json   what reviewers may see (built by tools/build_reviewer.py)
    $OPENHOST_APP_DATA_DIR/reviewer/files/*            scrubbed, leak-checked reviewer copies of attachments
    $OPENHOST_APP_DATA_DIR/reviewer_auth.json          salted hash of the reviewer password (reviewer_auth.py)
    $OPENHOST_APP_DATA_DIR/owner_guard                 optional; "enforce" makes owner pages demand the router's owner mark
Copy content in with `oh app ssh` (tools/push.sh). The repo holds code and sample data only.
"""
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import reviewer_auth
import voice_store

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("OPENHOST_APP_DATA_DIR", os.path.join(HERE, "sample_data"))
PORT = int(os.environ.get("PORT", "8080"))
FONT_DIR = os.path.join(HERE, "fonts")
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
MAX_BODY = 64 * 1024
LOCK = threading.Lock()
VOICE_HTML = os.path.join(HERE, "voice.html")                 # owned by the frontend; served as-is
VOICE_CLIP_RE = re.compile(r"^/api/voice/clip/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.wav$")


def voice():
    return voice_store.VoiceStore(os.path.join(DATA, "voice"))

# ---------------------------------------------------------------- content


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, f"missing: {os.path.basename(path)}"
    except json.JSONDecodeError as exc:                     # loud, never silent
        return None, f"{os.path.basename(path)} is not valid JSON: {exc}"


def experiments():
    doc, err = read_json(os.path.join(DATA, "experiments.json"))
    if doc is None:
        return [], err
    return sorted(doc.get("experiments", []), key=lambda e: e.get("date", ""), reverse=True), None


def task(slug):
    if not SLUG_RE.match(slug):
        return None, "bad task name"
    return read_json(os.path.join(DATA, "tasks", f"{slug}.json"))


def answers(slug):
    """Latest answer per item id. The log is append-only, so a changed mind is a new line."""
    out = {}
    try:
        with open(os.path.join(DATA, "responses", f"{slug}.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    out[r["id"]] = r
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        pass
    return out


# ---------------------------------------------------------------- reading log
# Every view of a file or an experiment page reports, from the browser itself, how long it was on screen and
# which part of the document was in the viewport for how many seconds (100 bands of 1% each). The phone's real
# viewport is measured, not assumed. Checkpoints are cumulative per view, so the highest seq wins.
#     $OPENHOST_APP_DATA_DIR/reads/views.jsonl    one line per checkpoint, append-only, pulled home by the Mac
READ_PATH_RE = re.compile(r"^/(files/[A-Za-z0-9_.-]{1,120}|x/[A-Za-z0-9_-]{1,64})$")
VIEW_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
LOCAL_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
BANDS = 100
MAX_READ_BODY = 16 * 1024
LOG_START = "2026-10-17"                                       # nothing was logged before this; "unopened" means since then
_READS = {"ino": None, "offset": 0, "views": {}}


def reads_file():
    return os.path.join(DATA, "reads", "views.jsonl")


def views():
    """Latest checkpoint per view id. Parses only the lines appended since the last call, so a page render
    costs O(new checkpoints), not O(the whole log); memory is O(views)."""
    path = reads_file()
    with LOCK:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            _READS.update(ino=None, offset=0, views={})
            return {}
        if st.st_ino != _READS["ino"] or st.st_size < _READS["offset"]:    # replaced or truncated: start over
            _READS.update(ino=st.st_ino, offset=0, views={})
        if st.st_size > _READS["offset"]:
            with open(path, "rb") as fh:
                fh.seek(_READS["offset"])
                chunk = fh.read(st.st_size - _READS["offset"])
            end = chunk.rfind(b"\n") + 1                       # a half-written last line waits for the next call
            for line in chunk[:end].splitlines():
                try:
                    r = json.loads(line)
                    old = _READS["views"].get(r["v"])
                    if old is None or r["seq"] >= old["seq"]:
                        _READS["views"][r["v"]] = r
                except (ValueError, KeyError, TypeError):           # only this app writes the log; skip, never crash
                    continue
            _READS["offset"] += end
        return dict(_READS["views"])


def reading_by_path():
    out = {}
    for r in views().values():
        a = out.setdefault(r["path"], {"views": 0, "active_s": 0, "dwell": [0] * BANDS, "last": ""})
        a["views"] += 1
        a["active_s"] += r.get("active_s", 0)
        for k, sec in enumerate(r.get("dwell", [])[:BANDS]):
            a["dwell"][k] += sec
        a["last"] = max(a["last"], r.get("local", ""))
    return out


def minutes(sec):
    return "under 1 min" if sec < 60 else f"{round(sec / 60)} min"


def strip(dwell):
    """The document as a 100-band bar, top of the page on the left: brighter = on screen longer, dark = never on screen."""
    top = max(dwell) or 1
    rects = "".join(f'<rect x="{k}" width="1" height="6" fill="#52a8ff" fill-opacity="{0.25 + 0.75 * sec / top:.2f}"/>'
                    for k, sec in enumerate(dwell) if sec)
    return (f'<svg class="strip" viewBox="0 0 {BANDS} 6" preserveAspectRatio="none" role="img" '
            f'aria-label="which part of the page was on screen"><rect width="{BANDS}" height="6" fill="#1a1a1a"/>{rects}</svg>')


def reading_line(a):
    if not a:
        return f'<p class="read">no visit logged since {LOG_START}</p>'
    seen = sum(1 for sec in a["dwell"] if sec)
    times = f'{a["views"]} visits, ' if a["views"] > 1 else ""
    return (f'<p class="read">{times}{minutes(a["active_s"])} active, {seen}% of the page on screen, '
            f'last {esc(a["last"] or "?")}</p>{strip(a["dwell"])}')


READ_JS = r"""
(()=>{if(window.__n1read)return;window.__n1read=1;
// n1 reading log: seconds this page was on screen while you were active, and which 1% bands of it were in view.
const B=100,dw=new Array(B).fill(0),id=(self.crypto&&crypto.randomUUID)?crypto.randomUUID():Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10);
let vis=0,act=0,maxd=0,last=Date.now(),dirty=false,seq=0,fails=0;
const p2=n=>String(n).padStart(2,'0'),d0=new Date(),local=d0.getFullYear()+'-'+p2(d0.getMonth()+1)+'-'+p2(d0.getDate())+' '+p2(d0.getHours())+':'+p2(d0.getMinutes());
const H=()=>Math.max(document.documentElement.scrollHeight,document.body?document.body.scrollHeight:0,innerHeight,1);
for(const e of ['scroll','touchstart','touchmove','keydown','mousemove','wheel','click'])addEventListener(e,()=>{last=Date.now()},{passive:true});
setInterval(()=>{if(document.hidden)return;vis++;if(Date.now()-last>60000)return;act++;
  const h=H(),y=Math.max(0,scrollY),a=Math.floor(B*y/h),b=Math.min(B-1,Math.ceil(B*(y+innerHeight)/h)-1);
  for(let k=Math.max(0,a);k<=b;k++)dw[k]++;maxd=Math.max(maxd,Math.min(1,(y+innerHeight)/h));dirty=true},1000);
function warn(m){let el=document.getElementById('n1readerr');if(!el){el=document.createElement('div');el.id='n1readerr';
  el.style.cssText='position:fixed;left:8px;bottom:8px;z-index:99;background:#3c1618;color:#ff6166;font:12px/16px ui-monospace,monospace;padding:4px 8px;border-radius:4px';
  document.body.appendChild(el)}el.textContent='reading log NOT SAVING: '+m}
function send(beacon){if(!dirty)return;dirty=false;
  const body=JSON.stringify({v:id,seq:++seq,path:location.pathname,title:document.title.slice(0,200),local,visible_s:vis,active_s:act,
    max_depth:+maxd.toFixed(3),dwell:dw,vw:innerWidth,vh:innerHeight,doc_h:H(),dpr:devicePixelRatio||1});
  if(beacon&&navigator.sendBeacon&&navigator.sendBeacon('/api/read',new Blob([body],{type:'application/json'})))return;
  fetch('/api/read',{method:'POST',headers:{'Content-Type':'application/json'},body,keepalive:true})
   .then(r=>{if(!r.ok||r.redirected)throw new Error(r.redirected?'logged out':'HTTP '+r.status);fails=0;const el=document.getElementById('n1readerr');if(el)el.remove()})
   .catch(e=>{dirty=true;if(++fails>=2)warn(e.message)})}
setInterval(()=>send(false),30000);
document.addEventListener('visibilitychange',()=>{if(document.hidden)send(true)});
addEventListener('pagehide',()=>send(true));
})();
"""
READ_TAG = ("<script>" + READ_JS + "</script>").encode("utf-8")


def with_reader(body):
    """Attach the reading log to a page: before the last </body>, or at the end when there is none."""
    data = body if isinstance(body, bytes) else body.encode("utf-8")
    i = data.rfind(b"</body>")
    if i < 0:                                                   # rare upper-case tag: only then pay for a lowered copy
        i = data.lower().rfind(b"</body>")
    return data[:i] + READ_TAG + data[i:] if i >= 0 else data + READ_TAG


# ---------------------------------------------------------------- rendering

def clean(text):
    """Design rule: no em or en dashes anywhere user-visible. Normalised at render, never in storage."""
    return str(text).replace("—", "-").replace("–", "-")


def esc(text):
    return html.escape(clean(text), quote=True)


def md(text):
    """A small markdown subset: #/##/### headings, - lists, tables, `code`, **bold**, [text](url), paragraphs."""
    def inline(s):
        s = esc(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\[([^\]]+)\]\(((?:https?://|/)[^)\s]+)\)", r'<a href="\2">\1</a>', s)
        return s
    out, para, lst, tbl = [], [], [], []

    def flush():
        nonlocal para, lst, tbl
        if para:
            out.append("<p>" + " ".join(para) + "</p>")
        if lst:
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in lst) + "</ul>")
        if tbl:
            rows = [r for r in tbl if not re.match(r"^\s*\|?[\s:|-]+\|?\s*$", r)]
            cells = [[inline(c.strip()) for c in r.strip().strip("|").split("|")] for r in rows]
            if cells:
                head = "".join(f"<th>{c}</th>" for c in cells[0])
                body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in cells[1:])
                out.append(f'<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')
        para, lst, tbl = [], [], []
    for line in clean(text).split("\n"):
        s = line.rstrip()
        if not s.strip():
            flush()
        elif s.startswith("|"):
            if para or lst:
                flush()
            tbl.append(s)
        elif re.match(r"^#{1,3} ", s):
            flush()
            n = len(s) - len(s.lstrip("#"))
            out.append(f"<h{n + 1}>{inline(s[n + 1:])}</h{n + 1}>")
        elif re.match(r"^\s*[-*] ", s):
            if para or tbl:
                flush()
            lst.append(inline(re.sub(r"^\s*[-*] ", "", s)))
        else:
            if lst or tbl:
                flush()
            para.append(inline(s))
    flush()
    return "\n".join(out)


CSS = """
@font-face{font-family:Geist;src:url(/fonts/geist.woff2) format('woff2-variations');font-weight:100 900;font-display:swap}
@font-face{font-family:'Geist Mono';src:url(/fonts/geist-mono.woff2) format('woff2-variations');font-weight:100 900;font-display:swap}
:root{--page:#0a0a0a;--card:#111111;--hair:#262626;--ink:#ededed;--body:#a1a1a1;--mute:#7a7a7a;--ok:#52a8ff;--okbg:#0d2440;--bad:#ff6166;--badbg:#3c1618;--warn:#f5a623;--warnbg:#33260d}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--ink);font:16px/24px Geist,Inter,system-ui,-apple-system,sans-serif}
::selection{background:#ededed;color:#171717}
main{max-width:760px;margin:0 auto;padding:24px 16px 96px}
a{color:var(--ok);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:32px;line-height:40px;font-weight:600;letter-spacing:-1.28px;margin:8px 0 8px}
h2{font-size:24px;line-height:32px;font-weight:600;letter-spacing:-.96px;margin:32px 0 8px}
h3{font-size:20px;line-height:28px;font-weight:600;letter-spacing:-.6px;margin:24px 0 8px}
h4{font-size:16px;font-weight:600;margin:20px 0 6px}
p,li{color:var(--body)}strong{color:var(--ink);font-weight:600}ul{padding-left:20px}
code,.mono{font:13px/20px 'Geist Mono',ui-monospace,SFMono-Regular,Menlo,monospace}
code{background:#1a1a1a;border-radius:4px;padding:1px 5px;color:var(--ink)}
.eyebrow{font:12px/16px 'Geist Mono',ui-monospace,monospace;text-transform:uppercase;color:var(--mute)}
.card{display:block;background:var(--card);border-radius:8px;padding:20px;margin:16px 0;box-shadow:0 0 0 1px var(--hair)}
a.card:hover{text-decoration:none;box-shadow:0 0 0 1px #3a3a3a}
.card h3{margin:6px 0 6px}.card p{margin:6px 0}
.pill{display:inline-block;font:12px/16px 'Geist Mono',ui-monospace,monospace;text-transform:uppercase;padding:3px 9px;border-radius:9999px;background:var(--okbg);color:var(--ok)}
.pill.warn{background:var(--warnbg);color:var(--warn)}.pill.bad{background:var(--badbg);color:var(--bad)}.pill.mute{background:#1a1a1a;color:var(--mute)}
.read{margin:10px 0 0;font:12px/16px 'Geist Mono',ui-monospace,monospace;color:var(--mute)}.strip{display:block;width:100%;height:6px;border-radius:2px;margin:6px 0 0}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tw{overflow-x:auto;margin:12px 0}table{border-collapse:collapse;width:100%;font-size:14px;line-height:20px}
th{font:12px/16px 'Geist Mono',ui-monospace,monospace;text-transform:uppercase;color:var(--mute);text-align:left;padding:8px 10px;background:#0f0f0f}
td{padding:8px 10px;border-top:1px solid var(--hair);color:var(--body);vertical-align:top}
.btn{display:inline-block;border:0;border-radius:6px;padding:12px 18px;font:500 15px/20px Geist,system-ui,sans-serif;background:var(--ink);color:#0a0a0a;cursor:pointer}
.btn.ghost{background:transparent;color:var(--ink);box-shadow:0 0 0 1px var(--hair)}
.err{background:var(--badbg);color:var(--bad);border-radius:8px;padding:12px 16px;margin:16px 0}
nav{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px}nav .eyebrow{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
"""

TASK_CSS = """
#stage{min-height:52vh}.ctx{font-size:13px;line-height:19px;color:var(--mute);white-space:pre-wrap;max-height:26vh;overflow:auto;border-left:2px solid var(--hair);padding:2px 0 2px 12px;margin:10px 0}
.msg p{margin:0 0 10px}.msg ul,.msg ol{margin:4px 0 10px;padding-left:22px}.msg code{font:13px 'Geist Mono',ui-monospace,monospace;background:#0d0d0d;padding:1px 4px;border-radius:4px}
.msg{font-size:16px;line-height:1.6;background:#161616;border-radius:8px;padding:14px 16px;word-break:break-word;color:var(--ink);box-shadow:0 0 0 1px var(--hair);margin:10px 0;max-height:34vh;overflow:auto}
.lab{margin:14px 0}.lab div{color:var(--body);font-size:15px;line-height:22px;margin:3px 0}.lab b{color:var(--ink);font-weight:600}
.bar{position:fixed;left:0;right:0;bottom:0;background:#0a0a0af2;border-top:1px solid var(--hair);padding:10px 12px calc(10px + env(safe-area-inset-bottom));display:flex;gap:8px;justify-content:center}
.bar button{flex:1;max-width:240px;border:0;border-radius:8px;padding:16px 8px;font:600 16px/20px Geist,system-ui,sans-serif;cursor:pointer}
.ok{background:var(--okbg);color:var(--ok)}.bad{background:var(--badbg);color:var(--bad)}.mid{background:#1a1a1a;color:var(--ink)}#back{flex:0 0 64px;background:#1a1a1a;color:var(--body)}
.on{outline:2px solid currentColor}
#note{width:100%;background:#111;color:var(--ink);border:0;box-shadow:0 0 0 1px var(--hair);border-radius:6px;padding:12px;font:15px/22px Geist,system-ui,sans-serif;margin-top:8px}
.prog{height:3px;background:#1a1a1a;border-radius:2px;overflow:hidden;margin:8px 0 14px}.prog i{display:block;height:100%;background:var(--ok);width:0}
#save{font:12px/16px 'Geist Mono',ui-monospace,monospace;color:var(--mute)}
"""


def page(title, body, extra_css=""):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<meta name="robots" content="noindex,nofollow"><title>{esc(title)}</title>'
            f"<style>{CSS}{extra_css}</style></head><body><main>{body}</main></body></html>")


STATUS_PILL = {"done": "", "running": "warn", "negative": "bad", "parked": "mute", "draft": "mute"}


def index_page():
    exps, err = experiments()
    parts = ['<div class="eyebrow">n = 1</div><h1>Research on myself.</h1>'
             '<p>Every experiment I have run on my own data, what it asked, and what came back. '
             'Negative results stay in.</p>']
    if err:
        parts.append(f'<div class="err">{esc(err)}</div>')
    parts.append('<a class="card" href="/voice"><div class="row"><span class="pill">voice lab</span>'
                 '<span class="eyebrow">collection only</span></div><h3>Record labelled voice samples</h3>'
                 '<p>Label clips as me, someone else, a replay, background or a mention. Saved privately on this '
                 'zone; nothing is trained or verified here.</p></a>')
    todo = []
    for e in exps:
        for slug in e.get("tasks", []):
            t, _ = task(slug)
            if t:
                done = len([a for a in answers(slug).values() if a.get("verdict")])
                if done < len(t.get("items", [])):
                    todo.append((slug, t, done))
    if todo:
        parts.append("<h2>Waiting on you</h2>")
        for slug, t, done in todo:
            parts.append(f'<a class="card" href="/t/{esc(slug)}"><div class="row"><span class="pill warn">task</span>'
                         f'<span class="eyebrow">{done} of {len(t["items"])} done</span></div>'
                         f'<h3>{esc(t.get("title", slug))}</h3><p>{esc(t.get("blurb", ""))}</p></a>')
    reads = reading_by_path()
    files = [a["file"] for e in exps for a in e.get("attachments", [])]
    unread = [f for f in files if "/files/" + f not in reads]
    week = sum(r.get("active_s", 0) for r in views().values() if r.get("local", "") >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400)))
    parts.append(f'<a class="card" href="/reads"><div class="row"><span class="pill mute">reading</span>'
                 f'<span class="eyebrow">{len(files) - len(unread)} of {len(files)} files opened</span></div>'
                 f'<h3>What I have read, and what I have not</h3><p>{minutes(week)} of active reading in the last 7 days. '
                 f'{len(unread)} files with no visit logged since {LOG_START}.</p></a>')
    parts.append("<h2>Experiments</h2>")
    for e in exps:
        st = e.get("status", "done")
        parts.append(f'<a class="card" href="/x/{esc(e["slug"])}"><div class="row">'
                     f'<span class="pill {STATUS_PILL.get(st, "")}">{esc(st)}</span>'
                     f'<span class="eyebrow">{esc(e.get("date", ""))}</span></div>'
                     f'<h3>{esc(e.get("title", e["slug"]))}</h3><p>{esc(e.get("question", ""))}</p>'
                     f'<p><strong>{esc(e.get("result", ""))}</strong></p></a>')
    return page("n=1 lab", "".join(parts))


def experiment_page(slug):
    exps, err = experiments()
    e = next((x for x in exps if x.get("slug") == slug), None)
    if not e:
        return None
    st = e.get("status", "done")
    parts = [f'<nav><a href="/">All experiments</a></nav><div class="row"><span class="pill {STATUS_PILL.get(st, "")}">{esc(st)}</span>'
             f'<span class="eyebrow">{esc(e.get("date", ""))}</span></div><h1>{esc(e.get("title", slug))}</h1>'
             f'<p>{esc(e.get("question", ""))}</p>']
    for t_slug in e.get("tasks", []):
        t, _ = task(t_slug)
        if t:
            done = len([a for a in answers(t_slug).values() if a.get("verdict")])
            parts.append(f'<a class="card" href="/t/{esc(t_slug)}"><div class="row"><span class="pill warn">task</span>'
                         f'<span class="eyebrow">{done} of {len(t.get("items", []))} done</span></div>'
                         f'<h3>{esc(t.get("title", t_slug))}</h3><p>{esc(t.get("blurb", ""))}</p></a>')
    parts.append(md(e.get("body_md", "")))
    if e.get("attachments"):
        reads = reading_by_path()
        parts.append("<h2>Files</h2>")
        for a in e["attachments"]:
            parts.append(f'<a class="card" href="/files/{esc(a["file"])}"><h3>{esc(a.get("label", a["file"]))}</h3>'
                         f'<p>{esc(a.get("note", ""))}</p>{reading_line(reads.get("/files/" + a["file"]))}</a>')
    return page(e.get("title", slug), "".join(parts))


def reads_page():
    exps, err = experiments()
    reads = reading_by_path()
    parts = ['<nav><a href="/">Lab</a></nav><div class="eyebrow">reading log</div><h1>What I have read.</h1>'
             '<p>Measured by the browser on whatever screen you read on: seconds the page was visible while you were '
             'active (touch, scroll or key in the last 60 s), and which 1% bands of the page were in the viewport. '
             'The bar is the page top to bottom; dark means never on screen. On screen is not the same as read.</p>']
    if err:
        parts.append(f'<div class="err">{esc(err)}</div>')
    parts.append(f"<h2>Not opened since {LOG_START}</h2><p>The log started that day, so older visits are not counted.</p>")
    n_unread = 0
    for e in exps:
        todo = [a for a in e.get("attachments", []) if "/files/" + a["file"] not in reads]
        if not todo:
            continue
        n_unread += len(todo)
        parts.append(f'<h3><a href="/x/{esc(e["slug"])}">{esc(e.get("title", e["slug"]))}</a> '
                     f'<span class="eyebrow">{esc(e.get("date", ""))}</span></h3><ul>'
                     + "".join(f'<li><a href="/files/{esc(a["file"])}">{esc(a.get("label", a["file"]))}</a></li>' for a in todo)
                     + "</ul>")
    if not n_unread:
        parts.append("<p>Every file has been opened at least once.</p>")
    rows = sorted(views().values(), key=lambda r: (r.get("local", ""), r.get("at", "")), reverse=True)[:60]
    parts.append(f"<h2>Sessions</h2><p>{len(rows)} most recent.</p>" if rows else "<h2>Sessions</h2><p>None yet.</p>")
    for r in rows:
        seen = sum(1 for sec in r.get("dwell", []) if sec)
        parts.append(f'<a class="card" href="{esc(r["path"])}"><div class="row"><span class="eyebrow">{esc(r.get("local", "?"))}</span>'
                     f'<span class="eyebrow">{r.get("vw", 0)}x{r.get("vh", 0)}</span></div>'
                     f'<h3>{esc(r.get("title") or r["path"])}</h3><p class="read">{minutes(r.get("active_s", 0))} active of '
                     f'{minutes(r.get("visible_s", 0))} open, {seen}% on screen, deepest {round(100 * r.get("max_depth", 0))}%</p>'
                     f'{strip(r.get("dwell", [0] * BANDS))}</a>')
    return page("Reading log", "".join(parts))


TASK_JS = r"""
// Verdicts come from the task (default Wrong/Right). Answers go into a localStorage queue first and
// are retried until the server says 200, so a dropped hotspot never loses a swipe. If storage is
// unavailable (private mode) it falls back to waiting for the server before moving on.
const S=window.TASK,A=window.ANS;let i=0,justAnswered=false;const n=S.items.length;
const V=S.verdicts||[{key:'wrong',label:S.wrong_label||'Wrong',swipe:'left'},{key:'right',label:S.right_label||'Right',swipe:'right'}];
const $=x=>document.getElementById(x),QK='n1q:'+S.slug;
let PERSIST=true;try{localStorage.setItem(QK+':t','1');localStorage.removeItem(QK+':t')}catch(e){PERSIST=false}
function loadQ(){if(!PERSIST)return [];try{return JSON.parse(localStorage.getItem(QK)||'[]')}catch(e){return []}}
function saveQ(q){if(PERSIST)try{localStorage.setItem(QK,JSON.stringify(q))}catch(e){}}
for(const x of loadQ())A[x.id]=x;
function firstOpen(){for(let k=0;k<n;k++){if(!A[S.items[k].id])return k}return n}
function esc(s){return String(s).replace(/[–—]/g,'-').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
// Markdown rendered and sanitised (agent text can carry raw HTML); escaped plain text without the CDN.
function md(t){return (window.marked&&window.DOMPurify)?DOMPurify.sanitize(marked.parse(String(t).replace(/[–—]/g,'-'),{breaks:true})):esc(t)}
function cls(v){return v.swipe==='left'?'bad':v.swipe==='right'?'ok':'mid'}
function status(err){const q=loadQ().length,el=$('save');if(!el)return;
  if(err){el.textContent='NOT SAVED: '+err;el.style.color='#ff6166';return}
  el.textContent=q?q+' answer'+(q>1?'s':'')+' waiting to save, retrying...':'';el.style.color=q?'#ff6166':''}
async function send(x){const r=await fetch('/api/task/'+S.slug+'/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(x)});
  if(!r.ok)throw new Error('HTTP '+r.status+(r.status===403||r.status===400?' (server refused; tell Claude)':''))}
let flushing=false;
async function flush(){if(flushing)return;flushing=true;
  // Re-read the queue each pass and drop only the answer just sent: a swipe made mid-upload must never
  // be overwritten by a stale copy of the queue (lost-answer race found in testing 2026-10-01).
  const same=(a,b)=>a.id===b.id&&a.verdict===b.verdict&&a.note===b.note;
  try{let q;while((q=loadQ()).length){const x=q[0];await send(x);saveQ(loadQ().filter(y=>!same(y,x)))}status()}catch(e){status(e.message+'. Kept on this phone; retrying.')}
  finally{flushing=false}}
function bar(){$('bar').innerHTML='<button id="back" aria-label="previous">Back</button>'+V.map((v,k)=>'<button class="v '+cls(v)+'" data-k="'+k+'">'+esc(v.label)+'</button>').join('');
  $('back').onclick=()=>{if(i>0){i--;justAnswered=false;draw()}};
  document.querySelectorAll('#bar .v').forEach(b=>b.onclick=()=>put(V[+b.dataset.k].key))}
function draw(){
  const done=Object.keys(A).length;$('p').style.width=(100*done/n)+'%';
  if(i>=n){const c={};Object.values(A).forEach(a=>c[a.verdict]=(c[a.verdict]||0)+1);
    $('stage').innerHTML='<h2>All '+n+' done.</h2><p>'+V.map(v=>esc(v.label)+': '+(c[v.key]||0)).join(' &middot; ')+'</p><p>The Mac pulls your answers on its own; nothing to paste.</p><div id="save"></div><p><a class="btn" href="/">Back to the lab</a></p>';
    $('bar').style.display='none';status();return}
  if(S.break_every&&justAnswered&&done%S.break_every===0){justAnswered=false;
    $('stage').innerHTML='<h2>'+done+' of '+n+' done.</h2><p>Good place to stop. It resumes here next time.</p><div id="save"></div><p><button class="btn" id="go">Keep going</button></p>';
    $('bar').style.display='none';status();$('go').onclick=()=>{draw()};return}
  $('bar').style.display='flex';const it=S.items[i],a=A[it.id]||{};
  $('stage').innerHTML='<div class="eyebrow">'+(i+1)+' of '+n+(it.source?' &middot; '+esc(it.source):'')+'</div>'+
    (it.context?'<details><summary class="eyebrow" style="cursor:pointer;margin-top:8px">'+esc(S.context_label||'what the assistant had just said')+'</summary><div class="ctx">'+esc(it.context)+'</div></details>':'')+
    '<div class="msg" id="card">'+md(it.text)+'</div><div class="lab">'+(it.label_lines||[]).map(l=>'<div>'+l+'</div>').join('')+'</div>'+
    '<textarea id="note" rows="2" placeholder="note (optional)">'+esc(a.note||'')+'</textarea><div id="save"></div>';
  document.querySelectorAll('#bar .v').forEach(b=>b.classList.toggle('on',V[+b.dataset.k].key===a.verdict));
  const cd=$('card');if(S.card_end&&cd){cd.style.maxHeight='48vh';cd.scrollTop=cd.scrollHeight}
  swipe(cd);status();window.scrollTo(0,0)}
async function put(verdict){const it=S.items[i],note=($('note')||{value:''}).value.trim(),x={id:it.id,verdict,note};
  if(!PERSIST){status();$('save').textContent='saving';
    try{await send(x)}catch(e){status(e.message+'. Check your connection and tap again.');return}}
  else{saveQ(loadQ().filter(y=>y.id!==it.id).concat([x]))}
  A[it.id]=x;i++;justAnswered=true;draw();if(PERSIST)flush()}
function swipe(el){const L=V.find(v=>v.swipe==='left'),R=V.find(v=>v.swipe==='right');if(!el||(!L&&!R))return;
  let x0=null,y0=null,dx=0,dy=0;el.style.touchAction='pan-y';
  el.addEventListener('pointerdown',e=>{x0=e.clientX;y0=e.clientY;dx=dy=0;el.style.transition='none'});
  el.addEventListener('pointermove',e=>{if(x0===null)return;dx=e.clientX-x0;dy=e.clientY-y0;
    if(Math.abs(dx)>Math.abs(dy)){el.style.transform='translateX('+dx+'px) rotate('+(dx/40)+'deg)';
      el.style.boxShadow='0 0 0 2px '+(dx>0?'var(--ok)':'var(--bad)')}});
  const end=()=>{if(x0===null)return;el.style.transition='transform .15s';el.style.transform='';el.style.boxShadow='';
    const go=Math.abs(dx)>90&&Math.abs(dx)>1.5*Math.abs(dy);x0=null;
    if(go&&dx>0&&R)put(R.key);else if(go&&dx<0&&L)put(L.key)};
  el.addEventListener('pointerup',end);el.addEventListener('pointercancel',end)}
document.addEventListener('keydown',e=>{if(e.target.tagName==='TEXTAREA')return;
  const k={ArrowLeft:'left',ArrowRight:'right',ArrowUp:'up'}[e.key],v=k&&V.find(v=>v.swipe===k);if(v&&i<n)put(v.key)});
window.addEventListener('online',flush);document.addEventListener('visibilitychange',()=>{if(!document.hidden)flush()});setInterval(flush,15000);
bar();i=firstOpen();draw();flush();
"""


def task_page(slug):
    t, err = task(slug)
    if t is None:
        return None
    t = dict(t, slug=slug)
    body = (f'<nav><a href="/">Lab</a><span class="eyebrow">{esc(t.get("title", slug))}</span></nav>'
            f'<p style="margin:0">{esc(t.get("intro", ""))}</p><div class="prog"><i id="p"></i></div><div id="stage"></div>'
            f'<div class="bar" id="bar"></div>'
            f"<script>window.TASK={json.dumps(t).replace('</', '<\\/')};window.ANS={json.dumps(answers(slug)).replace('</', '<\\/')};{TASK_JS}</script>")
    libs = ('<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>'
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js"></script>')
    return page(t.get("title", slug), libs + body, TASK_CSS)


# ---------------------------------------------------------------- reviewer area (/r/)
# One path prefix, one shared password, read-only. It exists so that "/r/" can be the only prefix ever added to
# public_paths: every route below is matched under /r/ and nothing here reaches an owner route.
#   - OFF unless reviewer_auth.json exists in the data dir: then every /r/ path answers the same 404 as any
#     unknown path.
#   - A reviewer sees only entries in reviewer/experiments.json that carry "reviewer": true, and only attachments
#     that carry "reviewer": true and whose bytes in reviewer/files/ match the sha256 the build recorded after its
#     leak check. The owner's experiments.json, files/, tasks/ and responses/ are never served from here.
#   - Review tasks are never shown and there is no task, answer, export, reading-log or listing route under /r/.
#   - The only state a reviewer request can change is the wrong-password counter, which lives in memory.
REVIEW_NEVER = frozenset({"mit-application"})           # never markable for reviewers, whatever the content says
REVIEW_NEVER_FILE_PREFIXES = ("mit_",)                  # that entry's attachments, by name
REVIEW_FILE_TYPES = {".html": "text/html; charset=utf-8", ".md": None, ".txt": "text/plain; charset=utf-8",
                     ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                     ".gif": "image/gif"}
REVIEW_FONTS = {"geist.woff2": "font/woff2", "geist-mono.woff2": "font/woff2", "OFL.txt": "text/plain; charset=utf-8"}
REVIEW_NEXT_RE = re.compile(r"^/r/(?:x/[A-Za-z0-9_-]{1,64}|files/[A-Za-z0-9_.-]{1,120})$")
REVIEW_ATTEMPTS = reviewer_auth.Attempts()
REVIEW_HASH_GATE = threading.BoundedSemaphore(1)        # one slow hash at a time: the container has a tenth of a core
MAX_LOGIN_BODY = 4096
# No script may run on a reviewer page and nothing may load from, or be sent to, another site.
REVIEW_HEADERS = (("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
                   "font-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
                  ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"))
REVIEW_CSS = """
.q{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.res{color:var(--ink);font-weight:500}
.field{width:100%;background:var(--card);color:var(--ink);border:0;box-shadow:0 0 0 1px var(--hair);border-radius:6px;padding:14px;font:16px/24px Geist,system-ui,sans-serif;margin:8px 0 16px}
.field:focus{outline:2px solid var(--ok);outline-offset:1px}
label{display:block;color:var(--body);font-size:14px;line-height:20px}
.linkbtn{background:none;border:0;padding:0;color:var(--mute);font:12px/16px 'Geist Mono',ui-monospace,monospace;text-transform:uppercase;cursor:pointer}
.note{color:var(--mute);font-size:14px;line-height:20px}
"""


def review_file_ok(name):
    return (isinstance(name, str) and bool(FILE_RE.match(name)) and ".." not in name and not name.startswith(".")
            and os.path.splitext(name)[1].lower() in REVIEW_FILE_TYPES
            and not name.lower().startswith(REVIEW_NEVER_FILE_PREFIXES))


def review_never_files():
    """Attachment names of the never-for-reviewers entries, read from the owner's content: a third fence, so a
    reviewer file cannot carry one of those names even without the prefix."""
    doc, _ = read_json(os.path.join(DATA, "experiments.json"))
    out = set()
    for e in (doc or {}).get("experiments", []) if isinstance(doc, dict) else []:
        if isinstance(e, dict) and e.get("slug") in REVIEW_NEVER:
            out.update(a.get("file") for a in e.get("attachments", []) if isinstance(a, dict))
    return out


def reviewer_site():
    """(entries, header) a signed-in reviewer may see. Anything not explicitly marked is absent: default hidden."""
    doc, err = read_json(os.path.join(DATA, "reviewer", "experiments.json"))
    if not isinstance(doc, dict):
        return [], {}
    never = review_never_files()
    out = []
    for e in doc.get("experiments", []) if isinstance(doc.get("experiments"), list) else []:
        if not isinstance(e, dict) or e.get("reviewer") is not True:
            continue
        slug = e.get("slug")
        if not isinstance(slug, str) or not SLUG_RE.match(slug) or slug in REVIEW_NEVER:
            continue
        files = [{"file": a["file"], "sha256": a["sha256"], "label": str(a.get("label") or a["file"]), "note": str(a.get("note") or "")}
                 for a in (e.get("attachments") if isinstance(e.get("attachments"), list) else [])
                 if isinstance(a, dict) and a.get("reviewer") is True and review_file_ok(a.get("file"))
                 and a.get("file") not in never and isinstance(a.get("sha256"), str)]
        row = {k: str(e.get(k) or "") for k in ("title", "question", "result", "result_line", "date", "status", "body_md")}
        row.update(slug=slug, n1=e.get("n1") is True, attachments=files)
        out.append(row)
    head = doc.get("site") if isinstance(doc.get("site"), dict) else {}
    return sorted(out, key=lambda e: e["date"], reverse=True), {k: str(v) for k, v in head.items() if isinstance(v, str)}


def reviewer_file(name):
    """(bytes, entry, attachment) for a file a reviewer may read, else None. The bytes must be exactly the ones
    the build checked: a file swapped in afterwards (an owner original, a symlink) does not match and is refused."""
    if not review_file_ok(name):
        return None
    for e in reviewer_site()[0]:
        for a in e["attachments"]:
            if a["file"] != name:
                continue
            fp = os.path.join(DATA, "reviewer", "files", name)
            try:
                if os.path.islink(fp) or not os.path.isfile(fp):
                    return None
                with open(fp, "rb") as fh:
                    data = fh.read()
            except OSError:
                return None
            if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), a["sha256"]):
                print(f"reviewer file REFUSED: {name} does not match the checked copy", file=sys.stderr, flush=True)
                return None
            return data, e, a
    return None


def reviewer_page(title, body):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<meta name="robots" content="noindex,nofollow"><title>{esc(title)}</title>'
            f'<style>{CSS.replace("url(/fonts/", "url(/r/fonts/")}{REVIEW_CSS}</style></head><body><main>{body}</main></body></html>')


REVIEW_SIGN_OUT = '<form method="post" action="/r/logout" style="margin:0"><button class="linkbtn">Sign out</button></form>'


def reviewer_login_page(message="", nxt=""):
    msg = f'<div class="err" role="alert">{esc(message)}</div>' if message else ""
    hidden = f'<input type="hidden" name="next" value="{esc(nxt)}">' if nxt and REVIEW_NEXT_RE.match(nxt) else ""
    return reviewer_page("Reviewer sign-in",
                         '<div class="eyebrow">n = 1 lab</div><h1>Reviewer sign-in</h1>'
                         '<p>Carl Kho\'s studies, read-only. Enter the password you were given.</p>'
                         f'{msg}<form method="post" action="/r/login">{hidden}'
                         '<label for="pw">Password</label>'
                         '<input class="field" id="pw" name="password" type="password" autocomplete="current-password" '
                         'autocapitalize="off" autocorrect="off" spellcheck="false" required autofocus>'
                         '<button class="btn" type="submit">Open</button></form>')


def reviewer_meta(e):
    st = e["status"] or "done"
    n1 = '<span class="eyebrow">single subject (N=1)</span>' if e["n1"] else ""
    return (f'<div class="row"><span class="pill {STATUS_PILL.get(st, "")}">{esc(st)}</span>'
            f'<span class="eyebrow">{esc(e["date"])}</span>{n1}</div>')


def reviewer_index_page():
    exps, head = reviewer_site()
    parts = [f'<nav><span class="eyebrow">{esc(head.get("eyebrow", "n = 1 lab"))}</span>{REVIEW_SIGN_OUT}</nav>'
             f'<h1>{esc(head.get("title", "Studies"))}</h1>']
    if head.get("intro"):
        parts.append(f'<p>{esc(head["intro"])}</p>')
    parts.append(f'<p class="note">{len(exps)} {"study" if len(exps) == 1 else "studies"}, newest first. Read-only.</p>')
    for e in exps:
        line = e["result_line"] or e["result"]
        parts.append(f'<a class="card" href="/r/x/{esc(e["slug"])}">{reviewer_meta(e)}<h3>{esc(e["title"] or e["slug"])}</h3>'
                     f'<p class="q">{esc(e["question"])}</p><p class="res">{esc(line)}</p></a>')
    if not exps:
        parts.append("<p>Nothing has been shared here yet.</p>")
    return reviewer_page(head.get("title", "Studies"), "".join(parts))


def reviewer_study_page(slug):
    e = next((x for x in reviewer_site()[0] if x["slug"] == slug), None)
    if not e:
        return None
    parts = [f'<nav><a href="/r/">All studies</a>{REVIEW_SIGN_OUT}</nav>{reviewer_meta(e)}'
             f'<h1>{esc(e["title"] or slug)}</h1><p>{esc(e["question"])}</p>']
    if e["result"]:
        parts.append(f'<div class="card"><div class="eyebrow">Result</div><p class="res">{esc(e["result"])}</p></div>')
    parts.append(md(e["body_md"]))
    if e["attachments"]:
        parts.append("<h2>Files</h2>")
        for a in e["attachments"]:
            parts.append(f'<a class="card" href="/r/files/{esc(a["file"])}"><h3>{esc(a["label"])}</h3><p>{esc(a["note"])}</p></a>')
    return reviewer_page(e["title"] or slug, "".join(parts))


# ---------------------------------------------------------------- owner guard (optional second fence)
# The zone's router marks a request it has authenticated as the owner's with "X-OpenHost-Is-Owner: true" and
# strips that header from anything a client sends. When the data dir holds a file named owner_guard containing
# "enforce", every route except /healthz and /r/ answers 403 without that mark, so an owner page stays closed
# even if a public prefix were ever matched more loosely than this app expects. Off when the file is absent.
_GUARD = {"stamp": None, "on": False}


def owner_guard_on():
    path = os.path.join(DATA, "owner_guard")
    try:
        st = os.stat(path)
    except OSError:
        _GUARD.update(stamp=None, on=False)
        return False
    stamp = (path, st.st_ino, st.st_mtime_ns, st.st_size)
    if _GUARD["stamp"] != stamp:
        try:
            with open(path, encoding="utf-8") as fh:
                on = fh.read(64).strip() == "enforce"
        except OSError as exc:                                     # unreadable guard file: stay closed, and say so
            print(f"owner guard: cannot read {path} ({exc}); treating it as enforce", file=sys.stderr, flush=True)
            on = True
        _GUARD.update(stamp=stamp, on=on)
    return _GUARD["on"]


# ---------------------------------------------------------------- server

class H(BaseHTTPRequestHandler):
    server_version = "n1lab/0.1"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def log_request(self, code="-", size="-"):
        line = self.requestline
        if getattr(self, "path", "").split("?", 1)[0].rstrip("/") == "/r" or getattr(self, "path", "").startswith("/r/"):
            # never a query string in the log under /r/: a password typed into a URL must not be recorded
            line = f"{self.command} {self.path.split('?', 1)[0]} {self.request_version}"
        self.log_message('"%s" %s %s', line, str(code), str(size))

    def send(self, code, body, ctype="text/html; charset=utf-8", cache="no-store", extra=()):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def not_found(self):
        self.send(404, page("Not found", '<nav><a href="/">Lab</a></nav><h1>Not here.</h1>'))

    def owner_only(self):
        """True (and a 403 already sent) when the owner guard is on and the router did not mark this request."""
        if owner_guard_on() and self.headers.get("X-OpenHost-Is-Owner") != "true":
            sys.stderr.write(f"owner guard: refused {self.command} {self.path.split('?', 1)[0]}\n")
            self.send(403, page("Owner only", "<h1>Owner sign-in required.</h1><p>This page is served to the zone owner only.</p>"))
            return True
        return False

    # ---- reviewer area: every /r/ request is answered here and never falls through to an owner route
    def review_send(self, code, body, ctype="text/html; charset=utf-8", cache="no-store", extra=()):
        self.send(code, body, ctype, cache, REVIEW_HEADERS + tuple(extra))

    def review_redirect(self, where, extra=()):
        self.review_send(303, "", extra=(("Location", where),) + tuple(extra))

    def reviewer_get(self, path):
        rec = reviewer_auth.load(DATA)
        if rec is None:                                            # off: the same 404 as any unknown path
            return self.not_found()
        m = re.match(r"^/r/fonts/([A-Za-z0-9_.-]{1,120})$", path)
        if m and m.group(1) in REVIEW_FONTS:                       # the sign-in page needs its typeface; these are public OFL files
            try:
                with open(os.path.join(FONT_DIR, m.group(1)), "rb") as fh:
                    return self.review_send(200, fh.read(), REVIEW_FONTS[m.group(1)], "public, max-age=604800")
            except OSError:
                return self.review_send(404, reviewer_page("Not found", "<h1>Not here.</h1>"))
        signed_in = reviewer_auth.session_from_cookie_header(rec, self.headers.get("Cookie"))
        if path == "/r/login":
            return self.review_redirect("/r/") if signed_in else self.review_send(200, reviewer_login_page(nxt=self.review_next()))
        if not signed_in:                                          # every other path looks the same without a session
            nxt = "?next=" + urllib.parse.quote(path, safe="/") if REVIEW_NEXT_RE.match(path) else ""
            return self.review_redirect("/r/login" + nxt)
        if path in ("/r", "/r/"):
            return self.review_redirect("/r/") if path == "/r" else self.review_send(200, reviewer_index_page())
        m = re.match(r"^/r/x/([A-Za-z0-9_-]{1,64})$", path)
        out = reviewer_study_page(m.group(1)) if m else None
        if out:
            return self.review_send(200, out)
        m = re.match(r"^/r/files/([A-Za-z0-9_.-]{1,120})$", path)
        got = reviewer_file(m.group(1)) if m else None
        if got:
            data, e, a = got
            ctype = REVIEW_FILE_TYPES[os.path.splitext(a["file"])[1].lower()]
            if ctype is None:                                      # markdown: rendered inside the reviewer frame
                back = f'<nav><a href="/r/x/{esc(e["slug"])}">{esc(e["title"] or e["slug"])}</a>{REVIEW_SIGN_OUT}</nav>'
                return self.review_send(200, reviewer_page(a["label"], back + md(data.decode("utf-8", "replace"))))
            return self.review_send(200, data, ctype)
        return self.review_send(404, reviewer_page("Not found", '<nav><a href="/r/">All studies</a></nav><h1>Not here.</h1>'))

    def review_next(self):
        nxt = urllib.parse.parse_qs(self.path.partition("?")[2], max_num_fields=4).get("next", [""])[0] if "?" in self.path else ""
        return nxt if REVIEW_NEXT_RE.match(nxt) else ""

    def reviewer_post(self, path):
        self.close_connection = True                               # some answers below go out before the body is read
        rec = reviewer_auth.load(DATA)
        if rec is None:
            return self.send(404, '{"error":"no such route"}', "application/json")
        if path not in ("/r/login", "/r/logout"):                  # no other write exists under /r/
            return self.review_send(404, '{"error":"no such route"}', "application/json")
        if self.cross_site():
            return self.review_send(403, reviewer_login_page("That request came from another site and was refused."))
        if path == "/r/logout":
            return self.review_redirect("/r/login", (("Set-Cookie", reviewer_auth.clear_cookie_header()),))
        client = reviewer_auth.client_key(self.client_address[0], self.headers.get("X-Forwarded-For"))
        wait = REVIEW_ATTEMPTS.blocked(client)
        if wait:
            return self.review_send(429, reviewer_login_page(f"Too many wrong tries. Try again in {max(1, round(wait / 60))} min."),
                                    extra=(("Retry-After", str(wait)),))
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= MAX_LOGIN_BODY:
                raise ValueError("body size")
            form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=4)
        except ValueError:
            return self.review_send(400, reviewer_login_page("That did not look like the sign-in form. Try again."))
        password, nxt = form.get("password", [""])[0], form.get("next", [""])[0]
        nxt = nxt if REVIEW_NEXT_RE.match(nxt) else ""
        if not password:
            return self.review_send(400, reviewer_login_page("Enter the password.", nxt))
        if not REVIEW_HASH_GATE.acquire(timeout=3):
            return self.review_send(503, reviewer_login_page("Busy. Try again in a moment.", nxt), extra=(("Retry-After", "3"),))
        try:
            ok = reviewer_auth.check_password(rec, password)
        finally:
            REVIEW_HASH_GATE.release()
        if not ok:
            REVIEW_ATTEMPTS.fail(client)
            sys.stderr.write(f"reviewer sign-in: wrong password from {client}\n")
            return self.review_send(401, reviewer_login_page("That password did not match.", nxt))
        REVIEW_ATTEMPTS.success(client)
        sys.stderr.write(f"reviewer sign-in: ok from {client}\n")
        return self.review_redirect(nxt or "/r/", (("Set-Cookie", reviewer_auth.set_cookie_header(reviewer_auth.sign_session(rec))),))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            exps, err = experiments()
            return self.send(200 if not err else 500, json.dumps({"ok": not err, "experiments": len(exps), "error": err}),
                             "application/json")
        if path == "/r" or path.startswith("/r/"):
            return self.reviewer_get(path)
        if self.owner_only():
            return
        if path == "/":
            return self.send(200, index_page())
        if path == "/voice":
            try:
                with open(VOICE_HTML, "rb") as fh:
                    return self.send(200, fh.read())
            except OSError as exc:                                 # loud: a missing asset is a build bug, not a 404
                return self.send(500, page("Voice lab", '<nav><a href="/">Lab</a></nav><h1>Voice lab is not built.</h1>'
                                           f'<div class="err">{esc(f"voice.html missing from the image: {exc}")}</div>'))
        if path == "/api/voice/status":
            try:
                return self.send(200, json.dumps(voice().status(), ensure_ascii=False), "application/json")
            except OSError as exc:
                return self.send(500, json.dumps({"ok": False, "error": f"could not read voice store: {exc}"}),
                                 "application/json")
        m = VOICE_CLIP_RE.match(path)
        if m:
            try:
                data, err = voice().clip(m.group(1))
            except OSError as exc:
                return self.send(500, json.dumps({"ok": False, "error": f"could not read clip: {exc}"}), "application/json")
            if data is None:
                return self.send(404 if err == "no such sample" else 500,
                                 json.dumps({"ok": False, "error": err}), "application/json")
            return self.send(200, data, "audio/wav", "private, no-store")
        if path == "/reads":
            return self.send(200, reads_page())
        if path == "/export-reads":                                  # the Mac pulls the reading log from here (owner login)
            rows = sorted(views().values(), key=lambda r: r.get("at", ""))
            return self.send(200, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "application/x-ndjson")
        m = re.match(r"^/x/([A-Za-z0-9_-]{1,64})$", path)
        if m:
            out = experiment_page(m.group(1))
            return self.send(200, with_reader(out)) if out else self.not_found()
        m = re.match(r"^/t/([A-Za-z0-9_-]{1,64})$", path)
        if m:
            out = task_page(m.group(1))
            return self.send(200, out) if out else self.not_found()
        m = re.match(r"^/export/([A-Za-z0-9_-]{1,64})$", path)       # the Mac pulls answers from here (owner login)
        if m:
            rows = list(answers(m.group(1)).values())
            return self.send(200, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
                             "application/x-ndjson")
        m = re.match(r"^/(fonts|files)/([A-Za-z0-9_.-]{1,120})$", path)
        if m and ".." not in m.group(2):
            root = FONT_DIR if m.group(1) == "fonts" else os.path.join(DATA, "files")
            fp = os.path.join(root, m.group(2))
            if os.path.isfile(fp):
                ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
                if fp.endswith(".md"):
                    with open(fp, encoding="utf-8") as fh:
                        text = fh.read()
                    return self.send(200, with_reader(page(m.group(2), '<nav><a href="/">Lab</a></nav>' + md(text))))
                with open(fp, "rb") as fh:
                    data = fh.read()
                if m.group(1) == "files" and ctype == "text/html":
                    data = with_reader(data)
                return self.send(200, data, ctype, "public, max-age=604800" if m.group(1) == "fonts" else "no-store")
        return self.not_found()

    do_HEAD = do_GET

    def cross_site(self):
        """Same-origin only: a cross-site page must not be able to write with Carl's login cookie.
        Sec-Fetch-Site is set by the browser and a page cannot forge it, so it decides when present.
        The Origin/Host comparison is only the fallback: the OpenHost router rewrites Host, which made
        every real same-origin POST look cross-site (403 on the phone, 2026-09-19)."""
        origin = self.headers.get("Origin")
        sfs = self.headers.get("Sec-Fetch-Site")
        hosts = {h.strip() for h in (self.headers.get("Host", ""),
                                     *self.headers.get("X-Forwarded-Host", "").split(",")) if h.strip()}
        if sfs is not None:
            refused = sfs not in ("same-origin", "none")
        else:
            refused = bool(origin) and origin.split("://", 1)[-1] not in hosts
        if refused:
            sys.stderr.write(f"refused write: sfs={sfs!r} origin={origin!r} hosts={sorted(hosts)!r}\n")
        return refused

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/r" or path.startswith("/r/"):
            return self.reviewer_post(path)
        if self.owner_only():
            return
        if path == "/api/voice/sample":
            return self.post_voice_sample()
        if path == "/api/read":
            return self.post_read()
        m = re.match(r"^/api/task/([A-Za-z0-9_-]{1,64})/answer$", path)
        if not m:
            return self.send(404, '{"error":"no such route"}', "application/json")
        if self.cross_site():
            return self.send(403, '{"error":"cross-site write refused"}', "application/json")
        slug = m.group(1)
        t, err = task(slug)
        if t is None:
            return self.send(404, json.dumps({"error": err}), "application/json")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= MAX_BODY:
                raise ValueError("body size")
            req = json.loads(self.rfile.read(n))
            item, verdict = str(req["id"]), req["verdict"]
            keys = {v["key"] for v in t.get("verdicts", [])} or {"right", "wrong"}
            if verdict not in keys or item not in {str(i["id"]) for i in t.get("items", [])}:
                raise ValueError("unknown id or verdict")
            row = {"id": item, "verdict": verdict, "note": str(req.get("note", ""))[:1000],
                   "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ua": self.headers.get("User-Agent", "")[:80]}
        except (ValueError, KeyError, TypeError) as exc:
            return self.send(400, json.dumps({"error": f"bad request: {exc}"}), "application/json")
        try:
            os.makedirs(os.path.join(DATA, "responses"), exist_ok=True)
            with LOCK, open(os.path.join(DATA, "responses", f"{slug}.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:                                  # the phone shows NOT SAVED; never a silent 200
            return self.send(500, json.dumps({"error": f"could not write: {exc}"}), "application/json")
        return self.send(200, '{"ok":true}', "application/json")

    def post_read(self):
        """POST /api/read: one cumulative checkpoint of a page view. Validated hard; a failed write is a 500, never a silent 200."""
        if self.cross_site():
            return self.send(403, '{"error":"cross-site write refused"}', "application/json")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= MAX_READ_BODY:
                raise ValueError("body size")
            req = json.loads(self.rfile.read(n))
            dwell = req["dwell"]
            if not isinstance(dwell, list) or len(dwell) != BANDS:
                raise ValueError(f"dwell must be {BANDS} numbers")
            local = str(req.get("local", ""))
            row = {"v": str(req["v"]), "seq": int(req["seq"]), "path": str(req["path"]),
                   "title": clean(str(req.get("title", "")))[:200], "local": local if LOCAL_RE.match(local) else "",
                   "visible_s": int(req["visible_s"]), "active_s": int(req["active_s"]),
                   "max_depth": round(float(req["max_depth"]), 3), "dwell": [int(x) for x in dwell],
                   "vw": int(req["vw"]), "vh": int(req["vh"]), "doc_h": int(req["doc_h"]),
                   "dpr": round(float(req.get("dpr", 1)), 2),
                   "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ua": self.headers.get("User-Agent", "")[:80]}
            if not VIEW_ID_RE.match(row["v"]) or not READ_PATH_RE.match(row["path"]):
                raise ValueError("bad view id or path")
            if not (0 < row["seq"] < 10 ** 6 and 0 <= row["active_s"] <= row["visible_s"] <= 86400
                    and all(0 <= x <= 86400 for x in row["dwell"]) and 0 <= row["max_depth"] <= 1
                    and all(0 <= row[k] <= 100000 for k in ("vw", "vh", "doc_h")) and 0 < row["dpr"] <= 10):
                raise ValueError("value out of range")
        except (ValueError, KeyError, TypeError) as exc:
            return self.send(400, json.dumps({"error": f"bad request: {exc}"}), "application/json")
        try:
            os.makedirs(os.path.dirname(reads_file()), exist_ok=True)
            with LOCK, open(reads_file(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            return self.send(500, json.dumps({"error": f"could not write: {exc}"}), "application/json")
        return self.send(200, '{"ok":true}', "application/json")

    def post_voice_sample(self):
        """POST /api/voice/sample. Every failure path answers ok:false with a NOT SAVED error; never a silent 200."""
        def fail(code, msg):
            return self.send(code, json.dumps({"ok": False, "error": f"NOT SAVED: {msg}"}, ensure_ascii=False),
                             "application/json")
        if self.cross_site():
            return fail(403, "cross-site write refused")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return fail(411, "Content-Length required")
        if n <= 0:
            return fail(400, "empty body")
        if n > voice_store.MAX_JSON_BYTES:
            # Refuse without parsing. Drain a bounded amount first so the client reads the 413 instead of a
            # connection reset; anything bigger than the drain cap gets the reset and that is fine.
            left = min(n, 4 * voice_store.MAX_JSON_BYTES)
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
            self.close_connection = True
            return fail(413, f"body larger than {voice_store.MAX_JSON_BYTES} bytes")
        raw = self.rfile.read(n)
        if len(raw) != n:
            return fail(400, "body shorter than Content-Length")
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return fail(400, f"body is not valid JSON: {str(exc)[:120]}")
        try:
            meta, pcm = voice_store.parse_sample(req)
        except voice_store.ValidationError as exc:
            return fail(400, str(exc))
        try:
            record, created = voice().save(meta, pcm, {"ua": self.headers.get("User-Agent", "")[:80]})
        except voice_store.ConflictError as exc:
            return fail(409, str(exc))
        except OSError as exc:
            return fail(500, f"could not write: {exc}")
        return self.send(200, json.dumps({"ok": True, "created": created, "sample": record}, ensure_ascii=False),
                         "application/json")


if __name__ == "__main__":
    print(f"n1-lab on :{PORT}, data {DATA}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
