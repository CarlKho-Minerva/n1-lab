#!/usr/bin/env python3
"""In-memory stand-in for the voice API, used only by tests/test_voice_frontend.py.

Serves voice.html at /voice, the fonts, and the fixed contract:
    GET  /api/voice/status            -> {ok, samples, counts, policy:'collection_only'}
    POST /api/voice/sample            -> {ok, sample}
    GET  /api/voice/clip/<uuid>.wav   -> WAV wrapper of the stored PCM
    POST /__test/fail_next?n=N        -> make the next N sample POSTs fail with 500 (tests only)
Nothing is written to disk. This is not the production backend; the other agent owns app.py.
"""
import base64
import json
import os
import re
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
LABELS = {"carl_live", "other_live", "carl_replay", "background", "mention"}
LOCK = threading.Lock()
STORE = {}          # id -> {"meta": {...}, "pcm": bytes}
FAIL = {"n": 0}
RATE = 16000


def minmax(pcm, bins=200):
    n = len(pcm) // 2
    if n == 0:
        return []
    vals = struct.unpack("<%dh" % n, pcm)
    out = []
    per = n / bins
    for b in range(bins):
        s, e = int(b * per), max(int(b * per) + 1, int((b + 1) * per))
        seg = vals[s:e]
        out.extend([min(seg) / 32768.0, max(seg) / 32768.0])
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if os.environ.get("STUB_VERBOSE"):
            print(fmt % args, file=sys.stderr)

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/voice", "/voice.html"):
            with open(os.path.join(ROOT, "voice.html"), "rb") as fh:
                return self.send(200, fh.read(), "text/html; charset=utf-8")
        m = re.match(r"^/fonts/([A-Za-z0-9_.-]+)$", path)
        if m and ".." not in m.group(1):
            fp = os.path.join(ROOT, "fonts", m.group(1))
            if os.path.isfile(fp):
                with open(fp, "rb") as fh:
                    return self.send(200, fh.read(), "font/woff2")
        if path == "/api/voice/status":
            with LOCK:
                samples = [v["meta"] for v in STORE.values()]
            counts = {}
            for s in samples:
                counts.setdefault(s["label"], {"fit": 0, "test": 0})[s["split"]] += 1
            return self.send(200, json.dumps({"ok": True, "samples": samples, "counts": counts, "policy": "collection_only"}))
        m = re.match(r"^/api/voice/clip/([0-9a-fA-F-]{36})\.wav$", path)
        if m:
            with LOCK:
                item = STORE.get(m.group(1).lower())
            if not item:
                return self.send(404, '{"ok":false,"error":"no such clip"}')
            pcm = item["pcm"]
            hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16) + b"data" + struct.pack("<I", len(pcm))
            return self.send(200, hdr + pcm, "audio/wav")
        if path == "/":
            return self.send(200, "<a href='/voice'>voice</a>", "text/html")
        return self.send(404, '{"ok":false,"error":"not found"}')

    def do_POST(self):
        path, _, qs = self.path.partition("?")
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b""
        if path == "/__test/fail_next":
            FAIL["n"] = int(re.search(r"n=(\d+)", qs).group(1)) if re.search(r"n=(\d+)", qs) else 1
            return self.send(200, '{"ok":true}')
        if path == "/__test/reset":
            with LOCK:
                STORE.clear()
            FAIL["n"] = 0
            return self.send(200, '{"ok":true}')
        if path != "/api/voice/sample":
            return self.send(404, '{"ok":false,"error":"no such route"}')
        if FAIL["n"] > 0:
            FAIL["n"] -= 1
            return self.send(500, '{"ok":false,"error":"injected failure"}')
        try:
            req = json.loads(raw)
            sid = str(req["id"]).lower()
            if not UUID_RE.match(sid):
                raise ValueError("id is not a uuid")
            if req["label"] not in LABELS:
                raise ValueError("bad label")
            if req["split"] not in ("fit", "test"):
                raise ValueError("bad split")
            if not UUID_RE.match(str(req["session"])):
                raise ValueError("session is not a uuid")
            note = str(req.get("note", ""))
            if len(note) > 500:
                raise ValueError("note too long")
            pcm = base64.b64decode(req["pcm_b64"], validate=True)
            if len(pcm) % 2 or not pcm:
                raise ValueError("pcm not s16le")
            marker = req.get("context_marker_s")
            if marker is not None and not isinstance(marker, (int, float)):
                raise ValueError("bad marker")
        except (KeyError, ValueError, TypeError) as exc:
            return self.send(400, json.dumps({"ok": False, "error": f"bad request: {exc}"}))
        vals = struct.unpack("<%dh" % (len(pcm) // 2), pcm)
        rms = (sum(v * v for v in vals) / len(vals)) ** 0.5 / 32768.0
        meta = {"id": sid, "label": req["label"], "split": req["split"], "session": req["session"], "device": str(req.get("device", ""))[:200],
                "note": note, "duration_s": len(vals) / RATE, "rms": rms, "clipping": any(abs(v) >= 32700 for v in vals),
                "waveform": minmax(pcm), "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "context_marker_s": marker}
        with LOCK:
            if sid in STORE:                                   # idempotent: same id, same answer, no duplicate
                meta = STORE[sid]["meta"]
            else:
                STORE[sid] = {"meta": meta, "pcm": pcm}
        return self.send(200, json.dumps({"ok": True, "sample": meta}))


def serve(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"voice stub on http://127.0.0.1:{port}/voice (memory only)", flush=True)
    serve(port).serve_forever()
