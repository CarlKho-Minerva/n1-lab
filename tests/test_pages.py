"""Tests for the owner pages as a phone reader meets them: a scannable index, the result on the study page,
file pages that say what they are and lead back to their study.
    python3 -m unittest discover -s tests -p test_pages.py -v
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app                                     # noqa: E402

LONG = "A long result sentence. " * 40
EXPS = {"experiments": [{"slug": "m3", "title": "M3 study", "date": "2026-10-04", "status": "done", "tasks": [],
                         "question": "What predicts the next action?", "result": LONG,
                         "attachments": [{"file": "note.md", "label": "START HERE: the note"}]}]}


class PagesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "files"))
        with open(os.path.join(self.dir, "experiments.json"), "w") as fh:
            json.dump(EXPS, fh)
        with open(os.path.join(self.dir, "files", "note.md"), "w") as fh:
            fh.write("# Note\n\nA path like `a/very/long/path/that/would/run/off/a/phone/screen/if/it/could/not/wrap.py` here.")
        with open(os.path.join(self.dir, "files", "loose.md"), "w") as fh:
            fh.write("# Loose\n\nnot attached to any study")
        app.DATA = self.dir
        app._READS.update(ino=None, offset=0, views={})
        app._GUARD.update(stamp=None, on=False)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir)

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        out = r.status, r.read().decode("utf-8")
        c.close()
        return out

    def test_index_cards_are_a_summary(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn('<p class="clamp3">What predicts the next action?</p>', body)
        self.assertIn('<p class="clamp4"><strong>A long result sentence.', body)
        self.assertIn("-webkit-line-clamp:4", body)

    def test_study_page_shows_the_whole_result(self):
        code, body = self.get("/x/m3")
        self.assertEqual(code, 200)
        self.assertIn(">Result</div>", body)
        self.assertEqual(body.count("A long result sentence."), 40)
        self.assertNotIn("clamp4\"><strong>", body)

    def test_markdown_file_is_titled_and_leads_back_to_its_study(self):
        code, body = self.get("/files/note.md")
        self.assertEqual(code, 200)
        self.assertIn("<title>START HERE: the note</title>", body)
        self.assertIn('<a href="/x/m3">M3 study</a>', body)
        code, body = self.get("/files/loose.md")                       # a file in no study keeps the old header
        self.assertEqual(code, 200)
        self.assertIn("<title>loose.md</title>", body)
        self.assertNotIn('href="/x/', body)

    def test_long_code_and_words_wrap_inside_the_column(self):
        css = app.CSS
        self.assertIn("overflow-wrap:anywhere", css[css.index("code{"):css.index("code{") + 200])
        self.assertIn("h1,h2,h3,p,li,td{overflow-wrap:break-word}", css)


if __name__ == "__main__":
    unittest.main()
