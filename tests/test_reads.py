"""Tests for the reading log: POST /api/read, the injected script, and the reading lines on the pages.

Everything runs against a tempfile data dir; nothing touches sample_data or private/.
    python3 -m unittest discover -s tests -p test_reads.py -v
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app                                     # noqa: E402

EXPS = {"experiments": [{"slug": "m3", "title": "M3", "date": "2026-10-04", "status": "done", "tasks": [],
                         "attachments": [{"file": "lesson.html", "label": "Lesson ten"},
                                         {"file": "note.md", "label": "The note"}]}]}


def checkpoint(**kw):
    row = {"v": "view-0001", "seq": 1, "path": "/files/lesson.html", "title": "Lesson", "local": "2026-10-17 21:03",
           "visible_s": 130, "active_s": 125, "max_depth": 0.4, "dwell": [0] * 100,
           "vw": 390, "vh": 664, "doc_h": 20000, "dpr": 3}
    row["dwell"][:40] = [3] * 40
    row.update(kw)
    return row


class ReadsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "files"))
        with open(os.path.join(self.dir, "experiments.json"), "w") as fh:
            json.dump(EXPS, fh)
        with open(os.path.join(self.dir, "files", "lesson.html"), "w") as fh:
            fh.write("<html><body><h1>GRU</h1></body></html>")
        with open(os.path.join(self.dir, "files", "note.md"), "w") as fh:
            fh.write("# Note\n\nhello")
        with open(os.path.join(self.dir, "files", "pic.png"), "wb") as fh:
            fh.write(b"\x89PNG</body>")
        app.DATA = self.dir
        app._READS.update(ino=None, offset=0, views={})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir)

    def req(self, method, path, body=None, sfs="same-origin"):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        headers = {"Content-Type": "application/json"}
        if sfs:
            headers["Sec-Fetch-Site"] = sfs
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        out = (r.status, r.read())
        c.close()
        return out

    def log_lines(self):
        try:
            with open(app.reads_file()) as fh:
                return fh.readlines()
        except FileNotFoundError:
            return []

    def test_valid_checkpoint_is_written_and_latest_seq_wins(self):
        self.assertEqual(self.req("POST", "/api/read", checkpoint())[0], 200)
        later = checkpoint(seq=2, active_s=300, visible_s=310)
        self.assertEqual(self.req("POST", "/api/read", later)[0], 200)
        stale = checkpoint(seq=1, active_s=5, visible_s=5)        # a late-arriving older checkpoint must not win
        self.assertEqual(self.req("POST", "/api/read", stale)[0], 200)
        self.assertEqual(len(self.log_lines()), 3)
        v = app.views()
        self.assertEqual(len(v), 1)
        self.assertEqual(v["view-0001"]["active_s"], 300)
        self.assertEqual(v["view-0001"]["vw"], 390)

    def test_refusals(self):
        self.assertEqual(self.req("POST", "/api/read", checkpoint(), sfs="cross-site")[0], 403)
        bad = [checkpoint(path="/t/gold50"), checkpoint(path="/files/../x"), checkpoint(v="x"),
               checkpoint(dwell=[1] * 99), checkpoint(active_s=200, visible_s=100), checkpoint(max_depth=2),
               checkpoint(seq=0), checkpoint(vw=-1)]
        for b in bad:
            self.assertEqual(self.req("POST", "/api/read", b)[0], 400, b)
        self.assertEqual(self.req("POST", "/api/read", b'{"v":')[0], 400)
        self.assertEqual(self.req("POST", "/api/read", b'{"max_depth": NaN}')[0], 400)
        self.assertEqual(self.req("POST", "/api/read", b"x" * (app.MAX_READ_BODY + 1))[0], 400)
        self.assertEqual(self.log_lines(), [])

    def test_unwritable_log_is_a_500_not_a_silent_200(self):
        os.makedirs(os.path.join(self.dir, "reads"))
        os.chmod(os.path.join(self.dir, "reads"), 0o500)
        try:
            if os.access(os.path.join(self.dir, "reads"), os.W_OK):
                self.skipTest("running as a user that ignores directory permissions")
            self.assertEqual(self.req("POST", "/api/read", checkpoint())[0], 500)
        finally:
            os.chmod(os.path.join(self.dir, "reads"), 0o700)

    def test_half_written_line_waits(self):
        self.req("POST", "/api/read", checkpoint())
        with open(app.reads_file(), "a") as fh:
            fh.write('{"v": "view-0002", "seq": 1, "pa')
        self.assertEqual(set(app.views()), {"view-0001"})
        with open(app.reads_file(), "a") as fh:
            fh.write('th": "/files/note.md", "active_s": 10, "dwell": [1], "local": ""}\n')
        self.assertEqual(set(app.views()), {"view-0001", "view-0002"})

    def test_script_injected_into_pages_not_assets(self):
        st, body = self.req("GET", "/files/lesson.html")
        self.assertEqual(st, 200)
        self.assertIn(b"__n1read", body)
        self.assertLess(body.index(b"__n1read"), body.index(b"</body>"))
        self.assertIn(b"__n1read", self.req("GET", "/files/note.md")[1])
        self.assertIn(b"__n1read", self.req("GET", "/x/m3")[1])
        self.assertEqual(self.req("GET", "/files/pic.png")[1], b"\x89PNG</body>")
        self.assertNotIn(b"__n1read", self.req("GET", "/")[1])

    def test_pages_show_what_was_read(self):
        page = self.req("GET", "/x/m3")[1].decode()
        self.assertEqual(page.count("no visit logged since"), 2)
        self.req("POST", "/api/read", checkpoint())
        page = self.req("GET", "/x/m3")[1].decode()
        self.assertEqual(page.count("no visit logged since"), 1)
        self.assertIn("2 min active, 40% of the page on screen, last 2026-10-17 21:03", page)
        reads = self.req("GET", "/reads")[1].decode()
        self.assertIn("The note", reads)                       # still unread
        self.assertIn("390x664", reads)
        self.assertIn("1 of 2 files opened", self.req("GET", "/")[1].decode())
        rows = self.req("GET", "/export-reads")[1].decode().splitlines()
        self.assertEqual(json.loads(rows[0])["path"], "/files/lesson.html")

    def test_browser_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        r = subprocess.run([node, "--check", "-"], input=app.READ_JS, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
