"""Tests for the voice lab backend: voice_store.py and the /voice routes in app.py.

Everything runs against a tempfile data dir; nothing touches sample_data or private/.
    python3 -m unittest tests.test_voice_store -v
"""
import base64
import http.client
import json
import math
import os
import stat
import struct
import sys
import tempfile
import threading
import unittest
import uuid
import wave
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app                                     # noqa: E402
import voice_store                             # noqa: E402

SR = voice_store.SAMPLE_RATE


def tone(seconds=1.0, amp=8000, freq=440.0):
    n = int(seconds * SR)
    return struct.pack(f"<{n}h", *(int(amp * math.sin(2 * math.pi * freq * i / SR)) for i in range(n)))


def silence(seconds=1.0):
    return b"\x00\x00" * int(seconds * SR)


def body(pcm=None, **over):
    doc = {"id": str(uuid.uuid4()), "label": "carl_live", "split": "fit", "session": str(uuid.uuid4()),
           "device": "iPhone test", "note": "hello", "pcm_b64": base64.b64encode(pcm or tone()).decode("ascii")}
    doc.update(over)
    return doc


# ---------------------------------------------------------------- pure functions

class ParseTests(unittest.TestCase):
    def ok(self, **over):
        return voice_store.parse_sample(body(**over))

    def bad(self, msg, **over):
        with self.assertRaises(voice_store.ValidationError, msg=msg) as cm:
            voice_store.parse_sample(body(**over))
        return str(cm.exception)

    def test_valid_minimal(self):
        meta, pcm = self.ok()
        self.assertEqual(meta["label"], "carl_live")
        self.assertIsNone(meta["context_marker_s"])
        self.assertEqual(len(pcm), 2 * SR)

    def test_uuid_normalised_lowercase(self):
        u = str(uuid.uuid4()).upper()
        meta, _ = self.ok(id=u)
        self.assertEqual(meta["id"], u.lower())

    def test_not_an_object(self):
        for v in ([], "x", 3, None):
            with self.assertRaises(voice_store.ValidationError):
                voice_store.parse_sample(v)

    def test_missing_and_unknown_fields(self):
        d = body()
        del d["note"]
        with self.assertRaises(voice_store.ValidationError) as cm:
            voice_store.parse_sample(d)
        self.assertIn("note", str(cm.exception))
        self.assertIn("unknown", self.bad("unknown", extra=1))

    def test_bad_ids(self):
        for v in ("../../etc/passwd", "abc", 12, None, "12345678-1234-1234-1234-12345678901g", ""):
            self.bad("id", id=v)
            self.bad("session", session=v)

    def test_bad_enums(self):
        for v in ("Carl_live", "", "carl", None, 1, ["carl_live"]):
            self.bad("label", label=v)
        for v in ("train", "", None, 0):
            self.bad("split", split=v)

    def test_text_fields(self):
        self.bad("device type", device=5)
        self.bad("device nl", device="a\nb")
        self.bad("device long", device="x" * (voice_store.DEVICE_MAX + 1))
        self.bad("note type", note=None)
        self.bad("note long", note="x" * 501)
        self.bad("note ctrl", note="a\x00b")
        meta, _ = self.ok(note="x" * 500, device="")
        self.assertEqual(len(meta["note"]), 500)
        meta, _ = self.ok(note="line one\nline two\ttabbed")
        self.assertIn("\n", meta["note"])

    def test_pcm_validation(self):
        self.bad("type", pcm_b64=123)
        self.bad("not b64", pcm_b64="!!!!")
        self.bad("bad len", pcm_b64="abc")
        self.bad("odd bytes", pcm_b64=base64.b64encode(tone() + b"\x00").decode())
        self.bad("too short", pcm_b64=base64.b64encode(tone(0.99)).decode())
        self.bad("too long", pcm_b64=base64.b64encode(silence(15.01)).decode())
        self.bad("absurd b64 bounded before decode", pcm_b64="A" * (voice_store.MAX_B64_CHARS + 4))
        self.ok(pcm_b64=base64.b64encode(silence(15.0)).decode())
        self.ok(pcm_b64=base64.b64encode(silence(1.0)).decode())

    def test_context_marker(self):
        meta, _ = self.ok(context_marker_s=0.5)
        self.assertEqual(meta["context_marker_s"], 0.5)
        meta, _ = self.ok(context_marker_s=None)
        self.assertIsNone(meta["context_marker_s"])
        for v in ("0.5", True, float("nan"), float("inf"), -0.1, 1.01, [], 10 ** 400):
            self.bad(f"marker {v!r}", context_marker_s=v)


class AnalyzeTests(unittest.TestCase):
    def test_silence(self):
        a = voice_store.analyze(silence(1.0))
        self.assertEqual(a["n_samples"], SR)
        self.assertEqual(a["duration_s"], 1.0)
        self.assertEqual(a["rms"], 0.0)
        self.assertIsNone(a["rms_dbfs"])
        self.assertEqual(a["peak"], 0.0)
        self.assertEqual(a["clipping"]["count"], 0)
        self.assertEqual(len(a["waveform"]["min"]), 200)
        self.assertEqual(len(a["waveform"]["max"]), 200)

    def test_tone(self):
        a = voice_store.analyze(tone(1.0, amp=8000))
        self.assertAlmostEqual(a["rms"], 8000 / 32768 / math.sqrt(2), places=3)
        self.assertAlmostEqual(a["peak"], 8000 / 32768, places=3)
        self.assertEqual(a["clipping"]["count"], 0)
        self.assertTrue(all(mn <= mx for mn, mx in zip(a["waveform"]["min"], a["waveform"]["max"])))
        self.assertTrue(all(-1 <= x <= 1 for x in a["waveform"]["min"] + a["waveform"]["max"]))

    def test_clipping_and_endianness(self):
        n = SR
        pcm = struct.pack(f"<{n}h", *([32767] * (n // 2) + [-32768] * (n - n // 2)))   # explicit little-endian
        a = voice_store.analyze(pcm)
        self.assertEqual(a["clipping"]["count"], n)
        self.assertEqual(a["clipping"]["ratio"], 1.0)
        self.assertAlmostEqual(a["peak"], 1.0, places=3)
        self.assertEqual(a["waveform"]["max"][0], round(32767 / 32768, 4))
        self.assertEqual(a["waveform"]["min"][-1], -1.0)

    def test_wav_roundtrip(self):
        pcm = tone(1.5)
        data = voice_store.wav_bytes(pcm)
        import io
        with wave.open(io.BytesIO(data)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, SR))
            self.assertEqual(w.readframes(w.getnframes()), pcm)


# ---------------------------------------------------------------- store

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = voice_store.VoiceStore(os.path.join(self.tmp.name, "voice"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_modes_and_files(self):
        meta, pcm = voice_store.parse_sample(body())
        rec, created = self.store.save(meta, pcm, {"ua": "t"})
        self.assertTrue(created)
        for d in (self.store.root, self.store.samples_dir):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        for ext in ("wav", "json"):
            p = os.path.join(self.store.samples_dir, f"{rec['id']}.{ext}")
            self.assertTrue(os.path.isfile(p))
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertFalse([n for n in os.listdir(self.store.samples_dir) if ".tmp-" in n])
        self.assertEqual(rec["policy"], "collection_only")
        self.assertFalse(rec["trained"])
        self.assertFalse(rec["verified"])
        self.assertEqual(rec["ua"], "t")
        self.assertEqual(rec["sha256"], voice_store.hashlib.sha256(pcm).hexdigest())
        with open(os.path.join(self.store.samples_dir, f"{rec['id']}.json")) as fh:
            self.assertEqual(json.load(fh)["id"], rec["id"])

    def test_idempotent_then_conflict(self):
        d = body()
        meta, pcm = voice_store.parse_sample(d)
        rec1, c1 = self.store.save(meta, pcm)
        rec2, c2 = self.store.save(meta, pcm)
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(rec1["created_at"], rec2["created_at"])
        meta_other, _ = voice_store.parse_sample(dict(d, note="changed"))
        with self.assertRaises(voice_store.ConflictError):
            self.store.save(meta_other, pcm)
        with self.assertRaises(voice_store.ConflictError):
            self.store.save(meta, tone(1.0, amp=100))
        self.assertEqual(len(self.store.list_samples()[0]), 1)

    def test_status_counts_report_and_problems(self):
        self.assertEqual(self.store.status()["counts"]["total"], 0)
        for lb, sp in (("carl_live", "fit"), ("carl_live", "test"), ("background", "fit")):
            meta, pcm = voice_store.parse_sample(body(label=lb, split=sp))
            self.store.save(meta, pcm)
        st = self.store.status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["policy"], "collection_only")
        self.assertEqual(st["counts"]["total"], 3)
        self.assertEqual(st["counts"]["by_label"]["carl_live"], 2)
        self.assertEqual(st["counts"]["by_split"]["test"], 1)
        self.assertEqual(st["counts"]["by_label_split"]["background/fit"], 1)
        self.assertEqual(st["counts"]["by_label_split"]["mention/test"], 0)
        self.assertNotIn("report", st)
        self.assertNotIn("problems", st)
        with open(os.path.join(self.store.root, "report.json"), "w") as fh:
            fh.write('{"note":"from the mac"}')
        self.assertEqual(self.store.status()["report"], {"note": "from the mac"})
        with open(os.path.join(self.store.root, "report.json"), "w") as fh:
            fh.write("{not json")
        self.assertIn("report_error", self.store.status())
        # a stray wav without json is not a sample; a json without wav is a loud problem
        sid = str(uuid.uuid4())
        with open(os.path.join(self.store.samples_dir, f"{sid}.wav"), "wb") as fh:
            fh.write(b"RIFF")
        with open(os.path.join(self.store.samples_dir, f"{str(uuid.uuid4())}.json"), "w") as fh:
            json.dump({"id": "x", "label": "carl_live", "split": "fit"}, fh)
        st = self.store.status()
        self.assertEqual(st["counts"]["total"], 3)
        self.assertEqual(len(st["problems"]), 1)

    def test_clip_lookup(self):
        meta, pcm = voice_store.parse_sample(body())
        rec, _ = self.store.save(meta, pcm)
        data, err = self.store.clip(rec["id"])
        self.assertIsNone(err)
        self.assertEqual(data, voice_store.wav_bytes(pcm))
        self.assertEqual(self.store.clip(str(uuid.uuid4())), (None, "no such sample"))
        self.assertEqual(self.store.clip("../x"), (None, "bad id"))
        self.assertEqual(self.store.clip(rec["id"].upper())[1], None)


# ---------------------------------------------------------------- HTTP

class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_data = app.DATA
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        app.DATA = cls.old_data

    def setUp(self):                                               # fresh data dir per test; app reads DATA per request
        self.tmp = tempfile.TemporaryDirectory()
        app.DATA = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def req(self, method, path, doc=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}       # None removes a default header
        data = raw if raw is not None else (json.dumps(doc).encode() if doc is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, dict(r.getheaders()), out

    def post(self, doc=None, **kw):
        st, h, out = self.req("POST", "/api/voice/sample", doc, **kw)
        return st, json.loads(out)

    def test_voice_page_and_home_link(self):
        st, h, out = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b'href="/voice"', out)
        st, h, out = self.req("GET", "/voice")
        if os.path.isfile(app.VOICE_HTML):
            self.assertEqual(st, 200)
            self.assertEqual(open(app.VOICE_HTML, "rb").read(), out)
        else:
            self.assertEqual(st, 500)                             # loud, not a 404 that looks like a typo
            self.assertIn(b"voice.html missing", out)

    def test_status_empty(self):
        st, h, out = self.req("GET", "/api/voice/status")
        self.assertEqual(st, 200)
        doc = json.loads(out)
        self.assertEqual(doc["ok"], True)
        self.assertEqual(doc["samples"], [])
        self.assertEqual(doc["policy"], "collection_only")
        self.assertEqual(doc["counts"]["total"], 0)
        self.assertEqual(h["Cache-Control"], "no-store")

    def test_post_roundtrip_idempotent_conflict_clip(self):
        d = body(context_marker_s=0.25, note="first")
        st, doc = self.post(d)
        self.assertEqual(st, 200, doc)
        self.assertTrue(doc["ok"])
        self.assertTrue(doc["created"])
        s = doc["sample"]
        self.assertEqual((s["id"], s["label"], s["split"]), (d["id"], "carl_live", "fit"))
        self.assertEqual(s["context_marker_s"], 0.25)
        self.assertEqual(len(s["waveform"]["min"]), 200)
        self.assertNotIn("pcm_b64", s)
        # same bytes + same metadata: 200, not created again
        st, doc2 = self.post(d)
        self.assertEqual(st, 200)
        self.assertFalse(doc2["created"])
        self.assertEqual(doc2["sample"]["created_at"], s["created_at"])
        # same id, different metadata: 409 and nothing changed
        st, doc3 = self.post(dict(d, note="second"))
        self.assertEqual(st, 409)
        self.assertFalse(doc3["ok"])
        self.assertIn("NOT SAVED", doc3["error"])
        st, doc4 = self.post(dict(d, pcm_b64=base64.b64encode(tone(1.0, amp=100)).decode()))
        self.assertEqual(st, 409)
        # status lists exactly one
        st, h, out = self.req("GET", "/api/voice/status")
        status = json.loads(out)
        self.assertEqual(status["counts"]["total"], 1)
        self.assertEqual(status["samples"][0]["note"], "first")
        # the clip comes back as a real wav, private, no-store
        st, h, out = self.req("GET", f"/api/voice/clip/{d['id']}.wav")
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Type"], "audio/wav")
        self.assertIn("no-store", h["Cache-Control"])
        import io
        with wave.open(io.BytesIO(out)) as w:
            self.assertEqual(w.getframerate(), SR)
            self.assertEqual(w.readframes(w.getnframes()), base64.b64decode(d["pcm_b64"]))
        # HEAD works, GET of an unknown id is a 404
        st, h, out = self.req("HEAD", f"/api/voice/clip/{d['id']}.wav")
        self.assertEqual(st, 200)
        self.assertEqual(out, b"")
        st, h, out = self.req("GET", f"/api/voice/clip/{uuid.uuid4()}.wav")
        self.assertEqual(st, 404)

    def test_clip_path_traversal(self):
        for p in ("/api/voice/clip/../../app.py", "/api/voice/clip/..%2F..%2Fapp.py", "/api/voice/clip/x.wav",
                  "/api/voice/clip/%2e%2e/report.json", "/api/voice/clip/00000000-0000-0000-0000-000000000000.wav/../x"):
            st, h, out = self.req("GET", p)
            self.assertEqual(st, 404, p)

    def test_malformed_bodies(self):
        cases = {
            "not json": (400, dict(raw=b"{nope")),
            "not utf8": (400, dict(raw=b"\xff\xfe\x00")),
            "array": (400, dict(raw=b"[]")),
            "empty": (400, dict(raw=b"")),
        }
        for name, (code, kw) in cases.items():
            st, doc = self.post(**kw)
            self.assertEqual(st, code, name)
            self.assertFalse(doc["ok"])
            self.assertTrue(doc["error"].startswith("NOT SAVED"), name)
        st, doc = self.post(body(label="unknown"))
        self.assertEqual(st, 400)
        self.assertIn("label", doc["error"])
        st, doc = self.post(body(id="../../etc/passwd"))
        self.assertEqual(st, 400)
        st, doc = self.post(body(pcm_b64=base64.b64encode(tone(0.5)).decode()))
        self.assertEqual(st, 400)
        self.assertIn("1-15 s", doc["error"])
        st, doc = self.post(body(context_marker_s=float("inf")))      # json.dumps emits Infinity; parser must refuse
        self.assertEqual(st, 400)
        st, doc = self.post(body(note="x" * 501))
        self.assertEqual(st, 400)
        huge = json.dumps(body()).replace('"note": "hello"', '"note": "hello", "context_marker_s": 1' + "0" * 400)
        st, doc = self.post(raw=huge.encode())                     # int too large for float: still a clean 400
        self.assertEqual(st, 400)
        self.assertIn("finite", doc["error"])
        st, doc = self.post(dict(body(), stray="field"))
        self.assertEqual(st, 400)
        st, h, out = self.req("GET", "/api/voice/status")
        self.assertEqual(json.loads(out)["counts"]["total"], 0, "a rejected payload must leave nothing behind")

    def test_oversize(self):
        big = json.dumps(body(pcm_b64="A" * voice_store.MAX_JSON_BYTES)).encode()
        self.assertGreater(len(big), voice_store.MAX_JSON_BYTES)
        st, doc = self.post(raw=big)
        self.assertEqual(st, 413)
        self.assertIn("NOT SAVED", doc["error"])
        # just under the JSON cap but audio over 15 s: refused at the audio bound, not saved
        over = json.dumps(body(pcm_b64=base64.b64encode(silence(15.5)).decode())).encode()
        self.assertLessEqual(len(over), voice_store.MAX_JSON_BYTES)
        st, doc = self.post(raw=over)
        self.assertEqual(st, 400)
        # full-length 15 s clip is accepted and fits the cap
        ok = json.dumps(body(pcm_b64=base64.b64encode(tone(15.0)).decode())).encode()
        self.assertLessEqual(len(ok), voice_store.MAX_JSON_BYTES)
        st, doc = self.post(raw=ok)
        self.assertEqual(st, 200, doc)
        self.assertEqual(doc["sample"]["duration_s"], 15.0)
        # missing Content-Length
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/api/voice/sample")
        c.putheader("Sec-Fetch-Site", "same-origin")
        c.endheaders()
        r = c.getresponse()
        self.assertEqual(r.status, 411)
        c.close()

    def test_cross_origin(self):
        d = body()
        # Sec-Fetch-Site decides when present
        for sfs in ("cross-site", "same-site", "garbage"):
            st, doc = self.post(d, headers={"Sec-Fetch-Site": sfs})
            self.assertEqual(st, 403, sfs)
            self.assertIn("NOT SAVED", doc["error"])
        # fallback: Origin vs Host / X-Forwarded-Host (router rewrites Host)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        raw = json.dumps(d).encode()
        c.request("POST", "/api/voice/sample", body=raw,
                  headers={"Content-Type": "application/json", "Origin": "https://evil.example", "Host": "n1.carl.zone"})
        r = c.getresponse()
        self.assertEqual(r.status, 403)
        r.read()
        c.close()
        st, h, out = self.req("GET", "/api/voice/status")
        self.assertEqual(json.loads(out)["counts"]["total"], 0)
        # same-origin via X-Forwarded-Host succeeds
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/api/voice/sample", body=raw,
                  headers={"Content-Type": "application/json", "Origin": "https://n1.carl.zone",
                           "Host": "internal:8080", "X-Forwarded-Host": "n1.carl.zone"})
        r = c.getresponse()
        self.assertEqual(r.status, 200, r.read())
        r.read()
        c.close()
        # no Origin, no Sec-Fetch-Site (curl from the Mac over the owner login) is allowed, as for tasks
        st, doc = self.post(body(), headers={"Sec-Fetch-Site": None})
        self.assertEqual(st, 200)

    def test_task_route_unchanged(self):
        st, h, out = self.req("POST", "/api/task/nope/answer", {"id": "1", "verdict": "right"})
        self.assertEqual(st, 404)
        st, h, out = self.req("POST", "/api/task/nope/answer", {"id": "1", "verdict": "right"},
                              headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(st, 403)
        st, h, out = self.req("POST", "/api/nothing", {})
        self.assertEqual(st, 404)
        st, h, out = self.req("GET", "/healthz")
        self.assertIn(st, (200, 500))                             # unchanged route, tempdir has no experiments.json


if __name__ == "__main__":
    unittest.main()
