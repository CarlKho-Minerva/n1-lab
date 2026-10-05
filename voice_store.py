"""voice_store - validation, analysis and durable storage for explicitly labelled voice samples.

Collection only. Nothing here trains, verifies, scores or calls a model; the samples are an
archive Carl labels himself, pulled home over `oh app ssh` like every other private artefact.

Layout (all under the app data dir, never in git):
    DATA/voice/                    0700
    DATA/voice/samples/            0700
    DATA/voice/samples/<id>.wav    0600  16 kHz mono signed 16-bit little-endian
    DATA/voice/samples/<id>.json   0600  metadata, written AFTER the wav; a sample exists iff its json exists
    DATA/voice/report.json         optional, pushed from the Mac; surfaced read-only by /api/voice/status

Standard library only. Pure-Python analysis of at most 15 s of audio (240k samples) is well under
a second on the 100 millicore zone, and the sizes are bounded before any byte is decoded.
"""
import array
import base64
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import wave
from io import BytesIO

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2                                   # bytes per sample, signed 16-bit little-endian
MIN_SECONDS = 1.0
MAX_SECONDS = 15.0
MIN_PCM_BYTES = int(MIN_SECONDS * SAMPLE_RATE) * SAMPLE_WIDTH      # 32,000
MAX_PCM_BYTES = int(MAX_SECONDS * SAMPLE_RATE) * SAMPLE_WIDTH      # 480,000
MAX_B64_CHARS = ((MAX_PCM_BYTES + 2) // 3) * 4                      # 640,000
MAX_JSON_BYTES = 750 * 1024
MAX_WAV_BYTES = MAX_PCM_BYTES + 1024                                # header slack; a bigger file is tampered
WAVEFORM_BINS = 200
NOTE_MAX = 500
DEVICE_MAX = 200
CLIP_LEVEL = 32000                                                  # |sample| >= this counts as clipped

LABELS = ("carl_live", "other_live", "carl_replay", "background", "mention")
SPLITS = ("fit", "test")
POLICY = "collection_only"
REQUIRED = ("id", "label", "split", "session", "device", "note", "pcm_b64")
OPTIONAL = ("context_marker_s",)
# Fields that must match byte-for-byte for a repeated id to count as the same sample.
IDENTITY_FIELDS = ("label", "split", "session", "device", "note", "context_marker_s", "sha256")

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
B64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")       # allow \t \n \r only

LOCK = threading.Lock()


class ValidationError(ValueError):
    """The payload is wrong. Nothing was saved."""


class ConflictError(Exception):
    """The id already exists with different bytes or metadata. Nothing was saved."""


# ---------------------------------------------------------------- validation

def _text(req, key, limit, allow_newlines):
    v = req.get(key)
    if not isinstance(v, str):
        raise ValidationError(f"{key} must be a string")
    if len(v) > limit:
        raise ValidationError(f"{key} longer than {limit} characters")
    bad = CONTROL_RE.search(v) or (not allow_newlines and re.search(r"[\n\r]", v))
    if bad:
        raise ValidationError(f"{key} contains control characters")
    return v


def _uuid(req, key):
    v = req.get(key)
    if not isinstance(v, str) or not UUID_RE.match(v):
        raise ValidationError(f"{key} must be a UUID (8-4-4-4-12 hex)")
    return v.lower()


def _enum(req, key, allowed):
    v = req.get(key)
    if not isinstance(v, str) or v not in allowed:
        raise ValidationError(f"{key} must be one of {', '.join(allowed)}")
    return v


def _pcm(req):
    v = req.get("pcm_b64")
    if not isinstance(v, str):
        raise ValidationError("pcm_b64 must be a base64 string")
    if len(v) > MAX_B64_CHARS:                                       # bound BEFORE decoding
        raise ValidationError(f"pcm_b64 longer than {MAX_SECONDS:g} s of 16 kHz mono 16-bit audio")
    if len(v) % 4 or not B64_RE.match(v):
        raise ValidationError("pcm_b64 is not valid base64")
    try:
        pcm = base64.b64decode(v, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"pcm_b64 is not valid base64: {exc}") from None
    if len(pcm) % SAMPLE_WIDTH:
        raise ValidationError("pcm has an odd byte count; expected signed 16-bit samples")
    if not MIN_PCM_BYTES <= len(pcm) <= MAX_PCM_BYTES:
        raise ValidationError(f"clip must be {MIN_SECONDS:g}-{MAX_SECONDS:g} s at 16 kHz mono 16-bit "
                              f"(got {len(pcm) / SAMPLE_WIDTH / SAMPLE_RATE:.2f} s)")
    return pcm


def _marker(req, duration_s):
    if "context_marker_s" not in req or req["context_marker_s"] is None:
        return None
    v = req["context_marker_s"]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValidationError("context_marker_s must be a finite number")
    try:                                                             # a 400-digit JSON int overflows float()
        f = float(v)
    except OverflowError:
        raise ValidationError("context_marker_s must be a finite number") from None
    if not math.isfinite(f):
        raise ValidationError("context_marker_s must be a finite number")
    if not 0 <= f <= duration_s:
        raise ValidationError(f"context_marker_s must be within the clip (0 to {duration_s:.3f} s)")
    return f


def parse_sample(req):
    """Validate the POST body. Returns (metadata without analysis, pcm bytes) or raises ValidationError."""
    if not isinstance(req, dict):
        raise ValidationError("body must be a JSON object")
    missing = [k for k in REQUIRED if k not in req]
    if missing:
        raise ValidationError(f"missing fields: {', '.join(missing)}")
    unknown = sorted(k for k in req if k not in REQUIRED and k not in OPTIONAL)
    if unknown:
        raise ValidationError(f"unknown fields: {', '.join(str(k)[:40] for k in unknown)}")
    meta = {
        "id": _uuid(req, "id"),
        "label": _enum(req, "label", LABELS),
        "split": _enum(req, "split", SPLITS),
        "session": _uuid(req, "session"),
        "device": _text(req, "device", DEVICE_MAX, allow_newlines=False),
        "note": _text(req, "note", NOTE_MAX, allow_newlines=True),
    }
    pcm = _pcm(req)
    meta["context_marker_s"] = _marker(req, len(pcm) / SAMPLE_WIDTH / SAMPLE_RATE)
    return meta, pcm


# ---------------------------------------------------------------- analysis

def analyze(pcm):
    """Waveform min/max in WAVEFORM_BINS bins (normalised to -1..1), RMS, peak, clipping. Pure stdlib."""
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    n = len(samples)
    mins, maxs = [], []
    sq = 0
    clipped = 0
    peak_raw = 0
    for b in range(WAVEFORM_BINS):
        lo, hi = b * n // WAVEFORM_BINS, (b + 1) * n // WAVEFORM_BINS
        if hi <= lo:                                                   # cannot happen for >= 1 s clips; keep shape anyway
            mins.append(0.0)
            maxs.append(0.0)
            continue
        chunk = samples[lo:hi]
        mn, mx = min(chunk), max(chunk)
        peak_raw = max(peak_raw, -mn, mx)
        mins.append(round(mn / 32768, 4))
        maxs.append(round(mx / 32768, 4))
        for s in chunk:
            sq += s * s
            if s >= CLIP_LEVEL or s <= -CLIP_LEVEL:
                clipped += 1
    peak = peak_raw / 32768
    rms = math.sqrt(sq / n) / 32768 if n else 0.0
    return {
        "sample_rate": SAMPLE_RATE, "channels": CHANNELS, "sample_width": SAMPLE_WIDTH,
        "n_samples": n, "duration_s": round(n / SAMPLE_RATE, 4), "bytes": len(pcm),
        "sha256": hashlib.sha256(pcm).hexdigest(),
        "rms": round(rms, 5), "rms_dbfs": round(20 * math.log10(rms), 2) if rms > 0 else None,
        "peak": round(peak, 4),
        "clipping": {"count": clipped, "ratio": round(clipped / n, 6) if n else 0.0, "level": CLIP_LEVEL},
        "waveform": {"bins": WAVEFORM_BINS, "min": mins, "max": maxs},
    }


def wav_bytes(pcm):
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


# ---------------------------------------------------------------- storage

def _write_private(path, data):
    """Atomic + durable: exclusive 0600 temp in the same dir, fsync, rename over, fsync the dir."""
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)                                         # umask-proof
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


class VoiceStore:
    def __init__(self, root):
        self.root = root                                             # DATA/voice
        self.samples_dir = os.path.join(root, "samples")

    def _ensure_dirs(self):
        for d in (self.root, self.samples_dir):
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)

    def _paths(self, sid):
        return os.path.join(self.samples_dir, f"{sid}.wav"), os.path.join(self.samples_dir, f"{sid}.json")

    def save(self, meta, pcm, extra=None):
        """Store one sample. Returns (metadata, created). Same id + same bytes + same metadata is
        idempotent (created=False); same id with anything different raises ConflictError."""
        record = dict(meta)
        record.update(analyze(pcm))
        record.update(extra or {})
        record.update({"policy": POLICY, "trained": False, "verified": False,
                       "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        wav_path, json_path = self._paths(record["id"])
        with LOCK:
            self._ensure_dirs()
            existing = self._read_meta(json_path)
            if existing is not None:
                same = all(existing.get(k) == record.get(k) for k in IDENTITY_FIELDS)
                if same and os.path.isfile(wav_path):
                    return existing, False
                raise ConflictError(f"id {record['id']} already exists with different audio or metadata")
            _write_private(wav_path, wav_bytes(pcm))
            _write_private(json_path, json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        return record, True

    def _read_meta(self, path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise OSError(f"{os.path.basename(path)} unreadable: {exc}") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("id"), str):
            raise OSError(f"{os.path.basename(path)} is not a sample record")
        return doc

    def list_samples(self):
        """All sample records sorted oldest first, plus a list of files that are present but broken (loud)."""
        out, problems = [], []
        try:
            names = sorted(os.listdir(self.samples_dir))
        except FileNotFoundError:
            return out, problems
        for name in names:
            if not name.endswith(".json") or not UUID_RE.match(name[:-5]):
                continue
            try:
                doc = self._read_meta(os.path.join(self.samples_dir, name))
            except OSError as exc:
                problems.append(str(exc))
                continue
            if doc is None or doc["id"] != name[:-5].lower():
                problems.append(f"{name}: id does not match filename")
                continue
            if not os.path.isfile(os.path.join(self.samples_dir, f"{doc['id']}.wav")):
                problems.append(f"{name}: wav file missing")
                continue
            out.append(doc)
        out.sort(key=lambda d: (d.get("created_at", ""), d["id"]))
        return out, problems

    @staticmethod
    def counts(samples):
        by_label = {k: 0 for k in LABELS}
        by_split = {k: 0 for k in SPLITS}
        by_label_split = {f"{lb}/{sp}": 0 for lb in LABELS for sp in SPLITS}
        seconds = 0.0
        for s in samples:
            by_label[s["label"]] = by_label.get(s["label"], 0) + 1
            by_split[s["split"]] = by_split.get(s["split"], 0) + 1
            key = f"{s['label']}/{s['split']}"
            by_label_split[key] = by_label_split.get(key, 0) + 1
            seconds += float(s.get("duration_s") or 0)
        return {"total": len(samples), "by_label": by_label, "by_split": by_split,
                "by_label_split": by_label_split, "seconds": round(seconds, 2)}

    def report(self):
        """Optional read-only report pushed from the Mac. (report, error): absent -> (None, None)."""
        path = os.path.join(self.root, "report.json")
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh), None
        except FileNotFoundError:
            return None, None
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"report.json unreadable: {exc}"

    def status(self):
        samples, problems = self.list_samples()
        report, report_err = self.report()
        doc = {"ok": True, "policy": POLICY, "trained": False, "verified": False,
               "limits": {"min_s": MIN_SECONDS, "max_s": MAX_SECONDS, "sample_rate": SAMPLE_RATE,
                          "max_json_bytes": MAX_JSON_BYTES, "note_max": NOTE_MAX, "labels": list(LABELS),
                          "splits": list(SPLITS)},
               "samples": samples, "counts": self.counts(samples)}
        if report is not None:
            doc["report"] = report
        if report_err:
            doc["report_error"] = report_err
        if problems:
            doc["problems"] = problems
        return doc

    def clip(self, sid):
        """(wav bytes, error). The id is validated against UUID_RE so no path can escape samples_dir."""
        if not isinstance(sid, str) or not UUID_RE.match(sid):
            return None, "bad id"
        wav_path, json_path = self._paths(sid.lower())
        if not os.path.isfile(json_path) or not os.path.isfile(wav_path):
            return None, "no such sample"
        if os.path.getsize(wav_path) > MAX_WAV_BYTES:
            return None, f"{sid}.wav is larger than any valid sample; refusing to serve it"
        with open(wav_path, "rb") as fh:
            return fh.read(MAX_WAV_BYTES), None
