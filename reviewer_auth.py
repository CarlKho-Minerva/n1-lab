#!/usr/bin/env python3
"""Reviewer sign-in for n1-lab: one shared password, kept only as a salted slow hash.

The reviewer area (/r/ in app.py) is OFF unless this file exists in the app's data directory:

    $OPENHOST_APP_DATA_DIR/reviewer_auth.json    {"v": 1, "salt", "hash", "key", "n", "r", "p", "created"}

It never lives in the repo or the image. `hash` is scrypt(password, salt); `key` signs session cookies and is
made new every time the password is set, so setting a password signs every reviewer out. Deleting the file
turns the reviewer area off on the next request, with no restart.

Set or change the password (it is read from the N1_REVIEWER_PASSWORD environment variable or from standard
input, never from the command line, and is never printed):

    printf '%s' "$PASSWORD" | python3 reviewer_auth.py set          # in the app container, DATA from the env
    python3 reviewer_auth.py status                                 # on or off, and when it was set
    python3 reviewer_auth.py off                                    # renames the file away; the area answers 404

Standard library only.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import sys
import threading
import time

AUTH_FILE = "reviewer_auth.json"
COOKIE = "n1_reviewer"
SESSION_SECONDS = 30 * 86400
MIN_PASSWORD = 12                        # characters; the password is meant to come from a password manager
MAX_PASSWORD = 256                       # bytes; bounds the work a login attempt can ask for
# scrypt cost: 2**14 * 8 * 128 bytes = 16 MB of memory and about 50 ms on one laptop core. The app container has
# 128 MB and a tenth of a core, so app.py lets one hash run at a time.
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32
SCRYPT_MAXMEM = 64 * 1024 * 1024


class AuthError(Exception):
    """The auth file or a password is not usable. The message never contains the password."""


def auth_path(data_dir):
    return os.path.join(data_dir, AUTH_FILE)


def _hash(raw, salt):
    return hashlib.scrypt(raw, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN, maxmem=SCRYPT_MAXMEM)


def check_new_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
    if len(password.encode("utf-8")) > MAX_PASSWORD:
        raise AuthError(f"password must be at most {MAX_PASSWORD} bytes")
    if password != password.strip() or any(ord(c) < 32 for c in password):
        raise AuthError("password must not start or end with a space or contain control characters")


def make_record(password):
    check_new_password(password)
    salt = secrets.token_bytes(16)
    return {"v": 1, "salt": salt.hex(), "hash": _hash(password.encode("utf-8"), salt).hex(), "key": secrets.token_bytes(32).hex(),
            "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def validate(rec):
    """A record the app is willing to use, or AuthError. The cost values are pinned, so an edited file cannot
    make a login attempt allocate a gigabyte."""
    try:
        if rec.get("v") != 1:
            raise AuthError("unknown version")
        salt, digest, key = bytes.fromhex(rec["salt"]), bytes.fromhex(rec["hash"]), bytes.fromhex(rec["key"])
        if len(salt) < 16 or len(digest) != SCRYPT_LEN or len(key) < 32:
            raise AuthError("salt, hash or key has the wrong length")
        if (rec["n"], rec["r"], rec["p"]) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            raise AuthError("unexpected scrypt cost values")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AuthError(f"malformed auth file: {type(exc).__name__}") from None
    return {"salt": salt, "hash": digest, "key": key, "created": str(rec.get("created", ""))}


_CACHE = {"stamp": None, "rec": None}
_CACHE_LOCK = threading.Lock()


def load(data_dir):
    """The validated record, or None when the reviewer area is off (no file) or the file is unusable.
    An unusable file is reported once on stderr and treated as off: closed, never open."""
    path = auth_path(data_dir)
    try:
        st = os.stat(path)
    except OSError:
        with _CACHE_LOCK:
            _CACHE.update(stamp=None, rec=None)
        return None
    stamp = (path, st.st_ino, st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        if _CACHE["stamp"] == stamp:
            return _CACHE["rec"]
    rec = None
    try:
        if st.st_size > 4096:
            raise AuthError("file too large")
        with open(path, encoding="utf-8") as fh:
            rec = validate(json.load(fh))
    except (OSError, ValueError, AuthError) as exc:
        print(f"reviewer area OFF: {AUTH_FILE} is not usable ({exc})", file=sys.stderr, flush=True)
    with _CACHE_LOCK:
        _CACHE.update(stamp=stamp, rec=rec)
    return rec


def write_record(data_dir, password):
    """Hash the password and write the auth file atomically with owner-only permissions."""
    rec = make_record(password)
    os.makedirs(data_dir, exist_ok=True)
    path, tmp = auth_path(data_dir), auth_path(data_dir) + f".tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return rec["created"]


def check_password(rec, password):
    """True when the password matches. The slow hash always runs, whatever was sent, and the comparison is
    constant-time. An over-long input is hashed as empty and refused, so it cannot buy extra work."""
    raw = password.encode("utf-8", "replace") if isinstance(password, str) else b""
    fits = len(raw) <= MAX_PASSWORD
    return hmac.compare_digest(_hash(raw if fits else b"", rec["salt"]), rec["hash"]) and fits


# ---------------------------------------------------------------- session cookie
# v1.<expiry epoch>.<random>.<HMAC-SHA256 of the first three parts>. Nothing is stored on the server, so a
# session cannot be revoked on its own: setting a new password (new key) ends them all.

def _sig(key, body):
    return base64.urlsafe_b64encode(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()).rstrip(b"=").decode("ascii")


def sign_session(rec, now=None):
    now = int(time.time() if now is None else now)
    body = f"v1.{now + SESSION_SECONDS}.{secrets.token_urlsafe(9)}"
    return f"{body}.{_sig(rec['key'], body)}"


def verify_session(rec, token, now=None):
    if not isinstance(token, str) or len(token) > 200 or not token.isascii():
        return False
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "v1" or not parts[1].isdigit():
        return False
    good = hmac.compare_digest(_sig(rec["key"], ".".join(parts[:3])), parts[3])
    now = int(time.time() if now is None else now)
    return good and now < int(parts[1]) <= now + SESSION_SECONDS + 60


def session_from_cookie_header(rec, header, now=None):
    """True when any cookie named COOKIE in the header is a live session (a browser may hold a stale twin)."""
    seen = 0
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            seen += 1
            if seen > 4:
                return False
            if verify_session(rec, value, now):
                return True
    return False


def set_cookie_header(token):
    return f"{COOKIE}={token}; Path=/r/; Max-Age={SESSION_SECONDS}; HttpOnly; Secure; SameSite=Lax"


def clear_cookie_header():
    return f"{COOKIE}=; Path=/r/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"


# ---------------------------------------------------------------- lockout

def client_key(peer, forwarded_for):
    """Who is trying: the right-most X-Forwarded-For entry (the one the zone's own proxy wrote) when it is an IP
    address, else the socket peer. IPv6 clients are counted by their /64, since one host owns a whole /64."""
    cand = (forwarded_for or "").split(",")[-1].strip() or str(peer)
    for text in (cand, str(peer)):
        try:
            ip = ipaddress.ip_address(text.split("%")[0])
        except ValueError:
            continue
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.version == 6:
            return str(ipaddress.ip_network((int(ip) >> 64 << 64, 64)))
        return str(ip)
    return "unknown"


class Attempts:
    """Wrong-password counter, in memory only (the reviewer area writes nothing to disk).

    Per client: MAX_CLIENT wrong tries inside WINDOW seconds locks that client out until WINDOW after its last
    wrong try. Across all clients: MAX_ALL wrong tries inside WINDOW locks sign-in for everyone until the oldest
    ages out, which caps a guesser with many addresses at MAX_ALL per WINDOW. While locked, tries are refused
    before the hash runs and are not counted, so the table never holds more than about MAX_ALL clients."""
    WINDOW = 900
    MAX_CLIENT = 5
    MAX_ALL = 60

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.by_client, self.all = {}, []

    def _prune(self, now):
        cut = now - self.WINDOW
        self.all = [t for t in self.all if t > cut]
        for k in [k for k, v in self.by_client.items() if v[-1] <= cut]:
            del self.by_client[k]

    def blocked(self, client):
        """Seconds until this client may try again; 0 when it may try now."""
        with self.lock:
            now = self.clock()
            self._prune(now)
            wait = 0
            mine = [t for t in self.by_client.get(client, []) if t > now - self.WINDOW]
            if len(mine) >= self.MAX_CLIENT:
                wait = mine[-1] + self.WINDOW - now
            if len(self.all) >= self.MAX_ALL:
                wait = max(wait, self.all[0] + self.WINDOW - now)
            return max(0, int(wait + 0.999))

    def fail(self, client):
        with self.lock:
            now = self.clock()
            self._prune(now)
            self.all.append(now)
            self.by_client.setdefault(client, []).append(now)
            del self.by_client[client][:-self.MAX_CLIENT]

    def success(self, client):
        with self.lock:
            self.by_client.pop(client, None)


# ---------------------------------------------------------------- command line

def _read_password():
    """From N1_REVIEWER_PASSWORD, else standard input (one line; a trailing newline is dropped)."""
    pw = os.environ.get("N1_REVIEWER_PASSWORD")
    if pw is None:
        if sys.stdin.isatty():
            import getpass
            pw = getpass.getpass("Reviewer password (not shown): ")
        else:
            pw = sys.stdin.readline()
    return pw.rstrip("\r\n")


def main(argv):
    data = os.environ.get("OPENHOST_APP_DATA_DIR")
    args = list(argv)
    if "--data" in args:
        i = args.index("--data")
        data = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    cmd = args[0] if args else ""
    if cmd not in ("set", "status", "off") or len(args) != 1:
        print("usage: reviewer_auth.py set|status|off [--data DIR]\n"
              "  set reads the password from N1_REVIEWER_PASSWORD or standard input; it is never an argument",
              file=sys.stderr)
        return 2
    if not data or not os.path.isdir(data):
        print(f"FAIL: data directory not found ({data!r}); set OPENHOST_APP_DATA_DIR or pass --data DIR", file=sys.stderr)
        return 1
    path = auth_path(data)
    if cmd == "status":
        rec = load(data)
        if rec:
            print(f"reviewer area ON: password set {rec['created'] or 'at an unknown time'} ({path})")
        elif os.path.exists(path):
            print(f"reviewer area OFF: {path} exists but is not usable")
            return 1
        else:
            print(f"reviewer area OFF: no {path}")
        return 0
    if cmd == "off":
        if not os.path.exists(path):
            print(f"reviewer area already OFF: no {path}")
            return 0
        parked = f"{path}.off-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(path, parked)
        print(f"reviewer area OFF: moved the auth file to {parked}")
        return 0
    try:
        created = write_record(data, _read_password())
    except AuthError as exc:
        print(f"FAIL: NOT SET: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"FAIL: NOT SET: could not write {path}: {exc}", file=sys.stderr)
        return 1
    if load(data) is None:
        print(f"FAIL: wrote {path} but it does not read back as usable", file=sys.stderr)
        return 1
    print(f"reviewer area ON: password hash written {created} to {path} (mode 600). Every earlier reviewer session is signed out.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
