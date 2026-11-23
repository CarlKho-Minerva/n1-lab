"""Tests for tools/build_reviewer.py: nothing is marked for reviewers unless it is named and comes out clean.

Made-up content in a tempfile dir; the private rules file and the real content are never read.
    python3 -m unittest discover -s tests -p test_build_reviewer.py -v
"""
import hashlib
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import app                                     # noqa: E402
import build_reviewer                          # noqa: E402
import reviewer_auth                           # noqa: E402

TOOL = os.path.join(ROOT, "tools", "build_reviewer.py")
CLEAN = ("<html><head><title>raw_name</title></head><body><h1>Lesson — one</h1><p>For you, Dana: step one.</p>"
         "<a href=\"/files/second.html\">next</a> <a href=\"/x/study\">back</a>"
         "<script>fetch('/api/read')</script><script src=\"https://cdn.example/x.js\"></script></body></html>")
OWNER = {"experiments": [
    {"slug": "study", "title": "A study", "date": "2026-10-01", "status": "done", "tasks": ["gold"],
     "question": "Asked by Dana: does it work?", "result": "It works. " + "More detail follows here. " * 10, "body_md": "See [the lesson](/files/lesson.html).",
     "attachments": [{"file": "lesson.html", "label": "Lesson"}, {"file": "second.html", "label": "Second"},
                     {"file": "leaky.md", "label": "Leaky"}, {"file": "data.zip", "label": "Zip"}]},
    {"slug": "other", "title": "Other", "date": "2026-10-02", "status": "done", "question": "q", "result": "r",
     "attachments": [{"file": "other.md", "label": "Other"}]},
    {"slug": "mit-application", "title": "Application", "date": "2026-11-22", "status": "running", "question": "q", "result": "r",
     "attachments": [{"file": "mit_notes.md", "label": "Notes"}, {"file": "plan.md", "label": "Plan"}]},
]}
FILES = {"lesson.html": CLEAN, "second.html": "<html><head></head><body><p>Second page.</p></body></html>",
         "leaky.md": "# Notes\n\nWrite to someone@example.com or see /Users/someone/notes.\n", "data.zip": "PK",
         "other.md": "# Other\n\nfine", "mit_notes.md": "# notes", "plan.md": "# plan"}


def rules(**over):
    base = {"site": {"title": "Studies"}, "block": {"people": r"(?i)\bdana\b"},
            "entries": [{"slug": "study", "mark": True, "n1": True,
                         "scrub": [["question", "Asked by Dana: d", "D"]],
                         "files": [{"file": "lesson.html", "mark": True, "label": "Lesson one", "note": "start here",
                                    "scrub": [["For you, Dana: step", "Step"]]},
                                   {"file": "second.html", "mark": True, "label": "Lesson two"},
                                   {"file": "leaky.md", "mark": True, "label": "Leaky"},
                                   {"file": "data.zip", "mark": True}]}]}
    base.update(over)
    return base


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.content = os.path.join(self.dir, "content")
        os.makedirs(os.path.join(self.content, "files"))
        with open(os.path.join(self.content, "experiments.json"), "w") as fh:
            json.dump(OWNER, fh)
        for name, text in FILES.items():
            with open(os.path.join(self.content, "files", name), "w", encoding="utf-8") as fh:
                fh.write(text)
        self.out = os.path.join(self.dir, "build")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_tool(self, r):
        path = os.path.join(self.dir, "rules.json")
        with open(path, "w") as fh:
            json.dump(r, fh)
        return subprocess.run([sys.executable, TOOL, "--rules", path, "--content", self.content, "--out", self.out,
                               "--report", os.path.join(self.dir, "report.json")], capture_output=True, text=True, timeout=60)

    def read(self, *rel):
        with open(os.path.join(self.out, "reviewer", *rel), encoding="utf-8") as fh:
            return fh.read()

    def manifest(self):
        with open(os.path.join(self.out, "reviewer", "experiments.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_clean_files_are_marked_and_leaky_ones_refused(self):
        r = self.run_tool(rules())
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.manifest()
        self.assertEqual([e["slug"] for e in m["experiments"]], ["study"])
        e = m["experiments"][0]
        self.assertIs(e["reviewer"], True)
        self.assertIs(e["n1"], True)
        self.assertEqual(e["question"], "Does it work?")
        self.assertEqual(e["result_line"], "It works.")
        self.assertNotIn("tasks", e)
        self.assertEqual(e["body_md"], "See [the lesson](/r/files/lesson.html).")
        self.assertEqual([a["file"] for a in e["attachments"]], ["lesson.html", "second.html"])
        self.assertTrue(all(a["reviewer"] is True for a in e["attachments"]))
        self.assertEqual(sorted(os.listdir(os.path.join(self.out, "reviewer", "files"))), ["lesson.html", "second.html"])
        page = self.read("files", "lesson.html")
        self.assertEqual(hashlib.sha256(page.encode("utf-8")).hexdigest(), e["attachments"][0]["sha256"])
        self.assertNotIn("<script", page)
        self.assertNotIn("Dana", page)
        self.assertNotIn("—", page)
        self.assertIn("<title>Lesson one</title>", page)
        self.assertIn('href="/r/files/second.html"', page)
        self.assertIn('href="/r/x/study"', page)                         # a way back to the study, and the old link rewritten
        self.assertIn("refused leaky.md", r.stdout)
        self.assertIn("email address", r.stdout)
        self.assertIn("local path", r.stdout)
        self.assertIn("refused data.zip", r.stdout)

    def test_entry_text_with_a_hit_hides_the_entry_and_its_files(self):
        r = rules()
        r["entries"][0]["scrub"] = []                                    # the name stays in the question
        got = self.run_tool(r)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.manifest()["experiments"], [])
        self.assertEqual(os.listdir(os.path.join(self.out, "reviewer", "files")), [])
        self.assertIn("REFUSED study", got.stdout)

    def test_a_link_to_something_reviewers_cannot_open_refuses_the_file(self):
        r = rules()
        r["entries"][0]["files"][1]["mark"] = False                      # second.html no longer shown; lesson.html links to it
        got = self.run_tool(r)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual([a["file"] for a in self.manifest()["experiments"][0]["attachments"]], [])
        self.assertIn("owner-only link", got.stdout)

    def test_wrong_rules_stop_the_build_and_keep_the_previous_one(self):
        self.assertEqual(self.run_tool(rules()).returncode, 0)
        before = self.read("experiments.json")
        bad = []
        r = rules(); r["entries"][0]["files"][0]["scrub"] = [["not in the file", ""]]; bad.append(("scrub matched 0", r))
        r = rules(); r["entries"][0]["files"][0]["scrub"] = [["<", "", 1]]; bad.append(("scrub matched", r))
        r = rules(); r["entries"].append({"slug": "mit-application", "mark": True}); bad.append(("never be marked", r))
        r = rules(); r["entries"].append({"slug": "other", "as": "mit-application", "mark": True}); bad.append(("never be marked", r))
        r = rules(); r["entries"][0]["files"].append({"file": "mit_notes.md", "mark": True}); bad.append(("never be marked", r))
        r = rules(); r["entries"][0]["files"].append({"file": "plan.md", "mark": True}); bad.append(("never be marked", r))
        r = rules(); r["entries"][0]["files"].append({"file": "other.md", "mark": True}); bad.append(("not an attachment", r))
        r = rules(); r["entries"][0]["files"].append({"file": "../experiments.json", "mark": True}); bad.append(("bad file name", r))
        r = rules(); r["entries"].append({"slug": "nope", "mark": True}); bad.append(("no such entry", r))
        r = rules(); r["block"] = {"broken": "("}; bad.append(("does not compile", r))
        r = rules(); r["site"] = {"title": "For Dana"}; bad.append(("site header", r))
        r = rules(); r["external_check"] = [sys.executable, "-c", "import sys; print('RESULT: 3 HITS'); sys.exit(1)"]; bad.append(("external leak check", r))
        for want, r in bad:
            got = self.run_tool(r)
            self.assertEqual(got.returncode, 1, want)
            self.assertIn("FAIL", got.stderr, want)
            self.assertIn(want, got.stderr, want)
            self.assertEqual(self.read("experiments.json"), before, want)
        self.assertEqual([d for d in os.listdir(self.dir) if ".building-" in d], [])

    def test_an_allowance_covers_only_its_own_words(self):
        r = rules(allow=[{"text": "someone@example.com", "why": "a made-up address in a test"}])
        got = self.run_tool(r)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("refused leaky.md", got.stdout)                    # the path beside the allowed address still refuses it
        self.assertIn("allowed on purpose", got.stdout)
        self.assertNotIn("email address", got.stdout)

    def test_the_app_serves_exactly_what_was_built(self):
        self.assertEqual(self.run_tool(rules(external_check=[sys.executable, "-c", "print('RESULT: CLEAN')"])).returncode, 0)
        data = os.path.join(self.dir, "data")
        shutil.copytree(self.content, data)
        shutil.copytree(os.path.join(self.out, "reviewer"), os.path.join(data, "reviewer"))
        reviewer_auth.write_record(data, "throwaway-test-password-1")
        app.DATA = data
        app._GUARD.update(stamp=None, on=False)
        app.REVIEW_ATTEMPTS = reviewer_auth.Attempts()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def get(path, cookie=None):
                c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
                c.request("GET", path, headers={"Cookie": cookie} if cookie else {})
                resp = c.getresponse()
                out = resp.status, resp.read().decode("utf-8", "replace")
                c.close()
                return out
            cookie = f"{reviewer_auth.COOKIE}={reviewer_auth.sign_session(reviewer_auth.load(data))}"
            code, index = get("/r/", cookie)
            self.assertEqual(code, 200)
            self.assertIn("Does it work?", index)
            self.assertIn(">It works.<", index)
            self.assertIn("single subject (N=1)", index)
            self.assertNotIn("Dana", index)
            code, page = get("/r/files/lesson.html", cookie)
            self.assertEqual(code, 200)
            self.assertIn("Step one.", page)
            self.assertNotIn("Dana", page)
            for path in ("/r/files/leaky.md", "/r/files/data.zip", "/r/files/other.md", "/r/x/other", "/r/x/mit-application"):
                self.assertEqual(get(path, cookie)[0], 404, path)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
