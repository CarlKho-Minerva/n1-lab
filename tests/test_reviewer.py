"""Tests for the reviewer area under /r/ and the optional owner guard.

Everything runs against a tempfile data dir with made-up content; nothing touches sample_data or private/.
    python3 -m unittest discover -s tests -p test_reviewer.py -v
"""
import contextlib
import hashlib
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app                                     # noqa: E402
import reviewer_auth                           # noqa: E402

PASSWORD = "throwaway-test-password-1"
OWNER_SECRET = "OWNER-ONLY-MARKER-7f3a"          # appears only in the owner's originals; must never reach /r/
REVIEW_TEXT = "Reviewer copy of the lesson."

OWNER = {"experiments": [
    {"slug": "shown", "title": "Shown study", "date": "2026-10-04", "status": "done", "tasks": ["gold"],
     "question": f"Owner question {OWNER_SECRET}", "result": f"Owner result {OWNER_SECRET}",
     "attachments": [{"file": "lesson.html", "label": "Lesson"}, {"file": "note.md", "label": "Note"},
                     {"file": "raw.md", "label": "Raw rows"}]},
    {"slug": "hidden", "title": f"Hidden study {OWNER_SECRET}", "date": "2026-10-05", "status": "done", "tasks": [],
     "question": "q", "result": "r", "attachments": [{"file": "hidden.html", "label": "Hidden"}]},
    {"slug": "mit-application", "title": f"Application {OWNER_SECRET}", "date": "2026-11-22", "status": "running",
     "tasks": [], "question": "q", "result": "r",
     "attachments": [{"file": "mit_notes.md", "label": "Review"}, {"file": "plan_v3.md", "label": "Statement"}]},
]}
TASK = {"title": "Gold", "blurb": "b", "intro": "i", "items": [{"id": "a1", "text": f"item {OWNER_SECRET}"}]}


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ReviewerBase(unittest.TestCase):
    """A data dir holding owner content, reviewer content and (unless a test removes it) a reviewer password."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for sub in ("files", "tasks", "reviewer/files"):
            os.makedirs(os.path.join(self.dir, sub))
        self.write("experiments.json", json.dumps(OWNER))
        self.write("tasks/gold.json", json.dumps(TASK))
        for name in ("lesson.html", "hidden.html"):
            self.write(f"files/{name}", f"<html><body><h1>{OWNER_SECRET}</h1><script>fetch('/api/read')</script></body></html>")
        for name in ("note.md", "raw.md", "mit_notes.md", "plan_v3.md"):
            self.write(f"files/{name}", f"# {OWNER_SECRET}\n\nowner original")
        self.rfiles = {"lesson.html": f"<html><body><h1>{REVIEW_TEXT}</h1></body></html>".encode(),
                       "note.md": b"# Reviewer note\n\nscrubbed text",
                       "raw.md": b"# Raw rows, reviewer copy",
                       "hidden.html": b"<html><body>reviewer copy of a hidden file</body></html>",
                       "mit_notes.md": b"# application, reviewer copy",
                       "plan_v3.md": b"# statement, reviewer copy",
                       "stray.html": b"<html><body>in the folder but not in the list</body></html>"}
        for name, data in self.rfiles.items():
            with open(os.path.join(self.dir, "reviewer", "files", name), "wb") as fh:
                fh.write(data)
        self.set_reviewer(self.default_manifest())
        reviewer_auth.write_record(self.dir, PASSWORD)
        app.DATA = self.dir
        app._READS.update(ino=None, offset=0, views={})
        app._GUARD.update(stamp=None, on=False)
        self.now = [1000.0]
        app.REVIEW_ATTEMPTS = reviewer_auth.Attempts(clock=lambda: self.now[0])
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir)

    def att(self, name, marked=True, **kw):
        row = {"file": name, "label": f"Label {name}", "sha256": sha(self.rfiles[name])}
        if marked is not None:
            row["reviewer"] = marked
        row.update(kw)
        return row

    def default_manifest(self):
        return {"site": {"title": "Studies"}, "experiments": [
            {"slug": "shown", "reviewer": True, "n1": True, "title": "Shown study", "date": "2026-10-04", "status": "done",
             "question": "Does the reviewer copy show?", "result": "Yes. And a second sentence.", "result_line": "Yes.",
             "tasks": ["gold"], "body_md": "Body for reviewers.",
             "attachments": [self.att("lesson.html"), self.att("note.md"), self.att("raw.md", marked=False)]},
            {"slug": "newer", "reviewer": True, "title": "Newer teaching set", "date": "2026-11-01", "status": "done",
             "question": "A later one?", "result": "Listed first.", "attachments": []},
            {"slug": "hidden", "title": "Hidden study", "date": "2026-10-05", "status": "done",      # no flag: default hidden
             "question": "q", "result": "r", "attachments": [self.att("hidden.html")]},
        ]}

    def write(self, rel, text):
        with open(os.path.join(self.dir, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def set_reviewer(self, manifest):
        self.write("reviewer/experiments.json", json.dumps(manifest))

    def req(self, method, path, body=None, cookie=None, headers=None, form=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        h = {"Sec-Fetch-Site": "same-origin"}
        if form is not None:
            body, h["Content-Type"] = urllib.parse.urlencode(form), "application/x-www-form-urlencoded"
        elif body is not None:
            body, h["Content-Type"] = json.dumps(body), "application/json"
        if cookie:
            h["Cookie"] = cookie
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        out = r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read().decode("utf-8", "replace")
        c.close()
        return out

    def sign_in(self, password=PASSWORD, **kw):
        return self.req("POST", "/r/login", form={"password": password}, **kw)

    def cookie(self):
        code, h, _ = self.sign_in()
        self.assertEqual(code, 303)
        return h["set-cookie"].split(";")[0]

    def tree(self):
        """Every file under the data dir with its size and hash: the evidence that nothing was written."""
        out = {}
        for dp, _, fn in os.walk(self.dir):
            for f in fn:
                p = os.path.join(dp, f)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, self.dir)] = sha(fh.read())
        return out


class OffByDefault(ReviewerBase):
    PATHS = ("/r", "/r/", "/r/login", "/r/x/shown", "/r/files/lesson.html", "/r/fonts/geist.woff2", "/r/anything")

    def test_no_auth_file_answers_404_everywhere(self):
        os.unlink(reviewer_auth.auth_path(self.dir))
        unknown = self.req("GET", "/no-such-page")
        for path in self.PATHS:
            code, h, body = self.req("GET", path)
            self.assertEqual((code, body), (404, unknown[2]), path)        # the same answer as any unknown path
            self.assertNotIn("set-cookie", h)
            self.assertNotIn("password", body.lower())
        self.assertEqual(self.sign_in()[0], 404)
        self.assertEqual(self.req("POST", "/r/logout")[0], 404)

    def test_unusable_auth_file_is_off_not_open(self):
        good = json.load(open(reviewer_auth.auth_path(self.dir)))
        for bad in ("not json", "{}", json.dumps(dict(good, n=2)), json.dumps(dict(good, hash="00")),
                    json.dumps(dict(good, key="ab")), json.dumps(dict(good, v=2)), json.dumps([1, 2])):
            self.write(reviewer_auth.AUTH_FILE, bad)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.req("GET", "/r/login")[0], 404, bad[:30])
                self.assertEqual(self.sign_in()[0], 404, bad[:30])

    def test_removing_the_file_turns_it_off_without_a_restart(self):
        cookie = self.cookie()
        self.assertEqual(self.req("GET", "/r/", cookie=cookie)[0], 200)
        os.unlink(reviewer_auth.auth_path(self.dir))
        self.assertEqual(self.req("GET", "/r/", cookie=cookie)[0], 404)


class SignIn(ReviewerBase):
    def test_without_a_session_only_the_sign_in_page_shows(self):
        code, h, body = self.req("GET", "/r/login")
        self.assertEqual(code, 200)
        self.assertIn('type="password"', body)
        self.assertIn('method="post" action="/r/login"', body)
        self.assertNotIn("Shown study", body)
        for path in ("/r", "/r/", "/r/x/shown", "/r/x/hidden", "/r/x/nope", "/r/files/lesson.html", "/r/files/nope.html",
                     "/r/t/gold", "/r/export/gold", "/r/whatever"):
            code, h, body = self.req("GET", path)
            self.assertEqual(code, 303, path)
            self.assertTrue(h["location"].startswith("/r/login"), path)
            self.assertEqual(body, "", path)
        # an entry that exists and one that does not are answered alike before sign-in
        self.assertEqual(self.req("GET", "/r/x/hidden")[0], self.req("GET", "/r/x/nope")[0])

    def test_wrong_password(self):
        log = io.StringIO()
        with contextlib.redirect_stderr(log), contextlib.redirect_stdout(log):
            code, h, body = self.sign_in("wrong-password-zzz")
        self.assertEqual(code, 401)
        self.assertNotIn("set-cookie", h)
        self.assertIn("did not match", body)
        self.assertNotIn("wrong-password-zzz", body)
        self.assertNotIn("wrong-password-zzz", log.getvalue())
        self.assertIn("wrong password", log.getvalue())                  # the failure itself is logged, loudly
        self.assertEqual(self.req("GET", "/r/")[0], 303)

    def test_empty_or_malformed_form_is_refused_without_counting(self):
        self.assertEqual(self.sign_in("")[0], 400)
        self.assertEqual(self.req("POST", "/r/login", form={"other": "x"})[0], 400)
        self.assertEqual(self.req("POST", "/r/login", headers={"Content-Length": "0"})[0], 400)
        self.assertEqual(self.req("POST", "/r/login", form={"password": "x" * 5000})[0], 400)   # over the body cap
        self.assertEqual(app.REVIEW_ATTEMPTS.all, [])
        self.assertEqual(self.sign_in()[0], 303)

    def test_right_password_sets_a_scoped_cookie(self):
        log = io.StringIO()
        with contextlib.redirect_stderr(log), contextlib.redirect_stdout(log):
            code, h, body = self.sign_in()
        self.assertEqual((code, h["location"]), (303, "/r/"))
        self.assertNotIn(PASSWORD, log.getvalue())
        parts = [p.strip() for p in h["set-cookie"].split(";")]
        self.assertTrue(parts[0].startswith(reviewer_auth.COOKIE + "=v1."))
        for flag in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/r/", "Max-Age=2592000"):
            self.assertIn(flag, parts)
        self.assertNotIn("Domain", h["set-cookie"])
        self.assertEqual(self.req("GET", "/r/", cookie=parts[0])[0], 200)
        self.assertEqual(self.req("GET", "/r/login", cookie=parts[0])[0], 303)      # already in: straight to the list

    def test_next_only_ever_points_inside_the_reviewer_area(self):
        self.assertEqual(self.req("GET", "/r/x/shown")[1]["location"], "/r/login?next=/r/x/shown")
        self.assertIn('name="next" value="/r/x/shown"', self.req("GET", "/r/login?next=/r/x/shown")[2])
        code, h, _ = self.req("POST", "/r/login", form={"password": PASSWORD, "next": "/r/x/shown"})
        self.assertEqual(h["location"], "/r/x/shown")
        for evil in ("https://example.com/", "//example.com/", "/x/shown", "/r/../x/shown", "/", "/r/x/shown?x=//e", "javascript:alert(1)"):
            self.assertNotIn("next", self.req("GET", "/r/login?next=" + urllib.parse.quote(evil, safe=""))[2].split("<form")[1], evil)
            code, h, _ = self.req("POST", "/r/login", form={"password": PASSWORD, "next": evil})
            self.assertEqual((code, h["location"]), (303, "/r/"), evil)

    def test_cross_site_sign_in_is_refused(self):
        code, h, _ = self.sign_in(headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(code, 403)
        self.assertNotIn("set-cookie", h)

    def test_a_password_in_a_url_is_not_logged(self):
        log = io.StringIO()
        with contextlib.redirect_stderr(log), contextlib.redirect_stdout(log):
            self.req("GET", "/r/login?password=" + PASSWORD)
            self.req("GET", "/r/?password=" + PASSWORD)
        self.assertIn("/r/login", log.getvalue())
        self.assertNotIn(PASSWORD, log.getvalue())

    def test_sign_out_clears_the_cookie(self):
        cookie = self.cookie()
        code, h, _ = self.req("POST", "/r/logout", cookie=cookie)
        self.assertEqual((code, h["location"]), (303, "/r/login"))
        self.assertIn("Max-Age=0", h["set-cookie"])
        self.assertIn("Path=/r/", h["set-cookie"])


class Sessions(ReviewerBase):
    def test_tampered_expired_and_foreign_cookies_are_refused(self):
        rec = reviewer_auth.load(self.dir)
        good = reviewer_auth.sign_session(rec)
        name = reviewer_auth.COOKIE
        self.assertEqual(self.req("GET", "/r/", cookie=f"{name}={good}")[0], 200)
        body, sig = good.rsplit(".", 1)
        flipped = body + "." + ("A" if sig[0] != "A" else "B") + sig[1:]
        longer = body.replace("v1.", "v1.9", 1) + "." + sig                       # a later expiry, old signature
        expired = reviewer_auth.sign_session(rec, now=time.time() - reviewer_auth.SESSION_SECONDS - 5)
        other = reviewer_auth.sign_session(reviewer_auth.validate(reviewer_auth.make_record("another-password-123")))
        for bad in (flipped, longer, expired, other, "", "v1", "x" * 300, good + "x", "v1.0.a.b"):
            self.assertEqual(self.req("GET", "/r/", cookie=f"{name}={bad}")[0], 303, bad[:40])
        # a stale twin beside the live one does not lock the reader out
        self.assertEqual(self.req("GET", "/r/", cookie=f"{name}={expired}; other=1; {name}={good}")[0], 200)

    def test_sessions_last_thirty_days(self):
        rec = reviewer_auth.load(self.dir)
        token = reviewer_auth.sign_session(rec, now=1_000_000)
        self.assertTrue(reviewer_auth.verify_session(rec, token, now=1_000_000 + 30 * 86400 - 1))
        self.assertFalse(reviewer_auth.verify_session(rec, token, now=1_000_000 + 30 * 86400))

    def test_a_new_password_signs_everyone_out(self):
        cookie = self.cookie()
        reviewer_auth.write_record(self.dir, "a-different-password-9")
        self.assertEqual(self.req("GET", "/r/", cookie=cookie)[0], 303)
        self.assertEqual(self.sign_in()[0], 401)
        self.assertEqual(self.sign_in("a-different-password-9")[0], 303)


class Lockout(ReviewerBase):
    def wrong(self, ip):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.sign_in("wrong-password-zzz", headers={"X-Forwarded-For": ip})

    def test_five_wrong_tries_lock_that_client_out_for_a_while(self):
        for _ in range(5):
            self.assertEqual(self.wrong("203.0.113.9")[0], 401)
        code, h, body = self.sign_in(headers={"X-Forwarded-For": "203.0.113.9"})     # even the right password now
        self.assertEqual(code, 429)
        self.assertNotIn("set-cookie", h)
        self.assertGreater(int(h["retry-after"]), 0)
        self.assertIn("Too many wrong tries", body)
        self.assertEqual(self.sign_in(headers={"X-Forwarded-For": "198.51.100.7"})[0], 303)   # someone else is not locked
        self.now[0] += reviewer_auth.Attempts.WINDOW - 5
        self.assertEqual(self.sign_in(headers={"X-Forwarded-For": "203.0.113.9"})[0], 429)
        self.now[0] += 10
        self.assertEqual(self.sign_in(headers={"X-Forwarded-For": "203.0.113.9"})[0], 303)

    def test_a_refused_try_is_not_hashed_or_counted(self):
        for _ in range(5):
            self.wrong("203.0.113.9")
        before = list(app.REVIEW_ATTEMPTS.all)
        for _ in range(20):
            self.assertEqual(self.wrong("203.0.113.9")[0], 429)
        self.assertEqual(app.REVIEW_ATTEMPTS.all, before)

    def test_many_addresses_hit_the_shared_limit(self):
        cap = reviewer_auth.Attempts.MAX_ALL
        for i in range(cap):
            self.assertEqual(self.wrong(f"198.51.{i // 250}.{i % 250 + 1}")[0], 401, i)
        self.assertEqual(self.sign_in(headers={"X-Forwarded-For": "192.0.2.1"})[0], 429)      # a brand-new address
        self.assertLessEqual(len(app.REVIEW_ATTEMPTS.by_client), cap)
        self.now[0] += reviewer_auth.Attempts.WINDOW + 1
        self.assertEqual(self.sign_in(headers={"X-Forwarded-For": "192.0.2.1"})[0], 303)

    def test_client_is_the_proxy_written_address(self):
        key = reviewer_auth.client_key
        self.assertEqual(key("10.0.0.1", "1.1.1.1, 203.0.113.9"), "203.0.113.9")     # right-most: the proxy's own entry
        self.assertEqual(key("10.0.0.1", None), "10.0.0.1")
        self.assertEqual(key("10.0.0.1", "not an address"), "10.0.0.1")
        self.assertEqual(key("10.0.0.1", "2001:db8:1:2:aaaa::1"), key("10.0.0.1", "2001:db8:1:2:bbbb::2"))   # one /64


class WhatAReviewerSees(ReviewerBase):
    def test_marked_entry_is_visible_from_reviewer_copies(self):
        cookie = self.cookie()
        code, h, body = self.req("GET", "/r/", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertIn("Shown study", body)
        self.assertIn("Does the reviewer copy show?", body)
        self.assertIn(">Yes.<", body)                                       # the one-line result, not the long one
        self.assertNotIn("second sentence", body)
        self.assertIn("2026-10-04", body)
        self.assertIn("single subject (N=1)", body)
        self.assertEqual(body.count("single subject (N=1)"), 1)             # only where it applies
        self.assertLess(body.index("Newer teaching set"), body.index("Shown study"))   # newest first
        self.assertIn("2 studies", body)
        code, h, body = self.req("GET", "/r/x/shown", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertIn("Yes. And a second sentence.", body)
        self.assertIn("Body for reviewers.", body)
        self.assertIn('href="/r/files/lesson.html"', body)
        self.assertIn('href="/r/files/note.md"', body)
        self.assertNotIn("raw.md", body)                                    # unmarked attachment of a marked entry
        code, h, body = self.req("GET", "/r/files/lesson.html", cookie=cookie)
        self.assertEqual((code, h["content-type"]), (200, "text/html; charset=utf-8"))
        self.assertIn(REVIEW_TEXT, body)
        code, h, body = self.req("GET", "/r/files/note.md", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertIn("scrubbed text", body)
        self.assertIn('href="/r/x/shown"', body)                            # a way back to the study

    def test_owner_originals_never_reach_a_reviewer(self):
        cookie = self.cookie()
        for path in ("/r/", "/r/x/shown", "/r/x/newer", "/r/files/lesson.html", "/r/files/note.md", "/r/x/hidden",
                     "/r/files/hidden.html", "/r/files/raw.md"):
            code, h, body = self.req("GET", path, cookie=cookie)
            self.assertNotIn(OWNER_SECRET, body, path)
            self.assertNotIn("Hidden study", body, path)

    def test_reviewer_pages_carry_no_script_and_a_strict_policy(self):
        cookie = self.cookie()
        for path in ("/r/login", "/r/", "/r/x/shown", "/r/files/lesson.html", "/r/files/note.md", "/r/x/nope"):
            code, h, body = self.req("GET", path, cookie=None if path == "/r/login" else cookie)
            self.assertNotIn("<script", body.lower(), path)
            self.assertNotIn("/api/", body, path)
            self.assertNotIn("sendBeacon", body, path)
            self.assertIn("default-src 'none'", h["content-security-policy"], path)
            self.assertNotIn("script-src", h["content-security-policy"], path)
            self.assertIn("frame-ancestors 'none'", h["content-security-policy"], path)
            self.assertEqual(h["cache-control"], "no-store", path)
            self.assertEqual(h["x-robots-tag"], "noindex, nofollow", path)
            self.assertEqual(h["x-content-type-options"], "nosniff", path)
            for owner_link in ('href="/"', 'href="/x/', 'href="/files/', 'href="/t/', 'href="/reads', 'url(/fonts/'):
                self.assertNotIn(owner_link, body, (path, owner_link))

    def test_fonts_are_served_inside_the_area(self):
        code, h, _ = self.req("GET", "/r/fonts/geist.woff2")                 # before sign-in: the sign-in page uses it
        self.assertEqual((code, h["content-type"]), (200, "font/woff2"))
        for path in ("/r/fonts/app.py", "/r/fonts/..%2fapp.py", "/r/fonts/../app.py", "/r/fonts/"):
            self.assertIn(self.req("GET", path)[0], (303, 404), path)

    def test_unmarked_entry_answers_exactly_like_a_missing_one(self):
        cookie = self.cookie()
        missing = self.req("GET", "/r/x/nope", cookie=cookie)
        self.assertEqual(missing[0], 404)
        for slug in ("hidden", "mit-application", "shown2", "SHOWN"):
            got = self.req("GET", f"/r/x/{slug}", cookie=cookie)
            self.assertEqual((got[0], got[2]), (missing[0], missing[2]), slug)
        index = self.req("GET", "/r/", cookie=cookie)[2]
        self.assertNotIn("/r/x/hidden", index)

    def test_only_an_explicit_true_marks_an_entry(self):
        cookie = self.cookie()
        for flag in (None, False, "true", 1, "yes", {"on": True}):
            m = self.default_manifest()
            m["experiments"][0]["reviewer"] = flag
            if flag is None:
                del m["experiments"][0]["reviewer"]
            self.set_reviewer(m)
            self.assertEqual(self.req("GET", "/r/x/shown", cookie=cookie)[0], 404, repr(flag))
            self.assertEqual(self.req("GET", "/r/files/lesson.html", cookie=cookie)[0], 404, repr(flag))

    def test_files_must_be_marked_listed_and_unchanged(self):
        cookie = self.cookie()
        missing = self.req("GET", "/r/files/nope.html", cookie=cookie)
        self.assertEqual(missing[0], 404)
        for name in ("raw.md",            # attachment present but not marked
                     "hidden.html",       # marked attachment of an unmarked entry
                     "stray.html",        # in reviewer/files but in no entry
                     "experiments.json", "../experiments.json", "..%2Fexperiments.json", "lesson.html/", "lesson.html%00"):
            got = self.req("GET", f"/r/files/{name}", cookie=cookie)
            self.assertEqual((got[0], got[2]), (missing[0], missing[2]), name)
        # the owner's original copied over the checked reviewer copy: refused, loudly
        shutil.copy(os.path.join(self.dir, "files", "lesson.html"), os.path.join(self.dir, "reviewer", "files", "lesson.html"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = self.req("GET", "/r/files/lesson.html", cookie=cookie)
        self.assertEqual(got[0], 404)
        self.assertNotIn(OWNER_SECRET, got[2])
        self.assertIn("REFUSED", err.getvalue())
        # a link in place of the checked copy is refused even when its target holds the very same bytes:
        # reviewer files are regular files inside the reviewer folder, nothing else
        self.assertEqual(self.req("GET", "/r/files/note.md", cookie=cookie)[0], 200)
        twin = os.path.join(self.dir, "note_twin.md")
        shutil.copy(os.path.join(self.dir, "reviewer", "files", "note.md"), twin)
        os.unlink(os.path.join(self.dir, "reviewer", "files", "note.md"))
        os.symlink(twin, os.path.join(self.dir, "reviewer", "files", "note.md"))
        self.assertEqual(self.req("GET", "/r/files/note.md", cookie=cookie)[0], 404)

    def test_a_missing_or_broken_reviewer_manifest_shows_nothing(self):
        cookie = self.cookie()
        for text in ("not json", "[]", '{"experiments": "x"}', '{"experiments": [1, "a", null]}'):
            self.write("reviewer/experiments.json", text)
            code, _, body = self.req("GET", "/r/", cookie=cookie)
            self.assertEqual(code, 200, text)
            self.assertIn("Nothing has been shared here yet", body, text)
            self.assertEqual(self.req("GET", "/r/x/shown", cookie=cookie)[0], 404, text)
        os.unlink(os.path.join(self.dir, "reviewer", "experiments.json"))
        self.assertEqual(self.req("GET", "/r/x/shown", cookie=cookie)[0], 404)
        # the owner's own manifest dropped into the reviewer folder by mistake marks nothing
        shutil.copy(os.path.join(self.dir, "experiments.json"), os.path.join(self.dir, "reviewer", "experiments.json"))
        code, _, body = self.req("GET", "/r/", cookie=cookie)
        self.assertNotIn(OWNER_SECRET, body)
        self.assertIn("Nothing has been shared here yet", body)


class NeverTheApplication(ReviewerBase):
    def test_mit_application_cannot_be_marked(self):
        m = self.default_manifest()
        m["experiments"].append({"slug": "mit-application", "reviewer": True, "title": "Application, marked by mistake",
                                 "date": "2026-11-22", "status": "running", "question": "q", "result": "r",
                                 "attachments": [self.att("mit_notes.md"), self.att("plan_v3.md")]})
        # its files smuggled under an entry that IS marked
        m["experiments"][0]["attachments"] += [self.att("mit_notes.md"), self.att("plan_v3.md")]
        self.set_reviewer(m)
        cookie = self.cookie()
        missing = self.req("GET", "/r/x/nope", cookie=cookie)
        got = self.req("GET", "/r/x/mit-application", cookie=cookie)
        self.assertEqual((got[0], got[2]), (404, missing[2]))
        index = self.req("GET", "/r/", cookie=cookie)[2]
        self.assertNotIn("mit-application", index)
        self.assertNotIn("Application, marked by mistake", index)
        study = self.req("GET", "/r/x/shown", cookie=cookie)[2]
        self.assertNotIn("mit_notes", study)
        self.assertNotIn("plan_v3", study)
        self.assertEqual(self.req("GET", "/r/files/mit_notes.md", cookie=cookie)[0], 404)       # refused by its name
        self.assertEqual(self.req("GET", "/r/files/plan_v3.md", cookie=cookie)[0], 404)     # refused: the owner entry lists it
        self.assertIn("mit-application", app.REVIEW_NEVER)
        self.assertFalse(app.review_file_ok("mit_anything.html"))
        self.assertFalse(app.review_file_ok("MIT_review.md"))


class ReadOnly(ReviewerBase):
    def test_tasks_are_never_shown(self):
        cookie = self.cookie()
        for path in ("/r/", "/r/x/shown"):
            body = self.req("GET", path, cookie=cookie)[2]
            self.assertNotIn("/t/", body)
            self.assertNotIn("Gold", body)
            self.assertNotIn("task", body.lower())
        for path in ("/r/t/gold", "/r/tasks/gold.json", "/r/files/gold.json"):
            self.assertEqual(self.req("GET", path, cookie=cookie)[0], 404, path)

    def test_no_write_export_or_listing_route_exists_for_a_reviewer(self):
        cookie = self.cookie()
        before = self.tree()
        answer = {"id": "a1", "verdict": "right", "note": "from a reviewer"}
        read = {"v": "view-0001", "seq": 1, "path": "/files/lesson.html", "title": "t", "local": "2026-10-17 21:03",
                "visible_s": 5, "active_s": 5, "max_depth": 0.4, "dwell": [1] * 100, "vw": 390, "vh": 664, "doc_h": 2000, "dpr": 3}
        for path, body in (("/r/api/task/gold/answer", answer), ("/r/api/read", read), ("/r/api/voice/sample", {"x": 1}),
                           ("/r/x/shown", answer), ("/r/files/lesson.html", answer), ("/r/", answer), ("/r/login/x", answer),
                           ("/r/../api/task/gold/answer", answer), ("/r/%2e%2e/api/read", read)):
            code, _, text = self.req("POST", path, body=body, cookie=cookie)
            self.assertEqual(code, 404, path)
        for path in ("/r/export/gold", "/r/export-reads", "/r/reads", "/r/voice", "/r/api/voice/status", "/r/healthz",
                     "/r/files/", "/r/files", "/r/x/", "/r/responses/gold.jsonl", "/r/tasks/gold.json",
                     "/r/reviewer_auth.json", "/r/files/reviewer_auth.json", "/r/experiments.json"):
            code, _, text = self.req("GET", path, cookie=cookie)
            self.assertEqual(code, 404, path)
            self.assertNotIn(OWNER_SECRET, text, path)
        with contextlib.redirect_stderr(io.StringIO()):
            self.sign_in("wrong-password-zzz")
        self.req("POST", "/r/logout", cookie=cookie)
        self.assertEqual(self.tree(), before)                               # a whole reviewer visit wrote nothing to disk
        self.assertFalse(os.path.exists(os.path.join(self.dir, "responses")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "reads")))

    def test_path_tricks_stay_inside_the_reviewer_area(self):
        cookie = self.cookie()
        for path in ("/r/../x/hidden", "/r/%2e%2e/x/hidden", "/r//x/hidden", "/r/x/../../x/hidden", "/r/./x/hidden",
                     "/r/x/hidden/", "/r/x/shown/../hidden", "/r/files/../../files/hidden.html", "/r/..;/x/hidden",
                     "/r/files/..%2f..%2ffiles%2fhidden.html", "/r/%2e%2e/export/gold", "/r/../export/gold", "/r/../",
                     "/r/..", "/r/x/shown%2f..%2f..%2f..%2ft%2fgold", "/r/\\..\\x\\hidden"):
            for c in (cookie, None):
                code, _, body = self.req("GET", path, cookie=c)
                self.assertIn(code, (303, 404), path)
                self.assertNotIn(OWNER_SECRET, body, path)

    def test_head_has_no_body(self):
        cookie = self.cookie()
        code, h, body = self.req("HEAD", "/r/x/shown", cookie=cookie)
        self.assertEqual((code, body), (200, ""))
        self.assertGreater(int(h["content-length"]), 0)


class OwnerPagesUnchanged(ReviewerBase):
    OWNER_GETS = ("/", "/x/shown", "/x/hidden", "/files/lesson.html", "/files/note.md", "/t/gold", "/export/gold",
                  "/export-reads", "/reads", "/healthz", "/fonts/geist.woff2", "/nope")

    def owner_answers(self, **kw):
        return {p: self.req("GET", p, **kw)[::2] for p in self.OWNER_GETS}

    def test_owner_pages_are_the_same_with_the_reviewer_area_on_off_or_signed_in(self):
        on = self.owner_answers()
        signed_in = self.owner_answers(cookie=self.cookie())
        os.unlink(reviewer_auth.auth_path(self.dir))
        shutil.rmtree(os.path.join(self.dir, "reviewer"))
        off = self.owner_answers()
        self.assertEqual(on, off)
        self.assertEqual(signed_in, off)
        self.assertEqual([p for p, (code, _) in off.items() if code != 200], ["/nope"])
        for path, (code, body) in off.items():
            self.assertNotIn('"/r/', body, path)                             # no owner page points at the reviewer area

    def test_owner_pages_still_show_what_they_did(self):
        self.assertIn(OWNER_SECRET, self.req("GET", "/x/shown")[2])
        self.assertIn("/t/gold", self.req("GET", "/")[2])
        self.assertIn("/api/read", self.req("GET", "/files/lesson.html")[2])           # reading log still attached for the owner
        code, _, _ = self.req("POST", "/api/task/gold/answer", body={"id": "a1", "verdict": "right"})
        self.assertEqual(code, 200)
        self.assertIn('"a1"', self.req("GET", "/export/gold")[2])

    def test_owner_guard_refuses_owner_routes_without_the_router_mark(self):
        cookie = self.cookie()
        self.write("owner_guard", "enforce\n")
        owner = {"X-OpenHost-Is-Owner": "true"}
        answer = {"id": "a1", "verdict": "right"}
        read = {"v": "view-0001", "seq": 1, "path": "/files/lesson.html", "title": "t", "local": "2026-10-17 21:03",
                "visible_s": 5, "active_s": 5, "max_depth": 0.4, "dwell": [1] * 100, "vw": 390, "vh": 664, "doc_h": 2000, "dpr": 3}
        before = self.tree()
        with contextlib.redirect_stderr(io.StringIO()):
            for c in (cookie, None):                                         # a reviewer session is worth nothing out here
                for path in ("/", "/x/shown", "/files/lesson.html", "/t/gold", "/export/gold", "/export-reads", "/reads",
                             "/voice", "/api/voice/status", "/fonts/geist.woff2"):
                    code, _, body = self.req("GET", path, cookie=c)
                    self.assertEqual(code, 403, path)
                    self.assertNotIn(OWNER_SECRET, body, path)
                for path, body in (("/api/task/gold/answer", answer), ("/api/read", read), ("/api/voice/sample", {"x": 1})):
                    self.assertEqual(self.req("POST", path, body=body, cookie=c)[0], 403, path)
                self.assertEqual(self.req("GET", "/x/shown", cookie=c, headers={"X-OpenHost-Is-Owner": "false"})[0], 403)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.req("GET", "/healthz")[0], 200)                # the health check stays open
        self.assertEqual(self.req("GET", "/r/", cookie=cookie)[0], 200)      # and so does the reviewer area
        self.assertEqual(self.req("GET", "/x/shown", headers=owner)[0], 200)
        self.assertEqual(self.req("POST", "/api/task/gold/answer", body=answer, headers=owner)[0], 200)
        os.unlink(os.path.join(self.dir, "owner_guard"))                     # undo: remove the file
        self.assertEqual(self.req("GET", "/x/shown")[0], 200)
        self.write("owner_guard", "off\n")
        self.assertEqual(self.req("GET", "/x/shown")[0], 200)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_cli(self, *args, stdin=None, env=None):
        e = {k: v for k, v in os.environ.items() if k != "N1_REVIEWER_PASSWORD"}
        e.update(env or {})
        return subprocess.run([sys.executable, os.path.join(ROOT, "reviewer_auth.py"), *args], input=stdin, env=e,
                              capture_output=True, text=True, timeout=30)

    def test_set_from_stdin_and_from_the_environment(self):
        r = self.run_cli("set", "--data", self.dir, stdin=PASSWORD + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)
        path = reviewer_auth.auth_path(self.dir)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        raw = open(path).read()
        self.assertNotIn(PASSWORD, raw)
        rec = reviewer_auth.load(self.dir)
        self.assertTrue(reviewer_auth.check_password(rec, PASSWORD))
        self.assertFalse(reviewer_auth.check_password(rec, PASSWORD + " "))
        self.assertFalse(reviewer_auth.check_password(rec, PASSWORD + "x" * 400))
        r = self.run_cli("set", env={"OPENHOST_APP_DATA_DIR": self.dir, "N1_REVIEWER_PASSWORD": "from-the-environment-22"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(reviewer_auth.check_password(reviewer_auth.load(self.dir), "from-the-environment-22"))
        self.assertNotEqual(open(path).read(), raw)                          # new salt, new hash, new signing key
        self.assertIn("ON", self.run_cli("status", "--data", self.dir).stdout)

    def test_a_weak_or_missing_password_is_refused_and_nothing_is_written(self):
        for pw in ("short", "", " leading-space-password", "tab\tinside-password"):
            r = self.run_cli("set", "--data", self.dir, stdin=pw + "\n")
            self.assertEqual(r.returncode, 1, pw)
            self.assertIn("NOT SET", r.stderr)
            self.assertEqual(os.listdir(self.dir), [], pw)
        self.assertEqual(self.run_cli("set", "--data", os.path.join(self.dir, "nope"), stdin=PASSWORD).returncode, 1)
        self.assertEqual(self.run_cli("set", PASSWORD, "--data", self.dir).returncode, 2)     # never as an argument
        self.assertEqual(os.listdir(self.dir), [])

    def test_off_parks_the_file_and_never_deletes_it(self):
        self.run_cli("set", "--data", self.dir, stdin=PASSWORD)
        r = self.run_cli("off", "--data", self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        names = os.listdir(self.dir)
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith(reviewer_auth.AUTH_FILE + ".off-"))
        self.assertIsNone(reviewer_auth.load(self.dir))
        self.assertIn("OFF", self.run_cli("status", "--data", self.dir).stdout)


if __name__ == "__main__":
    unittest.main()
