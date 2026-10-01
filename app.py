#!/usr/bin/env python3
"""n1-lab - Carl's N=1 research lab, self-hosted on his OpenHost zone (cloud in a bottle).

An archive of the experiments he runs on himself, plus phone-first review tasks (tick the
labels a model got wrong, blinded reads). Everything except /healthz sits behind the zone's
owner login (see openhost.toml); this file adds no auth of its own and must never be made public,
because it has write endpoints and serves private notes.

Content lives OUTSIDE this repo, in the app's data dir, and is re-read on every request:
    $OPENHOST_APP_DATA_DIR/experiments.json     the archive (one entry per experiment)
    $OPENHOST_APP_DATA_DIR/tasks/<slug>.json    review tasks
    $OPENHOST_APP_DATA_DIR/files/*              attachments (rendered notebooks, write-ups)
    $OPENHOST_APP_DATA_DIR/responses/<slug>.jsonl   Carl's answers, appended by POST, pulled home by the Mac
Copy content in with `oh app ssh` (tools/push.sh). The repo holds code and sample data only.
"""
import html
import json
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("OPENHOST_APP_DATA_DIR", os.path.join(HERE, "sample_data"))
PORT = int(os.environ.get("PORT", "8080"))
FONT_DIR = os.path.join(HERE, "fonts")
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
MAX_BODY = 64 * 1024
LOCK = threading.Lock()

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
        parts.append("<h2>Files</h2>")
        for a in e["attachments"]:
            parts.append(f'<a class="card" href="/files/{esc(a["file"])}"><h3>{esc(a.get("label", a["file"]))}</h3>'
                         f'<p>{esc(a.get("note", ""))}</p></a>')
    return page(e.get("title", slug), "".join(parts))


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


# ---------------------------------------------------------------- server

class H(BaseHTTPRequestHandler):
    server_version = "n1lab/0.1"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def send(self, code, body, ctype="text/html; charset=utf-8", cache="no-store"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def not_found(self):
        self.send(404, page("Not found", '<nav><a href="/">Lab</a></nav><h1>Not here.</h1>'))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            exps, err = experiments()
            return self.send(200 if not err else 500, json.dumps({"ok": not err, "experiments": len(exps), "error": err}),
                             "application/json")
        if path == "/":
            return self.send(200, index_page())
        m = re.match(r"^/x/([A-Za-z0-9_-]{1,64})$", path)
        if m:
            out = experiment_page(m.group(1))
            return self.send(200, out) if out else self.not_found()
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
                    text = open(fp, encoding="utf-8").read()
                    return self.send(200, page(m.group(2), '<nav><a href="/">Lab</a></nav>' + md(text)))
                with open(fp, "rb") as fh:
                    return self.send(200, fh.read(), ctype, "public, max-age=604800" if m.group(1) == "fonts" else "no-store")
        return self.not_found()

    do_HEAD = do_GET

    def do_POST(self):
        m = re.match(r"^/api/task/([A-Za-z0-9_-]{1,64})/answer$", self.path.split("?", 1)[0])
        if not m:
            return self.send(404, '{"error":"no such route"}', "application/json")
        # Same-origin only: a cross-site page must not be able to write answers with Carl's login cookie.
        # Sec-Fetch-Site is set by the browser and a page cannot forge it, so it decides when present.
        # The Origin/Host comparison is only the fallback: the OpenHost router rewrites Host, which made
        # every real same-origin POST look cross-site (403 on the phone, 2026-09-19).
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


if __name__ == "__main__":
    print(f"n1-lab on :{PORT}, data {DATA}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
