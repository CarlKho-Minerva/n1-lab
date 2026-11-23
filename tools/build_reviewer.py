#!/usr/bin/env python3
"""Build the reviewer copies that the /r/ area serves. Reads the owner's built content, writes a separate tree.

    tools/build_reviewer.py --rules private/reviewer_rules.json [--content private/content] [--out private/reviewer_build]

Nothing is marked for reviewers unless the rules file names it AND its text comes through the leak check with
no hit. For each named entry the script copies the entry's text and each named attachment, applies the exact
edits the rules list (every edit must match exactly as often as it says, or that file is refused), removes
every script, points root links at /r/, then searches the result for private material. A file with any hit is
left out and reported; an entry whose own text has a hit is left out with all its files. The app serves a
reviewer file only if its bytes still match the sha256 recorded here.

The rules file is private (it quotes the strings to cut) and is never committed. Its shape:

    {"site": {"eyebrow": "", "title": "", "intro": ""},
     "block": {"category": "regex", ...},              extra private patterns: names, places, project names
     "block_files": ["path to a file with one regex per line"],
     "allow": [{"text": "exact words", "why": "reason"}],        a hit that lies inside these exact words is let through and listed
     "external_check": ["python3", "path/to/leakcheck.py"],     optional last gate, run on the finished folder; must exit 0
     "entries": [{"slug": "owner-slug", "as": "slug-shown", "mark": true, "n1": true,
                  "set": {"title": "", "question": "", "result": "", "result_line": "", "body_md": "", "date": "", "status": ""},
                  "scrub": [["question", "old", "new"]], "allow": [...],
                  "files": [{"file": "name.html", "mark": true, "label": "", "note": "", "title": "",
                             "scrub": [["old", "new"], ["old", "new", 3]], "allow": [...]}]}]}

Exit 0: built (the report says what was marked and what was refused). Exit 1: a rule is wrong or a final check
failed, and the previous build is left in place. Standard library only.
"""
import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib

NEVER = {"mit-application"}                    # the same refusals as app.py: never markable, whatever the rules say
NEVER_FILE_PREFIXES = ("mit_",)
TEXT_TYPES = (".html", ".md", ".txt")
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")

# Generic patterns: safe to publish, enough to stop the obvious. Names of people, places and private projects
# belong in the private rules file ("block").
BLOCK = {
    "email address": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}",
    "phone number": r"(?<![\d.\w-])(?:\+?1[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\d\w])",
    "local path": r"/Users/|/home/[a-z]|C:\\+Users|~/[A-Za-z.]|/mnt/data|/opt/[a-z]|/private/var|/var/folders",
    "private host or address": r"(?i)[a-z0-9.-]+\.ts\.net|\blocalhost\b|127\.0\.0\.1|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b"
                               r"|\b192\.168\.\d+\.\d+\b|\b10\.\d+\.\d+\.\d+\b",
    "key-like string": r"sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|xox[bap]-[A-Za-z0-9-]{10,}"
                       r"|Bearer\s+[A-Za-z0-9._-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY",
    "medical or mental health": r"(?i)\b(hospital\w*|accident|crash\w*|fractur\w*|surgery|injur\w*|medical|medication|diagnos\w+|therap\w+"
                                r"|adhd|depress\w+|anxiety|lonel\w+|suicid\w*|mental health|doctor|clinic\w*|patient\w*)\b",
    "finance": r"(?i)\b(american express|credit card|bank\w*|salary|rent|debt|loan|invoice\w*)\b",
    "immigration": r"(?i)\b(uscis|i-765|i-20|sevp|immigration|work authori[sz]ation|passport|green card)\b|\b(EAD|OPT|DSO|O-1|visa|Visa)\b",
    "ticket or booking number": r"(?i)\b(ticket|booking|confirmation|order|case)\s*(number|no\.?|#|id)\b",
    "recording": r"(?i)voiceprint|<audio|<video|\.mp3|\.wav|\.m4a|\.mp4",
    "script or write endpoint": r"(?i)/api/|sendBeacon|XMLHttpRequest|\bfetch\s*\(|navigator\.|localStorage|<form\b|<iframe\b|<script\b|\bonclick=|\bonload=",
    "remote load": r"(?i)<link\b[^>]*href=\"https?:|\bsrc=\"https?:|@import|url\(\s*[\"']?https?:",
    "owner-only link": r"(?i)(?:href|src|action)=[\"']/(?!r/)[^\"'/]|\]\(/(?!r/)",
}
B64 = re.compile(r"data:image/([a-z+]+);base64,([A-Za-z0-9+/=]+)")
SCRIPT = re.compile(r"(?is)<script\b.*?</script\s*>")
# Reading on a phone: code wraps instead of running off the edge, printed output wraps between words instead of
# in the middle of a number, wide tables and figures stay inside the screen. Reviewer copies only.
PHONE_CSS = ("<style>.n1r-bar{font:14px/20px Geist,Inter,system-ui,-apple-system,sans-serif;background:#0a0a0a;color:#a1a1a1;"
             "border-bottom:1px solid #262626;padding:12px 16px}.n1r-bar a{color:#52a8ff;text-decoration:none}"
             "img,svg,video,canvas{max-width:100%;height:auto}"
             "@media (max-width:640px){body.jp-Notebook{padding:8px 4px;--jp-content-font-size1:16px;--jp-code-font-size:13px}"
             ".jp-InputPrompt,.jp-OutputPrompt,.jp-Collapser{display:none!important}"
             ".jp-Cell{padding:4px 0!important}.jp-RenderedHTMLCommon{padding-right:0!important}"
             ".jp-CodeCell .jp-InputArea pre,.highlight pre{white-space:pre-wrap!important;overflow-wrap:anywhere;word-break:break-word}"
             ".jp-OutputArea-output pre{white-space:pre-wrap!important;word-break:normal!important;overflow-wrap:break-word}"
             ".jp-RenderedHTMLCommon table,.jp-OutputArea-output table{display:block;overflow-x:auto;max-width:100%}"
             "pre{max-width:100%;overflow-x:auto}}</style>")


class RuleError(Exception):
    """A rule that cannot be applied as written. The build stops; nothing is swapped in."""


def esc(s):
    return html.escape(str(s), quote=True)


def png_text(data):
    """The text a PNG carries beside its pixels (tEXt, zTXt, iTXt, eXIf), or None when it is not a PNG."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    out, i = [], 8
    while i + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[i:i + 8])
        body = data[i + 8:i + 8 + n]
        if kind in (b"tEXt", b"iTXt", b"eXIf"):
            out.append(body.decode("latin-1"))
        elif kind == b"zTXt":
            key, _, rest = body.partition(b"\x00")
            try:
                out.append(key.decode("latin-1") + " " + zlib.decompress(rest[1:]).decode("latin-1"))
            except zlib.error:
                out.append(key.decode("latin-1") + " [zTXt chunk that does not decompress]")
        i += 12 + n
    return "\n".join(out)


class Checker:
    def __init__(self, rules):
        block = dict(BLOCK)
        for name, rx in (rules.get("block") or {}).items():
            block[f"private: {name}"] = rx
        for path in rules.get("block_files") or []:
            path = os.path.expanduser(path)
            if not os.path.exists(path):
                raise RuleError(f"block_files: {path} does not exist")
            with open(path, encoding="utf-8") as fh:
                extra = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
            if extra:
                block[f"private: list {os.path.basename(path)} ({len(extra)} patterns)"] = "(?i)" + "|".join(f"(?:{t})" for t in extra)
        try:
            self.rx = {k: re.compile(v) for k, v in block.items()}
        except re.error as exc:
            raise RuleError(f"a block pattern does not compile: {exc}") from None
        self.allow = list(rules.get("allow") or [])
        self.used = []                                                # (where, allowed text, why, context)

    def scan(self, where, text, allow=()):
        """Hits in one text as (category, context). An allowance must be spelled out with a reason and is recorded."""
        allow = [a for a in list(self.allow) + list(allow) if isinstance(a, dict) and a.get("text") and a.get("why")]
        hits, blobs = [], [("", B64.sub("data:image;base64,", text))]
        for m in B64.finditer(text):
            kind = m.group(1)
            try:
                data = base64.b64decode(m.group(2))
            except Exception as exc:                                   # a malformed image is itself worth refusing
                hits.append(("inline image", f"undecodable: {exc}"))
                continue
            if kind == "svg+xml":
                blobs.append((" [inline svg]", data.decode("utf-8", "replace")))
            elif kind == "png":
                meta = png_text(data)
                if meta is None:
                    hits.append(("inline image", "labelled png but is not a PNG"))
                else:
                    blobs.append((" [inline png text]", meta))
            else:
                blobs.append((f" [inline {kind}]", "".join(ch if 32 <= ord(ch) < 127 else " " for ch in data.decode("latin-1"))))
        for tag, body in blobs:
            for cat, rx in self.rx.items():
                for m in rx.finditer(body):
                    ctx = body[max(0, m.start() - 40):m.end() + 40].replace("\n", " ")
                    # an allowance covers a hit only when the matched words lie inside the allowed text itself,
                    # so it cannot wave through a different hit that merely sits nearby
                    ok = next((a for a in allow if a["text"] in ctx and m.group(0).lower() in a["text"].lower()), None)
                    if ok:
                        self.used.append((where + tag, ok["text"], ok["why"], ctx.strip()))
                    else:
                        hits.append((cat + tag, ctx.strip()))
        return hits

    def scan_bytes(self, where, data):
        """A picture on disk: only the text it carries, and plain strings in other formats."""
        meta = png_text(data)
        text = meta if meta is not None else "".join(ch if 32 <= ord(ch) < 127 else " " for ch in data.decode("latin-1"))
        return self.scan(where, text)


def apply_scrubs(text, scrubs, where):
    for row in scrubs or []:
        if not isinstance(row, list) or len(row) not in (2, 3) or not all(isinstance(x, str) for x in row[:2]) or not row[0]:
            raise RuleError(f"{where}: a scrub must be [old, new] or [old, new, count]")
        want = row[2] if len(row) == 3 else 1
        got = text.count(row[0])
        if got != want:
            raise RuleError(f"{where}: scrub matched {got} times, expected {want}: {row[0][:70]!r}")
        text = text.replace(row[0], row[1])
    return text


def first_sentence(text):
    """The one-line result for the index: the result itself when it is already short, else its first sentence."""
    text = text.strip()
    if len(text) <= 160:
        return text
    m = re.match(r"(.+?[.!?])(?:\s|$)", text, re.S)
    return (m.group(1) if m else text)[:400]


def relink(text, files_ok, slugs_ok, is_html):
    """Point root links at the reviewer area when the target is something a reviewer can open; leave the rest,
    so the leak check reports it as an owner-only link."""
    def fix(m):
        kind, name = m.group(2), m.group(3)
        if kind == "files" and name in files_ok:
            return f"{m.group(1)}/r/files/{name}"
        if kind == "x" and name in slugs_ok:
            return f"{m.group(1)}/r/x/{slugs_ok[name]}"
        return m.group(0)
    pat = r"((?:href|src)=[\"'])/(files|x)/([A-Za-z0-9_.-]+)" if is_html else r"(\]\()/(files|x)/([A-Za-z0-9_.-]+)"
    return re.sub(pat, fix, text)


def build(rules, content, tmp):
    with open(os.path.join(content, "experiments.json"), encoding="utf-8") as fh:
        owner = {e["slug"]: e for e in json.load(fh)["experiments"]}
    never_files = {a["file"] for s in NEVER if s in owner for a in owner[s].get("attachments", [])}
    check = Checker(rules)
    os.makedirs(os.path.join(tmp, "reviewer", "files"))
    wanted = [e for e in rules.get("entries") or [] if e.get("mark") is True]
    for e in wanted:
        if e.get("slug") in NEVER or e.get("as") in NEVER:
            raise RuleError(f"{e.get('slug')}: this entry can never be marked for reviewers")
        if e.get("slug") not in owner:
            raise RuleError(f"{e.get('slug')}: no such entry in the built content")
        if not SLUG_RE.match(e.get("as") or e["slug"]):
            raise RuleError(f"{e.get('slug')}: bad slug")
    shown = [e.get("as") or e["slug"] for e in wanted]
    if len(set(shown)) != len(shown):
        raise RuleError("two entries would be shown under the same slug")
    slugs_ok = {e["slug"]: e.get("as") or e["slug"] for e in wanted}
    files_ok = {f["file"] for e in wanted for f in e.get("files") or [] if f.get("mark") is True}
    out, report, seen_files = [], [], set()
    for e in wanted:
        src, slug = owner[e["slug"]], e.get("as") or e["slug"]
        row = {k: str((e.get("set") or {}).get(k, src.get(k, "")) or "") for k in ("title", "question", "result", "body_md", "date", "status")}
        for s in e.get("scrub") or []:                             # [field, old, new]: an exact edit to the entry's own text
            if not isinstance(s, list) or len(s) != 3 or s[0] not in ("title", "question", "result", "body_md"):
                raise RuleError(f"{slug}: an entry scrub must be [field, old, new] with field title, question, result or body_md")
            row[s[0]] = apply_scrubs(row[s[0]], [s[1:]], f"{slug}.{s[0]}")
        row["body_md"] = relink(row["body_md"], files_ok, slugs_ok, False)
        row["result_line"] = str((e.get("set") or {}).get("result_line") or first_sentence(row["result"]))
        text = f"slug: {slug}\n" + "\n".join(f"{k}: {v}" for k, v in row.items())      # the slug is in every link
        hits = check.scan(f"{slug} (entry text)", text, e.get("allow") or [])
        rep = {"slug": slug, "from": e["slug"], "marked": not hits, "hits": hits, "files": []}
        files = []
        for f in e.get("files") or []:
            name = f.get("file")
            fr = {"file": name, "marked": False, "why": "", "hits": []}
            rep["files"].append(fr)
            if f.get("mark") is not True:
                fr["why"] = "not marked in the rules"
                continue
            if not isinstance(name, str) or not FILE_RE.match(name) or ".." in name:
                raise RuleError(f"{slug}: bad file name {name!r}")
            if name.lower().startswith(NEVER_FILE_PREFIXES) or name in never_files:
                raise RuleError(f"{slug}: {name} belongs to an entry that can never be marked for reviewers")
            if name in seen_files:
                raise RuleError(f"{name} is listed twice")
            if name not in {a["file"] for a in src.get("attachments", [])}:
                raise RuleError(f"{slug}: {name} is not an attachment of {e['slug']}")
            ext = os.path.splitext(name)[1].lower()
            if ext not in TEXT_TYPES + IMAGE_TYPES:
                fr["why"] = f"file type {ext or '(none)'} is not served to reviewers"
                continue
            with open(os.path.join(content, "files", name), "rb") as fh:
                data = fh.read()
            if ext in TEXT_TYPES:
                try:
                    s = data.decode("utf-8")
                except UnicodeDecodeError:
                    fr["why"] = "not valid UTF-8 text"
                    continue
                s = apply_scrubs(s, f.get("scrub"), name)
                if ext == ".html":
                    s, fr["scripts_removed"] = SCRIPT.subn("", s)
                    s = s.replace("\u2014", "-").replace("\u2013", "-")     # the site's rule: no em or en dashes on a page
                    s = relink(s, files_ok, slugs_ok, True)
                    label = f.get("title") or f.get("label")
                    if label:
                        s = re.sub(r"(?is)<title>.*?</title>", lambda m: f"<title>{esc(label)}</title>", s, count=1)
                    bar = (f'<div class="n1r-bar"><a href="/r/x/{esc(slug)}">&larr; {esc(row["title"])}</a>'
                           + (f' / {esc(f.get("label"))}' if f.get("label") else "") + "</div>")
                    s, n_head = re.subn(r"(?i)</head>", lambda m: PHONE_CSS + m.group(0), s, count=1)
                    s, n_body = re.subn(r"(?i)(<body\b[^>]*>)", lambda m: m.group(1) + bar, s, count=1)
                    if n_head != 1 or n_body != 1:
                        fr["why"] = "no <head> or <body> to attach the way back to the study"
                        continue
                else:
                    s = relink(s, files_ok, slugs_ok, False)
                fr["hits"] = check.scan(name, s, (e.get("allow") or []) + (f.get("allow") or []))
                data = s.encode("utf-8")
            else:
                fr["hits"] = check.scan_bytes(name, data)
            if fr["hits"]:
                fr["why"] = f"{len(fr['hits'])} leak-check hits"
                continue
            if hits:
                fr["why"] = "the entry's own text has leak-check hits"
                continue
            with open(os.path.join(tmp, "reviewer", "files", name), "wb") as fh:
                fh.write(data)
            seen_files.add(name)
            fr.update(marked=True, bytes=len(data))
            files.append({"file": name, "reviewer": True, "sha256": hashlib.sha256(data).hexdigest(),
                          "label": str(f.get("label") or name), "note": str(f.get("note") or "")})
        if not hits:
            out.append(dict(row, slug=slug, reviewer=True, n1=e.get("n1") is True, attachments=files))
        report.append(rep)
    site = {k: v for k, v in (rules.get("site") or {}).items() if k in ("eyebrow", "title", "intro") and isinstance(v, str)}
    site_hits = check.scan("site header", "\n".join(site.values()))
    if site_hits:
        raise RuleError(f"the site header text has leak-check hits: {site_hits[:3]}")
    with open(os.path.join(tmp, "reviewer", "experiments.json"), "w", encoding="utf-8") as fh:
        json.dump({"built": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "site": site, "experiments": out}, fh, ensure_ascii=False, indent=1)
    return out, report, check


def verify(tmp):
    """Read the finished folder back the way the app will: every file listed, marked, present and unchanged; no strays."""
    with open(os.path.join(tmp, "reviewer", "experiments.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    listed = {}
    for e in doc["experiments"]:
        if e.get("reviewer") is not True or e["slug"] in NEVER:
            return f"{e.get('slug')}: in the manifest without a mark, or never markable"
        for a in e["attachments"]:
            listed[a["file"]] = a["sha256"]
    on_disk = set(os.listdir(os.path.join(tmp, "reviewer", "files")))
    if on_disk != set(listed):
        return f"files on disk and files listed differ: {sorted(on_disk ^ set(listed))[:5]}"
    for name, want in listed.items():
        with open(os.path.join(tmp, "reviewer", "files", name), "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() != want:
                return f"{name}: bytes on disk do not match the recorded sha256"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rules", required=True)
    ap.add_argument("--content", default="private/content")
    ap.add_argument("--out", default="private/reviewer_build")
    ap.add_argument("--report", help="write the full report here as JSON (it quotes private text: keep it private)")
    args = ap.parse_args()
    try:
        with open(args.rules, encoding="utf-8") as fh:
            rules = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"FAIL: cannot read the rules file {args.rules}: {exc}", file=sys.stderr)
        return 1
    out = os.path.abspath(args.out)
    tmp = f"{out}.building-{os.getpid()}"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    try:
        entries, report, check = build(rules, args.content, tmp)
        problem = verify(tmp)
        if problem:
            raise RuleError(f"read-back check failed: {problem}")
        ext = rules.get("external_check")
        if ext:
            cmd = [os.path.expanduser(x) for x in ext] + [os.path.join(tmp, "reviewer")]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            tail = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
            print(f"external check: exit {r.returncode}: {tail}")
            if r.returncode != 0:
                kept = f"{out}.rejected-{time.strftime('%Y%m%d-%H%M%S')}"
                os.replace(tmp, kept)
                with open(os.path.join(kept, "external_check.txt"), "w", encoding="utf-8") as fh:
                    fh.write(r.stdout + "\n" + r.stderr)
                print(f"FAIL: the external leak check found something in the finished folder. Nothing was swapped in.\n"
                      f"      Its full output: {kept}/external_check.txt", file=sys.stderr)
                return 1
    except (RuleError, OSError, KeyError, ValueError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"FAIL: {type(exc).__name__}: {exc}\n      Nothing was built; the previous build is unchanged.", file=sys.stderr)
        return 1
    if os.path.exists(out):                                        # keep one earlier build: the undo for a bad swap
        prev = out + ".prev"
        if os.path.exists(prev):
            shutil.rmtree(prev)
        os.replace(out, prev)
    os.replace(tmp, out)
    n_files = sum(len(e["attachments"]) for e in entries)
    size = sum(os.path.getsize(os.path.join(out, "reviewer", "files", f)) for f in os.listdir(os.path.join(out, "reviewer", "files")))
    print(f"built {len(entries)} entries, {n_files} files, {size / 1e6:.2f} MB into {out}/reviewer")
    for rep in report:
        print(f"  {'MARKED ' if rep['marked'] else 'REFUSED'} {rep['slug']}" + (f"  (entry text: {len(rep['hits'])} hits)" if rep["hits"] else ""))
        for cat, ctx in rep["hits"][:5]:
            print(f"            {cat}: ...{ctx[:110]}...")
        for fr in rep["files"]:
            print(f"      {'marked ' if fr['marked'] else 'refused'} {fr['file']}" + (f"  ({fr['why']})" if fr["why"] else ""))
            cats = {}
            for cat, ctx in fr["hits"]:
                cats.setdefault(cat, []).append(ctx)
            for cat, ctxs in cats.items():
                print(f"            {len(ctxs):4d}  {cat}: ...{ctxs[0][:100]}...")
    if check.used:
        print(f"allowed on purpose ({len(check.used)}):")
        for where, text, why, ctx in check.used:
            print(f"      {where}: {text!r} ({why})")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({"entries": report, "allowed": check.used}, fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
