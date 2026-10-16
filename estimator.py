"""
Learns how long a download really takes, from every real job, so the
progress bar's "time left" gets better the more the bot is used.

Every finished job is kept as a sample (job_samples.jsonl): what kind of
link it was, how big the result was, how many jobs were running at the
same time, how long fetching (extract + download + convert) and uploading
to Telegram took, and what was predicted. Predictions come from those
samples:

  * fetch time  = a + b * MB, fitted per kind of link (Instagram reel,
    story, YouTube video, ...) with a robust Theil-Sen line, so one stuck
    download doesn't skew it; falls back to the platform, then to a prior;
  * upload time = a + b * MB, fitted over every job (it doesn't depend on
    the platform);
  * a slowdown factor for each number of concurrent jobs.

A job is re-estimated as soon as more is known: when yt-dlp reports the
file size, and again when the files are on disk and only the upload is
left. accuracy() compares predictions with the real durations, for the
admin panel.
"""

import collections
import json
import logging
import os
import re
import statistics
import threading
import time

logger = logging.getLogger(__name__)

SAMPLES_FILE = "job_samples.jsonl"
MAX_SAMPLES = 6000
PER_KEY_WINDOW = 300
GLOBAL_WINDOW = 1500
UPLOAD_WINDOW = 400
MIN_FIT_SAMPLES = 8
MIN_FACTOR_SAMPLES = 15
MIN_SECONDS, MAX_SECONDS = 2.0, 1800.0
MB = 1024 * 1024

# Used until there is enough data (and no prior from the old model).
DEFAULT_SECONDS = {
    "instagram": 8,
    "tiktok": 8,
    "soundcloud": 12,
    "youtube": 25,
    "spotify": 20,
}

_lock = threading.RLock()
_samples = None
_file_lines = 0
_version = 0
_active = 0
_cache = {}


# --- what kind of job is this ---

def job_key(platform: str, url: str = "", quality: str = "") -> str:
    platform = platform or "unknown"
    if quality == "audio" and platform not in ("soundcloud", "spotify"):
        return f"{platform}:audio"
    if platform == "instagram":
        path = (url or "").lower()
        if "/stories/highlights/" in path or "instagram.com/s/" in path:
            return "instagram:highlight"
        if "/stories/" in path:
            return "instagram:story"
        if re.search(r"/(reel|reels|tv)/", path):
            return "instagram:reel"
        if "/p/" in path:
            return "instagram:post"
        return "instagram:other"
    return {
        "youtube": "youtube:video",
        "tiktok": "tiktok:video",
        "soundcloud": "soundcloud:track",
        "spotify": "spotify:track",
    }.get(platform, f"{platform}:media")


# --- storage ---

def _load_locked():
    global _samples, _file_lines
    if _samples is not None:
        return
    _samples = collections.deque(maxlen=MAX_SAMPLES)
    try:
        with open(SAMPLES_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    sample = json.loads(line)
                except ValueError:
                    continue
                if isinstance(sample, dict):
                    _samples.append(sample)
                    _file_lines += 1
    except OSError:
        pass


def _append_locked(sample: dict) -> None:
    global _file_lines, _version
    _samples.append(sample)
    _version += 1
    _cache.clear()
    try:
        if _file_lines >= 2 * MAX_SAMPLES:
            temp_path = f"{SAMPLES_FILE}.{os.getpid()}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                for item in _samples:
                    f.write(json.dumps(item, separators=(",", ":")) + "\n")
            os.replace(temp_path, SAMPLES_FILE)
            _file_lines = len(_samples)
        else:
            with open(SAMPLES_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(sample, separators=(",", ":")) + "\n")
            _file_lines += 1
    except OSError:
        logger.warning("Could not save a job timing sample", exc_info=True)


# --- models ---

def _theil_sen(points):
    """Robust line y = a + b*x: the median of all pairwise slopes, then the
    median intercept. Both are kept >= 0 (time never shrinks with size)."""
    slopes = []
    for i, (x1, y1) in enumerate(points):
        for x2, y2 in points[i + 1:]:
            if abs(x2 - x1) > 0.05:
                slopes.append((y2 - y1) / (x2 - x1))
    slope = max(0.0, statistics.median(slopes)) if len(slopes) >= 3 else 0.0
    intercept = max(0.0, statistics.median(y - slope * x for x, y in points))
    return intercept, slope


def _recent(predicate, window):
    picked = []
    for sample in reversed(_samples):
        if predicate(sample):
            picked.append(sample)
            if len(picked) >= window:
                break
    return picked


def _cached(name, compute):
    if name not in _cache:
        _cache[name] = compute()
    return _cache[name]


def _fetch_fit(key):
    def compute():
        for scope in (key, key.split(":")[0]):
            match = (lambda s, k=scope: s.get("key") == k) if ":" in scope else (
                lambda s, p=scope: str(s.get("key", "")).split(":")[0] == p)
            rows = _recent(lambda s: match(s) and "fetch" in s, PER_KEY_WINDOW)
            if len(rows) >= MIN_FIT_SAMPLES:
                points = [(s.get("mb", 0.0), s["fetch"]) for s in rows]
                return _theil_sen(points) + (statistics.median(p[0] for p in points),)
        return None
    return _cached(("fetch", key), compute)


def _upload_fit():
    def compute():
        rows = _recent(lambda s: "upload" in s, UPLOAD_WINDOW)
        if len(rows) < MIN_FIT_SAMPLES:
            return None
        return _theil_sen([(s.get("mb", 0.0), s["upload"]) for s in rows])
    return _cached("upload", compute)


def _base_total(key, mb):
    fetch, upload = _fetch_fit(key), _upload_fit()
    if fetch is None or upload is None:
        return None
    if mb is None:
        mb = fetch[2]  # typical size for this kind of link
    return fetch[0] + fetch[1] * mb + upload[0] + upload[1] * mb


def _concurrency_factor(concurrent):
    def compute():
        ratios = collections.defaultdict(list)
        for sample in _recent(lambda s: "total" in s and "conc" in s, GLOBAL_WINDOW):
            base = _base_total(sample.get("key", ""), sample.get("mb"))
            if base and base > 0:
                ratios[min(int(sample["conc"]), 4)].append(sample["total"] / base)
        alone = ratios.get(1) or []
        if len(alone) < MIN_FACTOR_SAMPLES:
            return {}
        reference = statistics.median(alone)
        return {
            bucket: min(4.0, max(1.0, statistics.median(values) / reference))
            for bucket, values in ratios.items()
            if bucket > 1 and len(values) >= MIN_FACTOR_SAMPLES and reference > 0
        }
    return _cached("concurrency", compute).get(min(int(concurrent or 1), 4), 1.0)


def _clamp(seconds):
    return min(max(float(seconds), MIN_SECONDS), MAX_SECONDS)


def estimate_total(key, size_bytes=None, concurrent=1, prior=None) -> float:
    """Expected seconds for a whole job (fetch + upload)."""
    with _lock:
        _load_locked()
        base = _base_total(key, size_bytes / MB if size_bytes else None)
        if base is None:
            return _clamp(prior or DEFAULT_SECONDS.get(key.split(":")[0], 12))
        return _clamp(base * _concurrency_factor(concurrent))


def _estimate_fetch(key, mb, concurrent):
    fetch = _fetch_fit(key)
    if fetch is None:
        return None
    return (fetch[0] + fetch[1] * mb) * _concurrency_factor(concurrent)


def _estimate_upload(mb, concurrent):
    upload = _upload_fit()
    if upload is None:
        return None
    return (upload[0] + upload[1] * mb) * _concurrency_factor(concurrent)


# --- one job ---

class Job:
    """Created when a download really starts (after the queue). Ask it for
    the estimate, tell it what becomes known, and call finish() once the
    file is delivered; close() (in a finally) always releases it."""

    def __init__(self, key, prior=None, size_bytes=None):
        global _active
        with _lock:
            _load_locked()
            _active += 1
            self.concurrent = _active
        self.key = key
        self.started = time.monotonic()
        self.known_size = size_bytes
        self.initial = estimate_total(key, size_bytes, self.concurrent, prior)
        self.current = self.initial
        self.refined = None
        self.fetch_seconds = None
        self.size_bytes = None
        self.items = None
        self.media_seconds = None
        self._size_seen = bool(size_bytes)
        self._closed = False

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def on_size(self, size_bytes):
        """yt-dlp reported how big the file is: new total estimate, or None."""
        if self._size_seen or not size_bytes:
            return None
        self._size_seen = True
        mb = size_bytes / MB
        with _lock:
            fetch = _estimate_fetch(self.key, mb, self.concurrent)
            upload = _estimate_upload(mb, self.concurrent)
        if fetch is None or upload is None:
            return None
        elapsed = self.elapsed()
        self.current = _clamp(elapsed + max(fetch - elapsed, 0.5) + upload)
        return self.current

    def fetched(self, size_bytes, items=None, media_seconds=None):
        """The files are on disk; only the upload is left. New total, or None."""
        self.fetch_seconds = self.elapsed()
        self.size_bytes = size_bytes or 0
        self.items = items
        self.media_seconds = media_seconds
        with _lock:
            upload = _estimate_upload(self.size_bytes / MB, self.concurrent)
        if upload is None:
            return None
        self.refined = _clamp(self.fetch_seconds + upload)
        self.current = self.refined
        return self.current

    def finish(self) -> None:
        total = self.elapsed()
        sample = {
            "t": int(time.time()),
            "key": self.key,
            "mb": round((self.size_bytes or self.known_size or 0) / MB, 3),
            "conc": self.concurrent,
            "total": round(total, 2),
            "est0": round(self.initial, 2),
        }
        if self.fetch_seconds is not None:
            sample["fetch"] = round(self.fetch_seconds, 2)
            sample["upload"] = round(max(total - self.fetch_seconds, 0.0), 2)
        if self.refined is not None:
            sample["est1"] = round(self.refined, 2)
        if self.items:
            sample["items"] = self.items
        if self.media_seconds:
            try:
                sample["sec"] = round(float(self.media_seconds), 1)
            except (TypeError, ValueError):
                pass
        with _lock:
            _append_locked(sample)
        self.close()

    def close(self) -> None:
        global _active
        with _lock:
            if not self._closed:
                self._closed = True
                _active = max(0, _active - 1)


# --- accuracy report ---

def accuracy(days=7, offset_days=0) -> dict:
    """Median error of the first estimate (what the user sees at the start)
    and of the refined one, plus the share within +-25%, for jobs that
    finished in the given window."""
    end = time.time() - offset_days * 86400
    start = end - days * 86400
    with _lock:
        _load_locked()
        rows = [s for s in _samples if start <= s.get("t", 0) < end and s.get("total", 0) > 0]

    def errors(field):
        return [abs(s[field] - s["total"]) / s["total"] for s in rows if field in s]

    first, refined = errors("est0"), errors("est1")
    return {
        "count": len(rows),
        "first_median": statistics.median(first) if first else None,
        "first_within": sum(e <= 0.25 for e in first) / len(first) if first else None,
        "refined_median": statistics.median(refined) if refined else None,
    }
