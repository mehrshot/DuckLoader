"""
Platform detection, media extraction, and audio tagging.

Instagram / YouTube / SoundCloud / TikTok are downloaded directly with
yt-dlp. YouTube also has a second, per-video path (see
probe_youtube_qualities / download_youtube_quality) that inspects a specific
video's actual available resolutions and their real file sizes, for the
thumbnail-plus-buttons quality picker.

Instagram has two extra paths yt-dlp doesn't cover on its own:
  * photo posts and photo carousels (yt-dlp only knows about videos), and
  * stories / highlights, including photo stories and "/s/..." highlight
    share links, which are fetched through Instagram's own web API with the
    configured account cookies.

Spotify is different: Spotify's own audio streams are DRM-protected, so
there is no direct "download from Spotify" here. Instead, track metadata
(title, artists, album, cover art, duration) is fetched from Spotify's
official Web API and the matching audio is located on YouTube Music first
(it carries the same label-supplied catalogue, so its "songs" results are
the official studio recordings), then regular YouTube, then SoundCloud.
Every candidate is scored against the Spotify title, artists and duration,
covers/remixes/live versions are penalised, and the downloaded file's real
length is verified before it is accepted — a search result is never taken
just because it happened to come first.
"""

import base64
import contextlib
import difflib
import itertools
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.parse

import requests
import yt_dlp
from yt_dlp.networking import Request as YDLRequest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DOWNLOAD_DIR = "downloads"
_JOB_DIR_PREFIX = "job_"

PLATFORM_NAMES = {
    "instagram": "Instagram",
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "soundcloud": "SoundCloud",
    "spotify": "Spotify",
}

_PLATFORM_DOMAINS = {
    "instagram": ("instagram.com", "instagr.am"),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
    "soundcloud": ("soundcloud.com", "snd.sc"),
    "spotify": ("spotify.com", "spotify.link"),
}

# Any sub-domain (www., m., music., vm., vt., on., open. ...) and an optional
# scheme — users often paste "instagram.com/reel/..." without https://.
_URL_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)*(?:"
    + "|".join(
        re.escape(domain)
        for domains in _PLATFORM_DOMAINS.values()
        for domain in domains
    )
    + r")(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Telegram's classic Bot API caps uploads at 50MB. Running your own Local Bot
# API Server raises that to 2000MB. Set MAX_UPLOAD_MB in .env if that ever
# changes; defaults to a hair under 2GB either way.
MAX_TELEGRAM_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "1990")) * 1024 * 1024

# Anything downloaded smaller than this is treated as a failed/corrupt
# attempt rather than a real file — no legitimate video or song is this
# small. Images get a much lower floor (a real photo can be ~10KB).
MIN_VALID_FILE_BYTES = 20 * 1024
MIN_VALID_IMAGE_BYTES = 1024

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}

# Hard cap on how many items one link may produce (Instagram highlights,
# SoundCloud sets, ...) so a single message can't tie up a download slot
# for an hour.
MAX_PLAYLIST_ENTRIES = int(os.environ.get("MAX_PLAYLIST_ENTRIES", "30"))
MAX_SPOTIFY_TRACKS = int(os.environ.get("SPOTIFY_MAX_TRACKS", "50"))

QUALITY_LADDER = ["best", "720p", "audio"]

# Quality tiers are applied through yt-dlp's format *sorting* ("res:720"),
# not a hard [height<=720] filter: "res" is the smallest dimension, so a
# 1080x1920 portrait Reel correctly counts as 1080p, and if nothing at or
# below the tier exists yt-dlp still picks the closest format instead of
# failing the whole download.
VIDEO_TIER_HEIGHTS = {
    "best": None,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}

# Prefer a ready-made single file (Instagram/TikTok serve these as H.264 +
# AAC, exactly what Telegram wants), and only merge separate DASH streams
# when no single file exists. Picking the highest-resolution DASH stream
# instead meant VP9 for most Reels, and re-encoding VP9 to H.264 made a
# 19MB Reel take ~2 minutes on the server. (The old "best/all" fallback
# downloaded *every* format into one filename when no single file existed,
# which caused the "Unable to rename file" errors.)
VIDEO_SELECTOR = "b/bv*+ba"

# H.264 first, resolution second: a format that needs no re-encoding beats
# a slightly sharper one that does.
def _video_format_sort(height=None) -> list:
    return ["vcodec:h264", f"res:{height}" if height else "res", "acodec:aac"]
AUDIO_SELECTOR = "ba/b"

# Standard resolution tiers offered by the per-video YouTube quality picker.
YOUTUBE_RESOLUTION_TIERS = [2160, 1440, 1080, 720, 480, 360]

# If the richest format list we can get for a video tops out at or below
# this, it's worth a second attempt with a different client.
MIN_ACCEPTABLE_MAX_HEIGHT = 480

YOUTUBE_PROBE_CACHE_TTL = 900  # 15 minutes
YOUTUBE_PROBE_CACHE_MAX = 64

_youtube_probe_cache = {}
_youtube_probe_cache_lock = threading.Lock()
_youtube_probe_semaphore = threading.Semaphore(1)


class FileTooLargeError(Exception):
    pass


class UnsupportedLinkError(Exception):
    """A link to a page that isn't a downloadable media item (an Instagram
    profile, an Instagram audio page, a Spotify artist page, ...). `kind`
    selects the user-facing explanation in bot_features."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


class PreviewOnlyError(Exception):
    """The platform only served a short preview instead of the full track."""


# ---------------------------------------------------------------------------
# Owner alerts for broken authentication
# ---------------------------------------------------------------------------

AUTH_ALERT_COOLDOWN = 6 * 3600

_auth_alert_handler = None
_auth_alert_last = {}
_auth_alert_lock = threading.Lock()


def set_auth_alert_handler(handler) -> None:
    """`handler(platform, detail)` is called (at most once per
    AUTH_ALERT_COOLDOWN per platform) when a platform's login cookies look
    expired, so the owner hears about it right away instead of discovering
    it from the error log days later."""
    global _auth_alert_handler
    _auth_alert_handler = handler


def _report_auth_problem(platform: str, detail: str) -> None:
    logger.warning("%s authentication problem: %s", platform, detail[:300])
    with _auth_alert_lock:
        now = time.monotonic()
        last = _auth_alert_last.get(platform)
        if last is not None and now - last < AUTH_ALERT_COOLDOWN:
            return
        _auth_alert_last[platform] = now
    if _auth_alert_handler:
        try:
            _auth_alert_handler(platform, detail)
        except Exception:
            logger.exception("Auth alert handler failed")


class _YtdlpLogger:
    """Routes yt-dlp's own messages into our logging. Warnings matter here:
    "The provided Instagram account cookies are no longer valid" used to be
    swallowed by no_warnings=True, so an expired session silently turned
    every Instagram request into a logged-out one."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        logger.warning("yt-dlp: %s", msg)
        if "cookies are no longer valid" in msg.lower():
            _report_auth_problem("instagram", msg)

    def error(self, msg):
        # The exception that follows is logged by whoever catches it.
        logger.debug("yt-dlp error: %s", msg)


_YTDLP_LOGGER = _YtdlpLogger()


def _base_opts(**extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": False,
        "logger": _YTDLP_LOGGER,
        "color": "no_color",
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }
    ffmpeg_location = _ffmpeg_location()
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location
    opts.update(extra)
    return opts


# ---------------------------------------------------------------------------
# Link detection
# ---------------------------------------------------------------------------

def extract_url(text: str) -> str | None:
    """The first supported platform URL in arbitrary text, with a scheme."""
    if not text:
        return None
    match = _URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(".,!?)]}>»«،؛")
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _platform_of_url(url: str) -> str | None:
    host = _host(url)
    for platform, domains in _PLATFORM_DOMAINS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return platform
    return None


def detect_platform(text: str):
    """Returns the platform key for the first recognized link in `text`, or None."""
    url = extract_url(text)
    return _platform_of_url(url) if url else None


def _is_youtube_url(url: str) -> bool:
    return _platform_of_url(url) == "youtube"


def _is_instagram_url(url: str) -> bool:
    return _platform_of_url(url) == "instagram"


def _resolve_redirects(url: str) -> str:
    """Follows share/short-link redirects without downloading any media."""
    try:
        response = requests.get(
            url,
            allow_redirects=True,
            timeout=20,
            headers={"User-Agent": USER_AGENT},
            stream=True,
        )
        response.close()
        return response.url or url
    except Exception as e:
        logger.info("Could not resolve redirects for %s: %s", url, e)
        return url


# ---------------------------------------------------------------------------
# Download folders and file helpers
# ---------------------------------------------------------------------------
#
# Every download attempt gets its own folder inside DOWNLOAD_DIR. Two people
# sending the same Reel at the same time (or one person double-tapping) used
# to share "downloads/<id>.mp4": one job's cleanup deleted the other's file
# mid-download, producing "Unable to rename file" and "downloaded file does
# not exist".

def _new_job_dir() -> str:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    return tempfile.mkdtemp(prefix=_JOB_DIR_PREFIX, dir=DOWNLOAD_DIR)


def _remove_tree(path: str | None) -> None:
    if path:
        shutil.rmtree(path, ignore_errors=True)


def _job_dir_of(path: str) -> str | None:
    parent = os.path.dirname(os.path.abspath(path))
    if (
        os.path.basename(parent).startswith(_JOB_DIR_PREFIX)
        and os.path.dirname(parent) == os.path.abspath(DOWNLOAD_DIR)
    ):
        return parent
    return None


def remove_download_files(paths) -> None:
    """Deletes downloaded files and the per-job folders they live in."""
    job_dirs = set()
    for path in paths or []:
        if not path:
            continue
        job_dir = _job_dir_of(path)
        if job_dir:
            job_dirs.add(job_dir)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    for job_dir in job_dirs:
        _remove_tree(job_dir)


def cleanup_stray_downloads() -> None:
    """Removes anything left in DOWNLOAD_DIR from a previous run that crashed
    or errored before its own cleanup ran. Safe to call on every startup."""
    if not os.path.isdir(DOWNLOAD_DIR):
        return
    for name in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError:
            pass


def media_kind(filepath: str) -> str:
    """Classifies a downloaded file so the bot knows whether to send it as a
    video, audio, or photo message."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "photo"


def format_size(num_bytes: int) -> str:
    """Human-readable size using Persian digits, matching the rest of the bot's
    button labels (e.g. '۷۲۰p')."""
    digits = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹.")
    if num_bytes >= 1024 * 1024 * 1024:
        text = f"{num_bytes / 1024 / 1024 / 1024:.1f} گیگابایت"
    else:
        text = f"{round(num_bytes / 1024 / 1024)} مگابایت"
    return text.translate(digits)


def _check_file_size(path: str, minimum: int) -> None:
    size = os.path.getsize(path)
    if size < minimum:
        raise Exception(f"incomplete download: {os.path.basename(path)} ({size} bytes)")
    if size > MAX_TELEGRAM_BYTES:
        raise FileTooLargeError(format_size(size))


def _audio_duration(path: str) -> float | None:
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path)
        if audio is not None and audio.info and audio.info.length:
            return float(audio.info.length)
    except Exception:
        pass
    return None


def make_thumbnail(source: str | None) -> str | None:
    """Downloads (if `source` is a URL) and scales an image into a JPEG that
    Telegram accepts as an audio/video thumbnail (≤320px, ≤200KB). Telegram
    silently ignores thumbnails passed as a URL, so this has to be a file.
    Returns the local path (inside its own job folder) or None."""
    ffmpeg = _ffmpeg_exe("ffmpeg")
    if not source or not ffmpeg:
        return None
    job_dir = _new_job_dir()
    try:
        if re.match(r"https?://", source, re.IGNORECASE):
            raw_path = os.path.join(job_dir, "cover_src")
            response = requests.get(source, timeout=15, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            with open(raw_path, "wb") as fh:
                fh.write(response.content)
        else:
            raw_path = source
        out_path = os.path.join(job_dir, "thumb.jpg")
        subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error", "-i", raw_path,
                "-vf", "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
                "-frames:v", "1", "-q:v", "4", out_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if os.path.exists(out_path) and 0 < os.path.getsize(out_path) <= 200 * 1024:
            return out_path
    except Exception as e:
        logger.info("Thumbnail preparation failed for %s: %s", source, e)
    _remove_tree(job_dir)
    return None


# ---------------------------------------------------------------------------
# External tools
# ---------------------------------------------------------------------------

_FFMPEG_LOCATION_CACHE = None


def _ffmpeg_location():
    """Path to the ffmpeg/ffprobe folder, resolved once and reused.

    Preference order: an explicit FFMPEG_LOCATION in .env, then Python's own
    shutil.which() — "ffmpeg is installed but yt-dlp still can't find it"
    usually means the *process* (e.g. a systemd service) has a different
    PATH than your interactive shell. Resolving it here and handing yt-dlp
    the answer directly sidesteps that mismatch."""
    global _FFMPEG_LOCATION_CACHE
    if _FFMPEG_LOCATION_CACHE is not None:
        return _FFMPEG_LOCATION_CACHE or None

    explicit = (os.environ.get("FFMPEG_LOCATION") or "").strip()
    if explicit:
        binary = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        folder = os.path.dirname(explicit) if os.path.isfile(explicit) else explicit
        if os.path.isfile(os.path.join(folder, binary)):
            logger.info("ffmpeg: using explicit FFMPEG_LOCATION=%s", folder)
            _FFMPEG_LOCATION_CACHE = folder
            return folder
        logger.warning(
            "ffmpeg: FFMPEG_LOCATION=%s does not contain %s — ignoring it and "
            "searching PATH instead. Fix or remove FFMPEG_LOCATION in .env.",
            explicit, binary,
        )

    found = shutil.which("ffmpeg")
    if found:
        folder = os.path.dirname(found)
        logger.info("ffmpeg: auto-detected at %s (this process's PATH)", found)
        _FFMPEG_LOCATION_CACHE = folder
        return folder

    logger.warning(
        "ffmpeg: shutil.which('ffmpeg') found nothing in this process's PATH (%s). "
        "If `which ffmpeg` over SSH finds it but this log line doesn't, the process "
        "(systemd service) has a different PATH than your shell. Fix: set "
        "FFMPEG_LOCATION in .env to the folder containing the ffmpeg binary.",
        os.environ.get("PATH", "<unset>"),
    )
    _FFMPEG_LOCATION_CACHE = ""
    return None


def _ffmpeg_exe(name: str) -> str | None:
    location = _ffmpeg_location()
    if location:
        candidate = os.path.join(location, name + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(candidate):
            return candidate
    return shutil.which(name)


def _ytmusic_available() -> bool:
    try:
        import ytmusicapi  # noqa: F401
        return True
    except ImportError:
        return False


def check_dependencies() -> None:
    """Logs the state of external dependencies once at startup, so problems
    show up immediately in `journalctl` instead of only surfacing as a
    confusing error the first time someone tries to download something."""
    _ffmpeg_location()  # logs its own found/not-found line

    if any(shutil.which(rt) for rt in ("deno", "node", "bun", "qjs")):
        logger.info("YouTube JS challenge solver: an external JS runtime is available.")
    else:
        logger.warning(
            "YouTube JS challenge solver: no external JS runtime found (deno/node/bun/qjs). "
            "Without one, downloads can fail with 'HTTP Error 403: Forbidden'. "
            "Install Deno: curl -fsSL https://deno.land/install.sh | sh "
            "then run: pip install -U yt-dlp yt-dlp-ejs"
        )

    if _ytmusic_available():
        logger.info("Spotify matching: YouTube Music search is available.")
    else:
        logger.warning(
            "Spotify matching: ytmusicapi is not installed, so Spotify tracks are matched "
            "with plain YouTube/SoundCloud search only (much less accurate). "
            "Fix: pip install -U ytmusicapi"
        )

    sources = [label for label, _ in _instagram_auth_sources() if label != "anonymous"]
    if not sources:
        logger.warning(
            "Instagram authentication: none configured. Stories, photo posts of "
            "restricted accounts and age-restricted Reels need a logged-in session — "
            "set INSTAGRAM_COOKIE_FILE (or INSTAGRAM_COOKIES_BROWSER) in .env."
        )
    else:
        logger.info("Instagram authentication sources: %s", ", ".join(sources))
        cookiefile = _cookie_file_from_env("INSTAGRAM_COOKIE_FILE", "instagram_cookies.txt")
        if cookiefile and not _cookie_file_has(cookiefile, "sessionid", "instagram.com"):
            logger.warning(
                "Instagram cookie file %s has no 'sessionid' cookie — it is not a "
                "logged-in session. Export it again while logged in.",
                cookiefile,
            )


def _get_random_proxy():
    """ROTATING_PROXIES in .env — one or more proxy URLs (comma-separated).
    Only used for lightweight search/probe calls and as a last-resort retry
    for region-blocked TikTok posts — never for the main file transfer of a
    normal download, because a rotating exit IP mid-transfer can corrupt it."""
    proxies_env = os.environ.get("ROTATING_PROXIES")
    if proxies_env:
        proxy_list = [p.strip() for p in proxies_env.split(",") if p.strip()]
        if proxy_list:
            return random.choice(proxy_list)
    return None


# ---------------------------------------------------------------------------
# Cookies and per-platform attempt plans
# ---------------------------------------------------------------------------

def _cookie_file_from_env(var: str, default: str = "") -> str | None:
    path = (os.environ.get(var) or default or "").strip()
    if not path:
        return None
    path = os.path.abspath(os.path.expanduser(path))
    return path if os.path.isfile(path) else None


def _cookie_file_has(path: str, name: str, domain: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 7 and domain in parts[0] and parts[5] == name and parts[6]:
                    return True
    except OSError:
        pass
    return False


def _browser_cookie_spec(env_prefix: str):
    browser = os.environ.get(f"{env_prefix}_COOKIES_BROWSER", "").strip()
    if not browser:
        return None
    profile = os.environ.get(f"{env_prefix}_COOKIES_PROFILE", "").strip()
    return (browser, profile) if profile else (browser,)


def _instagram_auth_sources() -> list:
    """Every configured Instagram login source, tried in order. If the first
    one's session has expired, the next one gets a chance before the request
    falls back to a logged-out attempt."""
    sources = []
    browser = _browser_cookie_spec("INSTAGRAM")
    if browser:
        sources.append(("browser", {"cookiesfrombrowser": browser}))
    cookiefile = _cookie_file_from_env("INSTAGRAM_COOKIE_FILE", "instagram_cookies.txt")
    if cookiefile:
        sources.append(("cookie-file", {"cookiefile": cookiefile, "_readonly_cookiefile": True}))
    if not sources:
        sources.append(("anonymous", {}))
    return sources


def _youtube_cookie_opts() -> dict:
    cookiefile = _cookie_file_from_env("YOUTUBE_COOKIE_FILE", "cookies.txt")
    if cookiefile:
        return {"cookiefile": cookiefile}
    browser = _browser_cookie_spec("YTDLP")
    if browser:
        return {"cookiesfrombrowser": browser}
    return {}


def _tiktok_cookie_opts() -> dict:
    browser = _browser_cookie_spec("TIKTOK")
    if browser:
        return {"cookiesfrombrowser": browser}
    cookiefile = _cookie_file_from_env("TIKTOK_COOKIE_FILE", "cookies.txt")
    if cookiefile:
        # Read-only copy: yt-dlp writes its cookie jar back on exit, and a
        # TikTok run must never overwrite the YouTube cookies in cookies.txt.
        return {"cookiefile": cookiefile, "_readonly_cookiefile": True}
    return {}


# YouTube's bot/token checks are an unstable target, so extraction tries
# several player clients in turn. Override with YTDLP_PLAYER_CLIENT in .env
# (comma-separated — that becomes a single attempt using all of them).
PLAYER_CLIENT_ATTEMPTS = [
    ["default", "web_embedded"],
    ["web_safari"],
    ["tv"],
    ["mweb"],
]
YOUTUBE_PUBLIC_DOWNLOAD_CLIENTS = [["default"], ["web_safari"], ["tv"], ["mweb"]]
YOUTUBE_AUTH_DOWNLOAD_CLIENTS = [["default", "web_safari"], ["tv"]]

# Substrings that mean "worth retrying with a different client/identity"
# rather than a real failure (private, deleted, ...) that retrying won't fix.
_YOUTUBE_RETRY_HINTS = (
    "reload", "sign in", "not a bot", "confirm you", "unavailable",
    "incomplete download", "403", "forbidden", "requested format is not available",
    "http error 5", "timed out", "po token",
)
_YOUTUBE_BOT_CHECK_HINTS = ("sign in to confirm", "not a bot", "confirm you")
_INSTAGRAM_RETRY_HINTS = (
    "log in", "login", "cookies", "empty media response", "unreachable",
    "rate-limit", "not available to everyone", "certain audiences", "401", "429",
    "media info not found",
    # "Media not found or unavailable": another configured account (e.g.
    # the browser session) may still be allowed to see the post.
    "http error 400",
)
_TIKTOK_RETRY_HINTS = (
    "ip address is blocked", "log in", "login", "unable to extract", "403",
    "timed out", "not available in your", "geo",
)


def _client_attempts():
    override = os.environ.get("YTDLP_PLAYER_CLIENT")
    if override:
        return [[c.strip() for c in override.split(",") if c.strip()]]
    return PLAYER_CLIENT_ATTEMPTS


def _youtube_plan(clients, use_cookies: bool) -> dict:
    extra = {"extractor_args": {"youtube": {"player_client": list(clients)}}}
    if use_cookies:
        extra.update(_youtube_cookie_opts())
    return {"label": f"youtube:{'+'.join(clients)}{':cookies' if use_cookies else ''}", "opts": extra, "auth": use_cookies}


def _youtube_download_plans() -> list:
    override = os.environ.get("YTDLP_PLAYER_CLIENT")
    public = _client_attempts() if override else YOUTUBE_PUBLIC_DOWNLOAD_CLIENTS
    plans = [_youtube_plan(clients, False) for clients in public]
    if _youtube_cookie_opts():
        auth = _client_attempts() if override else YOUTUBE_AUTH_DOWNLOAD_CLIENTS
        plans += [_youtube_plan(clients, True) for clients in auth]
    return plans


def _default_plans(url: str) -> list:
    platform = _platform_of_url(url)
    if platform == "youtube":
        return [_youtube_plan(clients, True) for clients in _client_attempts()]
    if platform == "instagram":
        return [
            {"label": f"instagram:{label}", "opts": opts, "auth": label != "anonymous"}
            for label, opts in _instagram_auth_sources()
        ]
    if platform == "tiktok":
        plans = [{"label": "tiktok", "opts": _tiktok_cookie_opts(), "auth": False}]
        proxy = _get_random_proxy()
        if proxy:
            plans.append({"label": "tiktok:proxy", "opts": {**_tiktok_cookie_opts(), "proxy": proxy}, "auth": False})
        return plans
    return [{"label": platform or "generic", "opts": {}, "auth": False}]


def _is_retryable(url: str, error: Exception) -> bool:
    text = str(error).lower()
    platform = _platform_of_url(url)
    hints = {
        "youtube": _YOUTUBE_RETRY_HINTS,
        "instagram": _INSTAGRAM_RETRY_HINTS,
        "tiktok": _TIKTOK_RETRY_HINTS,
    }.get(platform, ())
    return any(hint in text for hint in hints)


@contextlib.contextmanager
def _open_ydl(opts: dict):
    """A YoutubeDL instance for `opts`. Cookie files flagged read-only are
    copied first: yt-dlp saves its cookie jar back on exit, and when an
    Instagram session is rejected it *removes* the sessionid from the jar —
    saving that back used to wipe the login out of the cookie file for good
    after a single hiccup."""
    opts = dict(opts)
    temp_cookie = None
    if opts.pop("_readonly_cookiefile", False) and opts.get("cookiefile"):
        fd, temp_cookie = tempfile.mkstemp(prefix="cookies_", suffix=".txt")
        os.close(fd)
        shutil.copyfile(opts["cookiefile"], temp_cookie)
        opts["cookiefile"] = temp_cookie
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            yield ydl
    finally:
        if temp_cookie:
            try:
                os.remove(temp_cookie)
            except OSError:
                pass


def _run_plans(url: str, plans: list, attempt) -> object:
    """Runs `attempt(plan)` for each plan until one succeeds. Non-retryable
    errors stop immediately; a YouTube bot check skips the remaining
    cookie-less plans and goes straight to the authenticated ones."""
    last_error = None
    skip_public = False
    for index, plan in enumerate(plans, start=1):
        if skip_public and not plan.get("auth"):
            continue
        started = time.monotonic()
        try:
            result = attempt(plan)
            logger.info(
                "Attempt %d/%d succeeded | %s | %.1fs | %s",
                index, len(plans), plan["label"], time.monotonic() - started, url,
            )
            return result
        except (FileTooLargeError, UnsupportedLinkError):
            raise
        except Exception as e:
            last_error = e
            elapsed = time.monotonic() - started
            if index < len(plans) and _is_retryable(url, e):
                logger.warning(
                    "Attempt %d/%d failed; retrying | %s | %.1fs | %s",
                    index, len(plans), plan["label"], elapsed, str(e)[:400],
                )
                if not plan.get("auth") and any(h in str(e).lower() for h in _YOUTUBE_BOT_CHECK_HINTS):
                    skip_public = True
                continue
            logger.error(
                "Attempt %d/%d failed permanently | %s | %.1fs | %s",
                index, len(plans), plan["label"], elapsed, str(e)[:400],
            )
            raise
    raise last_error or Exception("Download failed.")


def _extract_resilient(base_opts: dict, target: str, plans: list | None = None, process: bool = False):
    """Metadata-only extraction with the platform's retry plans."""

    def attempt(plan):
        with _open_ydl({**base_opts, **plan["opts"]}) as ydl:
            info = ydl.extract_info(target, download=False, process=process)
        if not info:
            raise Exception("Media info not found.")
        return info

    return _run_plans(target, plans or _default_plans(target), attempt)


# ---------------------------------------------------------------------------
# MP4 preparation for Telegram
# ---------------------------------------------------------------------------

def _ensure_h264_mp4(filepath: str) -> str:
    """
    Prepare an MP4 for Telegram/iPhone streaming:

    1. H.264 + AAC       -> stream-copy, faststart only.
    2. H.264 + other     -> copy video, convert audio to AAC.
    3. anything else     -> H.264 + AAC with faststart in the same pass.
    """
    if not filepath or not os.path.exists(filepath) or not filepath.lower().endswith(".mp4"):
        return filepath

    ffmpeg_exe = _ffmpeg_exe("ffmpeg")
    ffprobe_exe = _ffmpeg_exe("ffprobe")
    if not ffmpeg_exe or not ffprobe_exe:
        logger.warning("ffmpeg/ffprobe not found; MP4 preparation skipped: %s", filepath)
        return filepath

    try:
        result = subprocess.run(
            [ffprobe_exe, "-v", "error", "-print_format", "json", "-show_streams", filepath],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except Exception as e:
        logger.warning("Could not inspect MP4 codecs for %s: %s", filepath, e)
        return filepath

    video_codec = next((s.get("codec_name") for s in streams if s.get("codec_type") == "video"), "") or ""
    audio_codec = next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), "") or ""
    if not video_codec:
        return filepath

    video_is_h264 = video_codec.lower() == "h264"
    audio_is_aac = audio_codec.lower() in {"aac", "mp4a", ""}

    temp_path = filepath + ".telegram.mp4"
    command = [ffmpeg_exe, "-y", "-loglevel", "error", "-i", filepath, "-map", "0:v:0", "-map", "0:a:0?"]
    if video_is_h264 and audio_is_aac:
        command += ["-c", "copy"]
    elif video_is_h264:
        command += ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
    else:
        logger.info("H.264 transcode required for %s (source codec: %s)", filepath, video_codec)
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-threads", "0", "-c:a", "aac", "-b:a", "160k",
        ]
    command += ["-movflags", "+faststart", temp_path]

    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.replace(temp_path, filepath)
    except Exception as e:
        logger.warning("MP4 Telegram preparation failed for %s: %s", filepath, e)
        try:
            os.remove(temp_path)
        except OSError:
            pass
    return filepath


# ---------------------------------------------------------------------------
# Generic yt-dlp download
# ---------------------------------------------------------------------------

_ENTRY_METADATA_KEYS = (
    "id", "title", "description", "thumbnail", "timestamp", "upload_date",
    "uploader", "uploader_id", "channel", "channel_id", "view_count",
    "like_count", "comment_count", "duration", "width", "height",
    "webpage_url", "extractor", "extractor_key", "artist", "track", "album",
)


def _is_photo_entry(url: str, info_raw: dict, entry: dict) -> bool:
    """True only for Instagram stills: entries with no formats at all, no
    video duration, and a thumbnail to download. Scoped to Instagram on
    purpose — a real download failure elsewhere must never be quietly
    swapped for a thumbnail."""
    extractor = f"{entry.get('extractor_key') or ''} {info_raw.get('extractor_key') or ''}".lower()
    if "instagram" not in extractor:
        return False
    if entry.get("formats") or entry.get("vcodec") not in (None, "none"):
        return False
    if entry.get("duration"):
        return False
    if re.search(r"instagram\.com/(?:[^/]+/)?(?:reels?|tv)/", url, re.IGNORECASE):
        return False
    return bool(entry.get("url") or entry.get("thumbnails") or entry.get("thumbnail"))


def _best_image_url(entry: dict) -> str | None:
    thumbnails = [t for t in (entry.get("thumbnails") or []) if t.get("url")]
    if thumbnails:
        best = max(
            enumerate(thumbnails),
            key=lambda item: ((item[1].get("width") or 0) * (item[1].get("height") or 0), item[0]),
        )[1]
        return best["url"]
    return entry.get("url") or entry.get("thumbnail")


def _image_extension(url: str, content_type: str = "") -> str:
    content_type = (content_type or "").lower()
    if "png" in content_type:
        return "png"
    if "webp" in content_type:
        return "webp"
    if "jpeg" in content_type or "jpg" in content_type:
        return "jpg"
    path = urllib.parse.urlparse(url).path.lower()
    for ext in ("png", "webp", "jpg", "jpeg"):
        if path.endswith("." + ext):
            return "jpg" if ext == "jpeg" else ext
    return "jpg"


def _ydl_fetch_to_file(ydl, url: str, path_without_ext: str, referer: str) -> str:
    """Downloads a direct media URL through yt-dlp's networking stack (same
    cookies/proxy/impersonation as the extraction) and returns the path."""
    request = YDLRequest(url, headers={"Referer": referer, "User-Agent": USER_AGENT})
    with ydl.urlopen(request) as response:
        content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("video/"):
            ext = "mp4"
        else:
            ext = _image_extension(url, content_type)
        path = f"{path_without_ext}.{ext}"
        with open(path, "wb") as fh:
            shutil.copyfileobj(response, fh, 1 << 16)
    if ext == "webp":
        path = _convert_image_to_jpg(path)
    return path


def _convert_image_to_jpg(path: str) -> str:
    """Telegram handles JPEG/PNG photos best; WebP stills are converted."""
    ffmpeg = _ffmpeg_exe("ffmpeg")
    if not ffmpeg:
        return path
    jpg_path = os.path.splitext(path)[0] + ".jpg"
    try:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", path, "-q:v", "2", jpg_path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60,
        )
        if os.path.getsize(jpg_path) > 0:
            os.remove(path)
            return jpg_path
    except Exception as e:
        logger.info("Image conversion failed for %s: %s", path, e)
    return path


def _find_downloaded_file(processed: dict, job_dir: str, entry_id, extract_audio: bool) -> str | None:
    candidates = []
    for download in reversed(processed.get("requested_downloads") or []):
        candidates.append(download.get("filepath") or download.get("_filename"))
    candidates.append(processed.get("filepath"))
    candidates.append(processed.get("_filename"))
    for path in candidates:
        if path and os.path.isfile(path):
            if extract_audio and os.path.splitext(path)[1].lower() not in AUDIO_EXTENSIONS:
                mp3 = os.path.splitext(path)[0] + ".mp3"
                if os.path.isfile(mp3):
                    return mp3
            return path

    wanted = AUDIO_EXTENSIONS if extract_audio else VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
    found = []
    for name in os.listdir(job_dir):
        ext = os.path.splitext(name)[1].lower()
        if ext in wanted and not name.endswith((".part", ".ytdl", ".telegram.mp4")):
            path = os.path.join(job_dir, name)
            found.append((str(name).startswith(f"{entry_id}."), os.path.getsize(path), path))
    return max(found)[2] if found else None


def _download_with_selector(
    url: str,
    format_selector: str,
    extract_audio: bool,
    progress_hook=None,
    *,
    plans: list | None = None,
    format_sort: list | None = None,
    wanted_ids=None,
    noplaylist: bool | None = None,
):
    """
    Download media with yt-dlp into a fresh per-job folder.

    Video: best matching format, merged to MP4 and prepared for Telegram.
    Audio: converted to MP3.
    Instagram photo entries (posts, carousel items) are saved as images.

    Returns (info, entries, filepaths).
    """
    platform = _platform_of_url(url)
    is_instagram = platform == "instagram"
    is_story = bool(re.search(r"instagram\.com/stories/", url, re.IGNORECASE))

    ydl_opts = _base_opts(
        format=format_selector,
        merge_output_format="mp4",
        noplaylist=is_story if noplaylist is None else noplaylist,
        # Instagram photo posts have no formats; without this yt-dlp raises
        # "There is no video in this post" before we ever see the images.
        ignore_no_formats_error=is_instagram,
    )
    if format_sort:
        ydl_opts["format_sort"] = format_sort
    elif platform == "youtube":
        # "lang" must come first. YouTube's auto-dubbed videos carry extra
        # audio tracks in other languages; yt-dlp marks the original track
        # (language_preference 10), the default one (5) and dubs (-1), but
        # our own sort fields are applied before its defaults — with "size"
        # ahead of "lang", whichever dub happened to be the largest file won
        # and users got the video in a random language.
        # Then: prefer H.264/M4A at equal resolution, but don't require it,
        # so 1440p/2160p stay available when only VP9/AV1 exist there.
        ydl_opts["format_sort"] = (
            ["lang"] if extract_audio else ["lang", "res", "fps", "codec:avc:m4a", "size"]
        )
    elif not extract_audio:
        ydl_opts["format_sort"] = _video_format_sort()
    if extract_audio:
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
        ]
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    wanted_ids = {str(i) for i in wanted_ids} if wanted_ids else None

    def attempt(plan):
        job_dir = _new_job_dir()
        try:
            opts = {**ydl_opts, **plan["opts"], "outtmpl": os.path.join(job_dir, "%(id)s.%(ext)s")}
            with _open_ydl(opts) as ydl:
                return _download_entries(ydl, url, job_dir, extract_audio, wanted_ids)
        except BaseException:
            _remove_tree(job_dir)
            raise

    return _run_plans(url, plans or _default_plans(url), attempt)


def _download_entries(ydl, url, job_dir, extract_audio, wanted_ids):
    info_raw = ydl.extract_info(url, download=False, process=False)
    if not info_raw:
        raise Exception("Media info not found.")

    raw_entries = info_raw.get("entries")
    if raw_entries is None:
        raw_entries = [info_raw]
    raw_entries = [e for e in itertools.islice(raw_entries, MAX_PLAYLIST_ENTRIES * 4) if e]
    if wanted_ids:
        raw_entries = [e for e in raw_entries if str(e.get("id")) in wanted_ids]
        if not raw_entries:
            raise Exception("This story item is no longer available (media info not found).")
    raw_entries = raw_entries[:MAX_PLAYLIST_ENTRIES]

    filepaths = []
    entries = []
    skipped_previews = 0

    for index, raw_entry in enumerate(raw_entries, start=1):
        for key in ("extractor", "extractor_key", "webpage_url", "channel", "uploader", "uploader_id"):
            if key not in raw_entry and key in info_raw:
                raw_entry[key] = info_raw[key]

        if _is_photo_entry(url, info_raw, raw_entry):
            if extract_audio:
                continue
            image_url = _best_image_url(raw_entry)
            if not image_url:
                continue
            entry_id = raw_entry.get("id") or f"image{index}"
            path = _ydl_fetch_to_file(
                ydl, image_url, os.path.join(job_dir, f"{entry_id}_{index}"), "https://www.instagram.com/",
            )
            _check_file_size(path, MIN_VALID_IMAGE_BYTES)
            filepaths.append(path)
            entries.append(dict(raw_entry))
            continue

        entry_id = raw_entry.get("id")
        processed = ydl.process_ie_result(raw_entry, download=True)
        if not processed:
            continue
        if processed.get("_type") in ("playlist", "multi_video"):
            # A URL entry that resolved to a nested playlist (e.g. a set).
            raise Exception("Nested playlists are not supported.")
        if not processed.get("formats") and not processed.get("requested_downloads"):
            # ignore_no_formats_error let a video without any formats through.
            raise Exception(
                "There is no downloadable video in this post "
                "(Instagram sent an empty media response; login may be required)."
            )

        for key in _ENTRY_METADATA_KEYS:
            if processed.get(key) is None and raw_entry.get(key) is not None:
                processed[key] = raw_entry[key]
        if processed.get("view_count") is None and raw_entry.get("video_view_count") is not None:
            processed["view_count"] = raw_entry["video_view_count"]

        path = _find_downloaded_file(processed, job_dir, entry_id, extract_audio)
        if not path or not os.path.exists(path):
            raise Exception(f"downloaded file does not exist: {entry_id}")
        if not extract_audio and media_kind(path) == "video":
            path = _ensure_h264_mp4(path)
        _check_file_size(path, MIN_VALID_FILE_BYTES)

        if extract_audio:
            expected = processed.get("duration") or raw_entry.get("duration")
            actual = _audio_duration(path)
            if expected and actual and expected - actual > 15 and actual < expected * 0.8:
                logger.warning(
                    "Only a %.0fs preview was served for a %.0fs track: %s",
                    actual, expected, processed.get("webpage_url") or url,
                )
                os.remove(path)
                skipped_previews += 1
                continue

        metadata = dict(raw_entry)
        metadata.update({k: v for k, v in processed.items() if not k.startswith("_") and k not in ("formats", "requested_downloads", "requested_formats")})
        if "instagram" in str(metadata.get("extractor_key") or metadata.get("extractor") or "").lower():
            _recover_instagram_view_count(ydl, url, metadata, processed, raw_entry, info_raw)

        filepaths.append(path)
        entries.append(metadata)

    if not filepaths:
        if skipped_previews:
            raise PreviewOnlyError("Only a short preview of this track is available.")
        if extract_audio:
            raise Exception("No audio could be downloaded from this link.")
        raise Exception("Nothing could be downloaded from this link.")

    return info_raw, entries, filepaths


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

_IG_APP_ID = "936619743392459"
_IG_SHORTCODE_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_IG_MEDIA_PATHS = {"p", "reel", "reels", "tv"}
_IG_NON_PROFILE_PATHS = {
    "explore", "accounts", "direct", "about", "legal", "developer", "challenge",
    "web", "api", "graphql", "emails", "session", "your_activity", "archive",
}


def _ig_pk_to_shortcode(pk) -> str:
    number = int(str(pk).split("_")[0])
    chars = []
    while number:
        number, rem = divmod(number, 64)
        chars.append(_IG_SHORTCODE_CHARS[rem])
    return "".join(reversed(chars)) or "A"


def _is_instagram_story_url(url: str) -> bool:
    return bool(re.search(r"instagram\.com/stories/[^/?#]+/\d+", url, re.IGNORECASE))


def normalize_instagram_url(url: str):
    """Returns (canonical_url, wanted_story_pk).

    * /s/<base64 "highlight:ID">?story_media_id=PK_UID  (the link the app's
      share button produces for a highlight item) -> the highlight, filtered
      to that single item.
    * /share/...  -> the post it redirects to.
    * /stories/<user>/<pk>, /reel(s)/<code>, /p/<code>, /tv/<code>,
      /<user>/p/<code>  -> a clean URL without tracking parameters.
    * profile pages and /reels/audio/ pages raise UnsupportedLinkError.
    """
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    segments = [s for s in parsed.path.split("/") if s]
    story_media_id = (query.get("story_media_id") or [""])[0].split("_")[0] or None

    if not segments:
        raise UnsupportedLinkError("ig_profile")

    first = segments[0].lower()

    if first == "share":
        resolved = _resolve_redirects(url)
        if resolved != url and "/share/" not in urllib.parse.urlparse(resolved).path:
            return normalize_instagram_url(resolved)
        return url, None

    if first == "s" and len(segments) > 1:
        token = segments[1]
        try:
            decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8", "ignore")
        except (ValueError, UnicodeDecodeError):
            decoded = ""
        match = re.fullmatch(r"highlight:(\d+)", decoded.strip())
        if match:
            return f"https://www.instagram.com/stories/highlights/{match.group(1)}/", story_media_id
        raise UnsupportedLinkError("ig_unsupported")

    if first == "stories" and len(segments) > 1:
        if segments[1].lower() == "highlights" and len(segments) > 2:
            return f"https://www.instagram.com/stories/highlights/{segments[2]}/", story_media_id
        username = segments[1]
        if len(segments) > 2 and segments[2].isdigit():
            return f"https://www.instagram.com/stories/{username}/{segments[2]}/", segments[2]
        return f"https://www.instagram.com/stories/{username}/", None

    if first == "reels" and len(segments) > 1 and segments[1].lower() == "audio":
        raise UnsupportedLinkError("ig_audio")

    if first in _IG_MEDIA_PATHS and len(segments) > 1:
        kind = "reel" if first in ("reel", "reels") else first
        return f"https://www.instagram.com/{kind}/{segments[1]}/", None

    if len(segments) > 2 and segments[1].lower() in _IG_MEDIA_PATHS:
        kind = "reel" if segments[1].lower() in ("reel", "reels") else segments[1].lower()
        return f"https://www.instagram.com/{kind}/{segments[2]}/", None

    if first in _IG_NON_PROFILE_PATHS or first == "reels":
        raise UnsupportedLinkError("ig_unsupported")

    raise UnsupportedLinkError("ig_profile")


def _ig_logged_in(ydl) -> bool:
    try:
        return any(
            cookie.name == "sessionid" and cookie.value and "instagram.com" in (cookie.domain or "")
            for cookie in ydl.cookiejar
        )
    except Exception:
        return False


def _ig_csrf_token(ydl) -> str | None:
    try:
        for cookie in ydl.cookiejar:
            if cookie.name == "csrftoken" and cookie.value and "instagram.com" in (cookie.domain or ""):
                return cookie.value
    except Exception:
        pass
    return None


def _ig_api(ydl, url: str, referer: str, data: bytes | None = None) -> dict:
    headers = {
        "X-IG-App-ID": _IG_APP_ID,
        "X-ASBD-ID": "359341",
        "X-IG-WWW-Claim": "0",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.instagram.com",
        "Referer": referer,
        "Accept": "*/*",
    }
    csrf = _ig_csrf_token(ydl)
    if csrf:
        headers["X-CSRFToken"] = csrf
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = YDLRequest(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        target = ImpersonateTarget()
        if ydl._impersonate_target_available(target):
            request.extensions["impersonate"] = target
    except Exception:
        pass
    with ydl.urlopen(request) as response:
        final_url = getattr(response, "url", "") or ""
        body = response.read()
    if "/accounts/login" in final_url or "/challenge" in final_url:
        raise Exception("Instagram login required: the session was redirected to the login page.")
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise Exception("Instagram returned an unexpected (non-JSON) response; login required?")


def _download_instagram_story_via_api(url: str, wanted_pk, progress_hook=None):
    """Fetches story / highlight items straight from Instagram's web API.
    Unlike yt-dlp's story extractor this also returns *photo* stories, and
    it can pick out the single item a /s/... share link points at."""
    match = re.search(r"instagram\.com/stories/([^/?#]+)/?(\d+)?", url, re.IGNORECASE)
    if not match:
        raise Exception("Not a story URL.")
    username, path_id = match.group(1), match.group(2)

    last_error = None
    for label, auth_opts in _instagram_auth_sources():
        if label == "anonymous":
            break
        job_dir = _new_job_dir()
        try:
            with _open_ydl(_base_opts(**auth_opts)) as ydl:
                if not _ig_logged_in(ydl):
                    raise Exception(f"Instagram login required ({label} has no session cookie).")

                reel = None
                if wanted_pk:
                    # One story item: its media info carries both the media
                    # and its owner — one request instead of two, and it
                    # avoids web_profile_info, which Instagram rate-limits
                    # very aggressively.
                    try:
                        media_info = _ig_api(
                            ydl, f"https://www.instagram.com/api/v1/media/{wanted_pk}/info/", url,
                        )
                        media_items = media_info.get("items") or []
                        if media_items:
                            reel = {"items": media_items, "user": media_items[0].get("user") or {}}
                    except Exception as e:
                        if "429" in str(e):
                            raise  # rate-limited: more requests only make it worse
                        logger.info("Story media info lookup failed, trying the reel feed: %s", str(e)[:200])

                if reel is None:
                    if username.lower() == "highlights":
                        reel_id = f"highlight:{path_id}"
                    else:
                        profile = _ig_api(
                            ydl,
                            f"https://www.instagram.com/api/v1/users/web_profile_info/?username={urllib.parse.quote(username)}",
                            f"https://www.instagram.com/{username}/",
                        )
                        user_id = ((profile.get("data") or {}).get("user") or {}).get("id")
                        if not user_id:
                            raise Exception("Instagram login required: could not resolve the story's account.")
                        reel_id = str(user_id)

                    payload = _ig_api(
                        ydl,
                        f"https://www.instagram.com/api/v1/feed/reels_media/?reel_ids={urllib.parse.quote(reel_id)}",
                        url,
                    )
                    reels = payload.get("reels") or {}
                    reel = reels.get(reel_id) or next(iter(reels.values()), None)
                    if not reel:
                        for item in payload.get("reels_media") or []:
                            reel = item
                            break
                    if not reel:
                        raise Exception("This story is no longer available (you need to log in to access this content?).")

                items = reel.get("items") or []
                if wanted_pk:
                    items = [i for i in items if str(i.get("pk") or str(i.get("id", "")).split("_")[0]) == str(wanted_pk)]
                    if not items:
                        raise Exception("This story item is no longer available (it may have expired).")
                items = items[:MAX_PLAYLIST_ENTRIES]

                user = reel.get("user") or {}
                owner = user.get("username") or (username if username.lower() != "highlights" else "")
                filepaths, entries = [], []
                for index, item in enumerate(items, start=1):
                    pk = str(item.get("pk") or str(item.get("id", "")).split("_")[0])
                    videos = [v for v in (item.get("video_versions") or []) if v.get("url")]
                    images = [c for c in ((item.get("image_versions2") or {}).get("candidates") or []) if c.get("url")]
                    if videos:
                        media_url = max(videos, key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))["url"]
                    elif images:
                        media_url = max(images, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))["url"]
                    else:
                        continue
                    path = _ydl_fetch_to_file(ydl, media_url, os.path.join(job_dir, f"{pk}_{index}"), "https://www.instagram.com/")
                    if media_kind(path) == "video":
                        path = _ensure_h264_mp4(path)
                        _check_file_size(path, MIN_VALID_FILE_BYTES)
                    else:
                        _check_file_size(path, MIN_VALID_IMAGE_BYTES)
                    filepaths.append(path)
                    entries.append({
                        "id": _ig_pk_to_shortcode(pk) if pk.isdigit() else pk,
                        "extractor": "Instagram",
                        "extractor_key": "Instagram",
                        "channel": owner,
                        "uploader": user.get("full_name") or owner,
                        "uploader_id": str(user.get("pk") or user.get("id") or ""),
                        "timestamp": item.get("taken_at"),
                        "duration": item.get("video_duration"),
                        "width": item.get("original_width"),
                        "height": item.get("original_height"),
                        "thumbnail": images[0]["url"] if images else None,
                        "webpage_url": url,
                    })
                    if progress_hook:
                        progress_hook({
                            "status": "downloading",
                            "_percent_str": f"{round(index / len(items) * 100)}%",
                            "_eta_str": "N/A",
                            "_total_bytes_str": "N/A",
                        })

                if not filepaths:
                    raise Exception("This story has no downloadable media.")

                info = dict(entries[0])
                info["title"] = reel.get("title") or f"Story by {owner}"
                return info, entries, filepaths
        except BaseException as e:
            _remove_tree(job_dir)
            if not isinstance(e, Exception) or isinstance(e, FileTooLargeError):
                raise
            last_error = e
            logger.warning("Instagram story API attempt failed | %s | %s", label, str(e)[:300])
            if "no longer available" in str(e) or "no downloadable media" in str(e):
                break
    raise last_error or Exception("Instagram login required: no Instagram session is configured.")


def _download_instagram(url: str, quality: str, progress_hook=None):
    url, wanted_pk = normalize_instagram_url(url)
    is_story = "/stories/" in url

    if quality == "audio":
        selector, extract_audio, tier, format_sort = AUDIO_SELECTOR, True, "audio", None
    else:
        tier = quality if quality in VIDEO_TIER_HEIGHTS else "best"
        height = VIDEO_TIER_HEIGHTS[tier]
        selector, extract_audio = VIDEO_SELECTOR, False
        format_sort = _video_format_sort(height)

    api_error = None
    if is_story and not extract_audio:
        try:
            info, entries, files = _download_instagram_story_via_api(url, wanted_pk, progress_hook)
            return info, entries, files, tier
        except Exception as e:
            api_error = e
            logger.info("Instagram story API path failed, falling back to yt-dlp: %s", str(e)[:300])
            if "no longer available" in str(e) or "429" in str(e):
                raise Exception(f"[instagram:story] {e}") from e

    wanted_ids = {_ig_pk_to_shortcode(wanted_pk)} if wanted_pk else None
    try:
        info, entries, files = _download_with_selector(
            url,
            selector,
            extract_audio,
            progress_hook,
            format_sort=format_sort,
            wanted_ids=wanted_ids,
            noplaylist=bool(is_story and "/highlights/" not in url),
        )
    except Exception as e:
        error_text = str(e).lower()
        failure = api_error if api_error is not None and (
            "media info not found" in error_text or "login" in error_text
        ) else e
        failure_text = str(failure).lower()
        # These only come from Instagram's *logged-out* code paths, so seeing
        # them while cookies are configured means the session isn't working.
        if any(h in failure_text for h in (
            "empty media response", "has no session cookie", "redirected to the login page",
        )) and any(label != "anonymous" for label, _ in _instagram_auth_sources()):
            _report_auth_problem("instagram", str(failure))
        shortcode = re.search(r"instagram\.com/(?:p|reel|tv)/([^/?#]+)", url)
        if shortcode and len(shortcode.group(1)) > 28 and "429" not in failure_text:
            # Shortcodes longer than 28 characters are private-account posts.
            raise Exception(
                "This content is only available for registered users who follow this account "
                f"(private post): {failure}"
            ) from e
        if is_story and "story" not in failure_text:
            raise Exception(f"[instagram:story] {failure}") from e
        if "video info extraction failed" in failure_text and "http error 400" in failure_text:
            # yt-dlp hides the reason; Instagram's body for this 400 is
            # {"message": "Media not found or unavailable"} — a deleted or
            # archived post, or one the bot's account isn't allowed to see.
            # Not something retrying or the bot can fix.
            raise Exception(
                "Instagram media not found or unavailable (deleted/archived post, "
                f"or its account isn't public): {failure}"
            ) from e
        if failure is not e:
            raise failure from e
        raise
    return info, entries, files, tier


def _get_instagram_view_count(*objects):
    """The Reel's real view count from any yt-dlp metadata shape (searched
    recursively; only positive integers count)."""

    def _search(obj):
        if isinstance(obj, dict):
            for key in ("view_count", "video_view_count", "play_count", "video_play_count"):
                value = obj.get(key)
                if value is None or isinstance(value, bool):
                    continue
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    return value
            for key, value in obj.items():
                if key in ("formats", "thumbnails", "requested_downloads", "http_headers"):
                    continue
                found = _search(value)
                if found is not None:
                    return found
        elif isinstance(obj, (list, tuple)):
            for value in obj:
                found = _search(value)
                if found is not None:
                    return found
        return None

    for obj in objects:
        found = _search(obj)
        if found is not None:
            return found
    return None


def _instagram_clip_play_count(ydl, entry: dict) -> int | None:
    """Recovers a Reel's play count through Instagram's Clips GraphQL
    connection (the same fallback Instaloader uses), for posts whose media
    info carries no view_count."""
    shortcode = entry.get("id")
    user_id = entry.get("uploader_id") or entry.get("channel_id")
    if not shortcode or not user_id or not _ig_csrf_token(ydl):
        return None
    try:
        cursor = None
        # One page only: every extra page is another ~1.5s and another
        # Instagram request per Reel, which matters far more than the count.
        for _ in range(1):
            variables = {"data": {"include_feed_video": True, "page_size": 12, "target_user_id": str(user_id)}}
            if cursor:
                variables["after"] = cursor
            data = urllib.parse.urlencode({
                "variables": json.dumps(variables, separators=(",", ":")),
                "doc_id": "27234427476213202",
            }).encode("utf-8")
            payload = _ig_api(
                ydl, "https://www.instagram.com/graphql/query", f"https://www.instagram.com/reel/{shortcode}/", data=data,
            )
            connection = (payload.get("data") or {}).get("xdt_api__v1__clips__user__connection_v2") or {}
            for edge in connection.get("edges") or []:
                media = (edge.get("node") or {}).get("media") or {}
                if str(media.get("code")) == str(shortcode):
                    count = int(media.get("play_count") or 0)
                    return count if count > 0 else None
            page_info = connection.get("page_info") or {}
            cursor = page_info.get("end_cursor")
            if not cursor or not page_info.get("has_next_page"):
                break
    except Exception as e:
        logger.info("Instagram play-count fallback failed for %s: %s", shortcode, str(e)[:200])
    return None


def _recover_instagram_view_count(ydl, url, metadata, processed, raw_entry, info_raw) -> None:
    if _is_instagram_story_url(url):
        return
    views = _get_instagram_view_count(processed, raw_entry, info_raw)
    if views is None:
        views = _instagram_clip_play_count(ydl, metadata)
    if views is not None:
        metadata["view_count"] = views
        metadata["video_view_count"] = views


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------

def _is_tiktok_photo_url(url: str) -> bool:
    return bool(re.search(r"tiktok\.com/@[^/]+/photo/\d+", url, re.IGNORECASE))


def resolve_tiktok_url(url: str) -> str:
    """Resolve TikTok short/share URLs without downloading media."""
    if not url or not re.search(r"(?:vm|vt)\.tiktok\.com/|tiktok\.com/t/", url, re.IGNORECASE):
        return url
    return _resolve_redirects(url)


def _resolve_tiktok_photo(url: str) -> dict:
    """Resolve a TikTok photo/slideshow post from the public web page's
    embedded item data (yt-dlp does not handle /photo/ URLs)."""
    item_id_match = re.search(r"/(?:photo|video)/(\d+)", url)
    if not item_id_match:
        raise ValueError("Could not determine the TikTok photo ID.")
    item_id = item_id_match.group(1)
    video_url = re.sub(r"/photo/(\d+)", r"/video/\1", url, flags=re.IGNORECASE)

    response = requests.get(
        video_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
        },
        timeout=30,
    )
    response.raise_for_status()

    blobs = []
    for pattern in (
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>',
    ):
        for raw_json in re.findall(pattern, response.text, flags=re.DOTALL | re.IGNORECASE):
            try:
                blobs.append(json.loads(raw_json.strip()))
            except ValueError:
                continue

    def find_item(obj):
        if isinstance(obj, dict):
            if str(obj.get("aweme_id") or obj.get("id") or "") == item_id and (
                obj.get("imagePost") or obj.get("image_post_info") or obj.get("imagePostInfo")
            ):
                return obj
            for value in obj.values():
                found = find_item(value)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = find_item(value)
                if found is not None:
                    return found
        return None

    item = next((found for found in map(find_item, blobs) if found), None)
    if item:
        post = item.get("image_post_info") or item.get("imagePostInfo") or item.get("imagePost") or {}
        image_urls = []
        for image in post.get("images") or []:
            if not isinstance(image, dict):
                continue
            ref = image.get("display_image") or image.get("displayImage") or image.get("imageURL") or image.get("imageUrl") or {}
            if isinstance(ref, dict):
                urls = ref.get("url_list") or ref.get("urlList") or []
                if urls:
                    image_urls.append(urls[-1])
            elif isinstance(ref, str):
                image_urls.append(ref)
        image_urls = list(dict.fromkeys(image_urls))
        if image_urls:
            author = item.get("author") or {}
            username = (author.get("unique_id") or author.get("uniqueId")) if isinstance(author, dict) else str(author)
            return {
                "id": item_id,
                "url": url,
                "title": item.get("desc") or "",
                "username": username or "",
                "images": image_urls,
            }
    raise ValueError("Could not extract photos from this TikTok post.")


def download_tiktok_photo(url: str, progress_hook=None):
    """Downloads every image in a TikTok photo/slideshow post.
    Returns (info, entries, filepaths)."""
    resolved = _resolve_tiktok_photo(url)
    image_urls = resolved["images"]
    job_dir = _new_job_dir()
    filepaths, entries = [], []
    try:
        for index, image_url in enumerate(image_urls[:MAX_PLAYLIST_ENTRIES], start=1):
            response = requests.get(
                image_url, timeout=30, headers={"User-Agent": USER_AGENT, "Referer": "https://www.tiktok.com/"},
            )
            response.raise_for_status()
            ext = _image_extension(image_url, response.headers.get("Content-Type", ""))
            path = os.path.join(job_dir, f"{resolved['id']}_{index}.{ext}")
            with open(path, "wb") as fh:
                fh.write(response.content)
            _check_file_size(path, MIN_VALID_IMAGE_BYTES)
            filepaths.append(path)
            entries.append({
                "id": f"{resolved['id']}_{index}",
                "extractor": "TikTok",
                "extractor_key": "TikTok",
                "url": image_url,
                "title": resolved["title"],
                "uploader": resolved["username"],
                "channel": resolved["username"],
                "thumbnail": image_url,
            })
            if progress_hook:
                progress_hook({
                    "status": "downloading",
                    "_percent_str": f"{round(index / len(image_urls) * 100)}%",
                    "_eta_str": "N/A",
                    "_total_bytes_str": "N/A",
                })
    except BaseException:
        _remove_tree(job_dir)
        raise

    info = {
        "id": resolved["id"],
        "extractor": "TikTok",
        "extractor_key": "TikTok",
        "webpage_url": resolved["url"],
        "title": resolved["title"],
        "uploader": resolved["username"],
        "channel": resolved["username"],
        "thumbnail": image_urls[0] if image_urls else None,
    }
    return info, entries, filepaths


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------

_SOUNDCLOUD_SHORT_HOSTS = ("on.soundcloud.com", "snd.sc")


def _clean_soundcloud_url(url: str) -> str:
    """https://soundcloud.com/<artist>/<track>[/s-<secret>] without the
    share-sheet tracking parameters (ref, si, utm_*, ...)."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in ("m.soundcloud.com", "www.soundcloud.com"):
        host = "soundcloud.com"
    return urllib.parse.urlunparse(("https", host, parsed.path.rstrip("/"), "", "", ""))


def resolve_soundcloud_url(url: str) -> str:
    """Turns an on.soundcloud.com / snd.sc short link into the real track URL.

    yt-dlp has no extractor for short links, so it hands them to its
    *generic* extractor, which downloads the short-link page itself — and
    SoundCloud's short-link service regularly answers server IPs with
    "HTTP Error 503: Service Unavailable". Only the redirect's Location
    header is needed, so it's read directly, retried, and (if configured)
    retried once more through ROTATING_PROXIES."""
    host = _host(url)
    if host not in _SOUNDCLOUD_SHORT_HOSTS:
        return _clean_soundcloud_url(url) if host.endswith("soundcloud.com") else url

    proxy = _get_random_proxy()
    routes = [None] + ([proxy] if proxy else [])
    last_error = None
    for attempt in range(3):
        for route in routes:
            try:
                response = requests.get(
                    url,
                    allow_redirects=False,
                    timeout=15,
                    headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                    proxies={"http": route, "https": route} if route else None,
                )
                location = response.headers.get("Location")
                if response.is_redirect and location:
                    target = urllib.parse.urljoin(url, location)
                    if _host(target) in _SOUNDCLOUD_SHORT_HOSTS:
                        url = target  # chained short link
                        continue
                    return _clean_soundcloud_url(target)
                last_error = Exception(f"HTTP Error {response.status_code} while resolving SoundCloud short link")
            except requests.RequestException as e:
                last_error = e
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    logger.warning("SoundCloud short link could not be resolved: %s | %s", url, last_error)
    raise Exception(f"Could not resolve SoundCloud short link: {last_error}")


def _soundcloud_metadata(url: str) -> dict:
    """Title/artist/duration of a SoundCloud track even when its audio is
    DRM-protected (ignore_no_formats_error lets the metadata through)."""
    with _open_ydl(_base_opts(ignore_no_formats_error=True, noplaylist=True)) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
        # Short links (on.soundcloud.com) come back as a reference to the
        # real track URL first.
        for _ in range(3):
            if not info or info.get("_type") not in ("url", "url_transparent") or not info.get("url"):
                break
            info = ydl.extract_info(info["url"], download=False, process=False)
    if not info or not info.get("title"):
        raise Exception("Media info not found.")
    if info.get("entries") is not None:
        raise Exception("SoundCloud sets with protected tracks are not supported.")
    return info


def _download_soundcloud(url: str, progress_hook=None):
    url = resolve_soundcloud_url(url)
    try:
        info, entries, files = _download_with_selector(url, "bestaudio/best", True, progress_hook)
        return info, entries, files, "audio"
    except Exception as e:
        text = str(e).lower()
        if not (isinstance(e, PreviewOnlyError) or "drm" in text):
            raise
        logger.info("SoundCloud track is protected/preview-only; looking for the same song elsewhere: %s", url)
        original_error = e

    try:
        meta = _soundcloud_metadata(url)
    except Exception:
        raise original_error

    artist = meta.get("artist") or (meta.get("artists") or [None])[0] or meta.get("uploader") or ""
    title = meta.get("track") or meta.get("title") or ""
    if artist and " - " in title and _normalize_text(artist) in _normalize_text(title.split(" - ")[0]):
        title = title.split(" - ", 1)[1]
    track = {
        "name": title,
        "artists": artist,
        "artist_list": [artist] if artist else [],
        "album": meta.get("album") or "",
        "album_artist": "",
        "cover_url": meta.get("thumbnail"),
        "duration_ms": int((meta.get("duration") or 0) * 1000),
        "release_date": meta.get("release_date") or "",
    }
    try:
        filepath = download_matching_audio(track, progress_hook, sources=("ytmusic", "youtube"))
    except Exception as fallback_error:
        logger.warning("SoundCloud fallback search failed: %s", str(fallback_error)[:300])
        raise original_error

    entry = {
        "id": meta.get("id"),
        "extractor": "soundcloud",
        "extractor_key": "Soundcloud",
        "title": title,
        "track": title,
        "artist": artist,
        "uploader": meta.get("uploader") or artist,
        "thumbnail": meta.get("thumbnail"),
        "duration": _audio_duration(filepath) or meta.get("duration"),
        "timestamp": meta.get("timestamp"),
        "like_count": meta.get("like_count"),
        "comment_count": meta.get("comment_count"),
        "view_count": meta.get("view_count"),
        "description": meta.get("description"),
        "webpage_url": url,
    }
    return entry, [entry], [filepath], "audio"


# ---------------------------------------------------------------------------
# Direct downloads (Instagram / TikTok / SoundCloud / YouTube audio-only)
# ---------------------------------------------------------------------------

def _generic_selector(tier: str):
    if tier == "audio":
        return AUDIO_SELECTOR, True, None
    height = VIDEO_TIER_HEIGHTS.get(tier)
    return VIDEO_SELECTOR, False, _video_format_sort(height)


def download_direct(url: str, quality: str = "best", allow_fallback: bool = False, progress_hook=None):
    """
    Download media from Instagram / TikTok / SoundCloud (and YouTube when the
    user always wants audio only).

    Returns (info, entries, filepaths, quality_used). `quality_used` differs
    from `quality` only when auto_quality_fallback had to step down the
    ladder because a file was too large for Telegram.
    """
    platform = _platform_of_url(url)

    if platform == "instagram":
        return _download_instagram(url, quality, progress_hook)

    if platform == "soundcloud":
        return _download_soundcloud(url, progress_hook)

    if platform == "tiktok":
        url = resolve_tiktok_url(url)
        if _is_tiktok_photo_url(url):
            if quality == "audio":
                raise UnsupportedLinkError("tiktok_photo_audio")
            info, entries, filepaths = download_tiktok_photo(url, progress_hook)
            return info, entries, filepaths, "best"

    if platform == "spotify":
        raise UnsupportedLinkError("spotify_direct")

    start = QUALITY_LADDER.index(quality) if quality in QUALITY_LADDER else 0
    tiers = QUALITY_LADDER[start:] if allow_fallback else [quality if quality in VIDEO_TIER_HEIGHTS or quality == "audio" else "best"]

    last_error = None
    for tier in tiers:
        selector, extract_audio, format_sort = _generic_selector(tier)
        if platform == "youtube":
            selector = "bestaudio[ext=m4a]/bestaudio/best" if extract_audio else selector
            format_sort = None
        try:
            info, entries, filepaths = _download_with_selector(
                url,
                selector,
                extract_audio,
                progress_hook,
                plans=_youtube_download_plans() if platform == "youtube" else None,
                format_sort=format_sort,
            )
            return info, entries, filepaths, tier
        except FileTooLargeError as e:
            # Only a size problem is worth retrying at a lower tier.
            last_error = e
            continue
    raise last_error or Exception("Download failed.")


# ---------------------------------------------------------------------------
# Per-video YouTube quality picker (thumbnail + buttons)
# ---------------------------------------------------------------------------

def _format_size_estimate(fmt: dict, duration) -> int:
    if not fmt:
        return 0
    exact = fmt.get("filesize") or fmt.get("filesize_approx")
    if exact:
        return int(exact)
    bitrate = fmt.get("vbr") or fmt.get("abr") or fmt.get("tbr")
    if bitrate and duration:
        return int(float(bitrate) * 1000 / 8 * duration)
    return 0


def _video_codec_rank(fmt: dict) -> int:
    codec = (fmt.get("vcodec") or "").lower()
    if codec.startswith("avc1"):
        return 50
    if codec.startswith(("hev1", "hvc1")):
        return 40
    if codec.startswith("vp9") or codec.startswith("vp09"):
        return 30
    if codec.startswith("av01"):
        return 20
    return 10


def _audio_codec_rank(fmt: dict) -> int:
    codec = (fmt.get("acodec") or "").lower()
    for rank, prefix in ((50, "mp4a"), (40, "aac"), (30, "opus"), (20, "vorbis")):
        if codec.startswith(prefix):
            return rank
    return 10


def _is_video_format(fmt: dict) -> bool:
    return fmt.get("vcodec") not in (None, "none") and bool(fmt.get("height"))


def _is_audio_format(fmt: dict) -> bool:
    return fmt.get("vcodec") in (None, "none") and fmt.get("acodec") not in (None, "none")


def probe_youtube_qualities(url: str) -> dict:
    """
    Probe a YouTube URL once and build all quality buttons locally, with a
    realistic size per button. Results are cached briefly so repeated
    requests for the same URL don't cause another extraction.
    """
    cache_key = url.strip().split("&")[0]
    now = time.monotonic()
    with _youtube_probe_cache_lock:
        cached = _youtube_probe_cache.get(cache_key)
        if cached:
            if now - cached[0] < YOUTUBE_PROBE_CACHE_TTL:
                return cached[1]
            del _youtube_probe_cache[cache_key]

    probe_opts = _base_opts(noplaylist=True)
    info = None

    # Public extraction first: ordinary public videos often expose a richer
    # format list without the authenticated session (which can get
    # SABR-only formats).
    try:
        with _youtube_probe_semaphore:
            public_info = _extract_resilient(probe_opts, url, plans=[_youtube_plan(["default"], False)])
        max_height = max((f.get("height") or 0 for f in public_info.get("formats") or [] if _is_video_format(f)), default=0)
        if max_height <= MIN_ACCEPTABLE_MAX_HEIGHT:
            raise Exception(f"Public YouTube probe returned only {max_height}p.")
        info = public_info
        logger.info("YouTube public probe succeeded | video_id=%s | max_height=%sp", info.get("id"), max_height)
    except Exception as public_error:
        logger.info("YouTube public probe not rich enough; using authenticated extraction | %s", str(public_error)[:300])
        with _youtube_probe_semaphore:
            info = _extract_resilient(probe_opts, url)

    if not info:
        raise Exception("Media info not found.")

    duration = info.get("duration") or 0
    formats = info.get("formats") or []
    video_formats = [f for f in formats if _is_video_format(f)]
    audio_formats = [f for f in formats if _is_audio_format(f)]

    # Some client responses expose only combined HLS formats and no
    # standalone audio. A small cookie-less probe usually has them.
    if not any(_format_size_estimate(f, duration) for f in audio_formats):
        try:
            audio_info = _extract_resilient(probe_opts, url, plans=[_youtube_plan(["default"], False)])
            fallback = [
                f for f in audio_info.get("formats") or []
                if _is_audio_format(f) and _format_size_estimate(f, duration)
            ]
            if fallback:
                audio_formats = fallback
        except Exception as e:
            logger.warning("YouTube audio fallback probe failed | %s", str(e)[:300])

    best_audio = None
    if audio_formats:
        m4a = [f for f in audio_formats if (f.get("ext") or "").lower() == "m4a"]
        # Original-language track first (see the "lang" note in
        # _download_with_selector), so the size shown matches what's sent.
        best_audio = max(
            m4a or audio_formats,
            key=lambda f: (
                f.get("language_preference") if f.get("language_preference") is not None else -1,
                f.get("abr") or 0,
                _audio_codec_rank(f),
                f.get("asr") or 0,
                _format_size_estimate(f, duration),
            ),
        )
    audio_size = _format_size_estimate(best_audio, duration)

    options = []
    seen_heights = set()
    for target_height in YOUTUBE_RESOLUTION_TIERS:
        candidates = [f for f in video_formats if (f.get("height") or 0) <= target_height]
        if not candidates:
            continue
        best = max(
            candidates,
            key=lambda f: (
                f.get("height") or 0,
                f.get("fps") or 0,
                _video_codec_rank(f),
                20 if f.get("ext") == "mp4" else 10,
                f.get("vbr") or 0,
                f.get("tbr") or 0,
                _format_size_estimate(f, duration),
            ),
        )
        height = best.get("height")
        if not height or height in seen_heights:
            continue
        video_size = _format_size_estimate(best, duration)
        total_size = video_size if best.get("acodec") not in (None, "none") else video_size + audio_size
        if total_size <= 0 or total_size > MAX_TELEGRAM_BYTES:
            continue
        seen_heights.add(height)
        options.append({"kind": "video", "label": f"{height}p", "height": height, "size_bytes": total_size})

    if best_audio and audio_size <= MAX_TELEGRAM_BYTES:
        options.append({"kind": "audio", "label": "Audio", "height": 0, "size_bytes": max(audio_size, 0)})

    result = {
        "id": info.get("id"),
        "title": info.get("title", ""),
        "thumbnail": info.get("thumbnail"),
        "options": options,
    }

    with _youtube_probe_cache_lock:
        if len(_youtube_probe_cache) >= YOUTUBE_PROBE_CACHE_MAX:
            oldest_key = min(_youtube_probe_cache, key=lambda k: _youtube_probe_cache[k][0])
            del _youtube_probe_cache[oldest_key]
        _youtube_probe_cache[cache_key] = (time.monotonic(), result)
    return result


def youtube_option_size(video_id: str, choice: str) -> int | None:
    """The size the quality picker showed for this button, if the probe
    result is still cached (used to estimate how long the job will take)."""
    with _youtube_probe_cache_lock:
        results = [result for _, result in _youtube_probe_cache.values()]
    for result in results:
        if result.get("id") != video_id:
            continue
        for option in result.get("options") or []:
            if (choice == "audio" and option.get("kind") == "audio") or str(option.get("height")) == str(choice):
                return option.get("size_bytes") or None
    return None


def _youtube_download(url: str, selector: str, extract_audio: bool, progress_hook=None):
    """Public clients first (richest formats, no account risk), then the
    configured cookies. A "confirm you're not a bot" check skips straight to
    the authenticated attempts; a 403 moves on to the next client."""
    return _download_with_selector(url, selector, extract_audio, progress_hook, plans=_youtube_download_plans())


def download_youtube_quality(video_id: str, height_or_audio: str, progress_hook=None):
    """Download one specific YouTube resolution (or the audio)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        raise ValueError("Invalid YouTube video id.")
    url = f"https://www.youtube.com/watch?v={video_id}"

    if height_or_audio == "audio":
        return _youtube_download(url, "bestaudio[ext=m4a]/bestaudio/best", True, progress_hook)

    height = int(height_or_audio)
    selector = (
        f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio/"
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/best"
    )
    return _youtube_download(url, selector, False, progress_hook)


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

_SPOTIFY_URL_RE = re.compile(
    r"spotify\.com/(?:intl-[a-z_-]+/)?(?:embed/)?(track|album|playlist|artist|episode|show)/([A-Za-z0-9]{22})",
    re.IGNORECASE,
)


_spotify_client = None
_spotify_client_lock = threading.Lock()


def get_spotify_client():
    """One shared client, so its access token is fetched once and refreshed
    only when it expires (instead of a token request per link)."""
    global _spotify_client
    with _spotify_client_lock:
        if _spotify_client is None:
            import spotipy
            from spotipy.cache_handler import MemoryCacheHandler
            from spotipy.oauth2 import SpotifyClientCredentials

            # Keep the access token in memory: the default cache handler
            # writes it to a ".cache" file in the working directory.
            auth = SpotifyClientCredentials(
                client_id=os.environ["SPOTIFY_CLIENT_ID"],
                client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
                cache_handler=MemoryCacheHandler(),
            )
            _spotify_client = spotipy.Spotify(client_credentials_manager=auth, requests_timeout=20, retries=3)
        return _spotify_client


def _parse_spotify_url(url: str):
    if "spotify.link" in _host(url):
        url = _resolve_redirects(url)
    match = _SPOTIFY_URL_RE.search(url)
    if not match:
        match = re.search(r"spotify:(track|album|playlist|artist|episode|show):([A-Za-z0-9]{22})", url)
    if not match:
        raise UnsupportedLinkError("spotify_unsupported")
    return match.group(1).lower(), match.group(2)


def _track_info(t: dict, album: dict | None = None) -> dict:
    album_data = album or t.get("album") or {}
    images = album_data.get("images") or []
    artist_list = [a["name"] for a in t.get("artists") or [] if a.get("name")]
    return {
        "name": t.get("name") or "",
        "artists": ", ".join(artist_list),
        "artist_list": artist_list,
        "album": album_data.get("name", ""),
        "album_artist": ", ".join(a["name"] for a in album_data.get("artists") or [] if a.get("name")),
        "cover_url": images[0]["url"] if images else None,
        "duration_ms": t.get("duration_ms") or 0,
        "track_number": t.get("track_number"),
        "disc_number": t.get("disc_number"),
        "total_tracks": album_data.get("total_tracks"),
        "release_date": album_data.get("release_date", ""),
        "isrc": (t.get("external_ids") or {}).get("isrc"),
        "explicit": bool(t.get("explicit")),
    }


def _spotify_paginate(sp, page: dict, limit: int) -> list:
    items = list(page.get("items") or [])
    while page.get("next") and len(items) < limit:
        page = sp.next(page)
        if not page:
            break
        items.extend(page.get("items") or [])
    return items[:limit]


def resolve_spotify_tracks(url: str) -> list:
    """Track/album/playlist metadata only, from Spotify's official Web API —
    no audio here yet. Albums and playlists are paginated (the API returns
    at most 50/100 items per page) and capped at MAX_SPOTIFY_TRACKS."""
    kind, spotify_id = _parse_spotify_url(url)
    sp = get_spotify_client()

    if kind == "track":
        return [_track_info(sp.track(spotify_id))]

    if kind == "album":
        album = sp.album(spotify_id)
        items = _spotify_paginate(sp, album.get("tracks") or {}, MAX_SPOTIFY_TRACKS)
        return [_track_info(t, album=album) for t in items if t and t.get("name")]

    if kind == "playlist":
        page = sp.playlist_items(spotify_id, additional_types=("track",))
        tracks = []
        for item in _spotify_paginate(sp, page, MAX_SPOTIFY_TRACKS * 2):
            track = (item or {}).get("track") or (item or {}).get("item")
            if not track or track.get("is_local") or track.get("type", "track") != "track" or not track.get("name"):
                continue
            tracks.append(_track_info(track))
            if len(tracks) >= MAX_SPOTIFY_TRACKS:
                break
        return tracks

    raise UnsupportedLinkError("spotify_unsupported")


# --- audio matching ---------------------------------------------------------

# Words that mark a *different recording* of the song. A candidate is
# penalised for each one it contains that the wanted title doesn't.
_VERSION_MARKERS = (
    "cover", "karaoke", "instrumental", "remix", "live", "acoustic", "sped up",
    "speed up", "slowed", "reverb", "nightcore", "8d", "bass boosted",
    "reaction", "tutorial", "lesson", "piano", "pianoforte", "acapella",
    "a cappella", "orchestral", "symphonic", "lofi", "lo fi", "music box",
    "8 bit", "bootleg", "rework", "extended", "hour", "hours", "loop",
    "mashup", "parody", "teaser", "trailer", "snippet", "preview", "demo",
    "rehearsal", "concert", "tiktok", "chipmunk", "guitar", "violin",
    "کاور", "ریمیکس", "بی کلام", "بیکلام", "لایو", "اجرای زنده", "کارائوکه",
)

MATCH_ACCEPT_SCORE = 50
MATCH_STRONG_SCORE = 85
MAX_MATCH_DOWNLOAD_ATTEMPTS = 6


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"['’`´]", "", text)
    return " ".join(re.findall(r"\w+", text))


def _core_title(title: str) -> str:
    """The song name without "(feat. X)", "[Official Video]",
    " - Remastered 2011" and similar decorations."""
    text = re.sub(r"[\(\[\{【（].*?[\)\]\}】）]", " ", title or "")
    text = re.sub(
        r"\s[-–—|]\s.*\b(remaster(ed)?|version|edit|mix|live|mono|stereo|from|feat|with|recorded|original)\b.*$",
        " ", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"\b(feat|ft|featuring)\b\.?.*$", " ", text, flags=re.IGNORECASE)
    return _normalize_text(text) or _normalize_text(title)


def _artist_tokens(name: str) -> list:
    tokens = _normalize_text(name).split()
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    return tokens


def _token_coverage(needle: list, haystack: list) -> float:
    """Fraction of `needle` tokens present in `haystack`, with fuzzy matching
    for transliteration differences ("kojaie" vs "kojaei")."""
    if not needle:
        return 0.0
    hay = set(haystack)
    hits = 0.0
    for token in needle:
        if token in hay:
            hits += 1
        elif len(token) >= 4 and any(
            # Transliterations keep the length ("kojaie"/"kojaei"); a
            # different word that merely starts the same ("koja") doesn't.
            abs(len(token) - len(h)) <= 1 and difflib.SequenceMatcher(None, token, h).ratio() >= 0.8
            for h in hay
        ):
            hits += 0.9
    return hits / len(needle)


def _has_marker(text: str, marker: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", text) is not None


def _score_candidate(track: dict, cand: dict) -> float:
    wanted_title = _core_title(track["name"])
    wanted_tokens = wanted_title.split()
    raw_wanted = _normalize_text(f"{track['name']} {track.get('album', '')}")
    artist_names = track.get("artist_list") or [a.strip() for a in (track.get("artists") or "").split(",") if a.strip()]

    cand_title_raw = cand.get("title") or ""
    cand_title = _core_title(cand_title_raw)
    cand_artists = cand.get("artists") or []
    cand_context = _normalize_text(" ".join([cand_title_raw, cand.get("channel") or "", " ".join(cand_artists)]))
    context_tokens = cand_context.split()

    # Title
    if cand["source"] == "ytmusic":
        ratio = difflib.SequenceMatcher(None, wanted_title, cand_title).ratio()
        coverage = min(_token_coverage(wanted_tokens, cand_title.split()), _token_coverage(cand_title.split(), wanted_tokens))
        title_score = max(ratio, coverage)
    else:
        title_score = _token_coverage(wanted_tokens, _normalize_text(cand_title_raw).split())

    # Artist
    artist_score = 0.0
    for position, name in enumerate(artist_names):
        tokens = _artist_tokens(name)
        if cand_artists:
            best = 0.0
            for cand_artist in cand_artists:
                cand_tokens = _artist_tokens(cand_artist)
                # Whole-string similarity only counts when it's near-identical;
                # two unrelated names still share ~40% of their letters.
                ratio = difflib.SequenceMatcher(None, " ".join(tokens), " ".join(cand_tokens)).ratio()
                best = max(best, _token_coverage(tokens, cand_tokens), ratio if ratio >= 0.85 else 0.0)
        else:
            best = _token_coverage(tokens, context_tokens)
        artist_score = max(artist_score, best if position == 0 else best * 0.8)

    # Duration
    expected = (track.get("duration_ms") or 0) / 1000
    duration = cand.get("duration")
    if expected and duration:
        diff = abs(float(duration) - expected)
        if diff > 25:
            return -100.0
        duration_score = 30 if diff <= 2 else 25 if diff <= 5 else 12 if diff <= 10 else 0
    else:
        duration_score = -10

    # Different recordings
    cand_raw = _normalize_text(cand_title_raw)
    penalties = sum(
        1 for marker in _VERSION_MARKERS
        if _has_marker(cand_raw, _normalize_text(marker) or marker) and not _has_marker(raw_wanted, _normalize_text(marker) or marker)
    )

    bonus = 0
    if cand["source"] == "ytmusic":
        bonus += 10
    if (cand.get("channel") or "").lower().endswith("- topic"):
        bonus += 10
    bonus += {0: 5, 1: 2}.get(cand.get("rank", 9), 0)

    # A cover/remix/live take, or the same title by a different artist, is
    # the wrong song — those must fall below the acceptance threshold even
    # with a perfect title and duration.
    mismatch = 60 * penalties
    if artist_score < 0.4:
        mismatch += 40
    if title_score < 0.4:
        mismatch += 20

    return 40 * title_score + 25 * artist_score + duration_score + bonus - mismatch


def _search_ytmusic(track: dict) -> list:
    if not _ytmusic_available():
        return []
    from ytmusicapi import YTMusic

    main_artist = (track.get("artist_list") or [track.get("artists", "")])[0]
    queries = [f"{main_artist} {_core_title(track['name'])}".strip(), f"{track['name']} {track.get('artists', '')}".strip()]
    ytmusic = YTMusic()
    candidates = []
    for query in dict.fromkeys(queries):
        try:
            results = ytmusic.search(query, filter="songs", limit=10)
        except Exception as e:
            logger.warning("YouTube Music search failed | %s | %s", query, str(e)[:200])
            continue
        for rank, result in enumerate(r for r in results if r.get("videoId")):
            candidates.append({
                "source": "ytmusic",
                "rank": rank,
                "url": f"https://www.youtube.com/watch?v={result['videoId']}",
                "title": result.get("title") or "",
                "artists": [a.get("name") for a in result.get("artists") or [] if a.get("name")],
                "duration": result.get("duration_seconds"),
                "channel": "",
            })
            if rank >= 6:
                break
        if candidates:
            break
    return candidates


def _search_with_ytdlp(query: str, source: str) -> list:
    opts = _base_opts(extract_flat="in_playlist", ignoreerrors=True, playlistend=8)
    proxy = _get_random_proxy()
    if proxy:
        opts["proxy"] = proxy
    try:
        with _open_ydl(opts) as ydl:
            info = ydl.extract_info(query, download=False, process=True)
    except Exception as e:
        logger.warning("Search failed | %s | %s", query, str(e)[:200])
        return []
    candidates = []
    for rank, entry in enumerate(e for e in (info or {}).get("entries") or [] if e):
        url = entry.get("webpage_url") or entry.get("url")
        if source == "youtube" and entry.get("id") and not (url or "").startswith("http"):
            url = f"https://www.youtube.com/watch?v={entry['id']}"
        if not url:
            continue
        candidates.append({
            "source": source,
            "rank": rank,
            "url": url,
            "title": entry.get("title") or "",
            "artists": [],
            "duration": entry.get("duration"),
            "channel": entry.get("channel") or entry.get("uploader") or "",
        })
    return candidates


def _match_sources(track: dict, sources) -> list:
    main_artist = (track.get("artist_list") or [track.get("artists", "")])[0]
    core = _core_title(track["name"])
    stages = []
    for source in sources:
        if source == "ytmusic":
            stages.append(("ytmusic", lambda: _search_ytmusic(track)))
        elif source == "youtube":
            stages.append(("youtube", lambda: _search_with_ytdlp(f"ytsearch8:{main_artist} - {core}", "youtube")))
        elif source == "soundcloud":
            stages.append(("soundcloud", lambda: _search_with_ytdlp(f"scsearch8:{main_artist} {core}", "soundcloud")))
    return stages


def download_matching_audio(track: dict, progress_hook=None, sources=("ytmusic", "youtube", "soundcloud")) -> str:
    """
    Find, download, verify and tag the recording that matches `track`
    (a dict shaped like _track_info()). Returns the audio file path.

    Candidates from every source are scored (title, artists, duration,
    source trust) and tried best-first; covers, remixes, live versions etc.
    are penalised, and the downloaded file's real length must match the
    expected duration — which also rejects 30-second SoundCloud previews.
    """
    if not (track.get("name") or "").strip():
        raise Exception("Cannot search for a track without a title.")
    expected = (track.get("duration_ms") or 0) / 1000
    tolerance = 8 + expected * 0.025
    pool, tried = [], set()
    attempts = 0
    last_error = None
    youtube_blocked = False  # bot check with no working cookies: stop trying YouTube URLs
    stages = _match_sources(track, sources)

    for stage_index, (source, search) in enumerate(stages):
        for cand in search():
            cand["score"] = _score_candidate(track, cand)
            pool.append(cand)
            logger.info(
                "Match candidate | %.0f | %s | %s | %s | %ss",
                cand["score"], source, cand["title"], ", ".join(cand["artists"]) or cand["channel"], cand.get("duration"),
            )

        ranked = sorted(
            (c for c in pool if c["score"] >= MATCH_ACCEPT_SCORE and c["url"] not in tried),
            key=lambda c: c["score"],
            reverse=True,
        )
        is_last_stage = stage_index == len(stages) - 1
        if not is_last_stage and (not ranked or ranked[0]["score"] < MATCH_STRONG_SCORE):
            continue  # gather more sources before settling for a weak match

        for cand in ranked:
            if attempts >= MAX_MATCH_DOWNLOAD_ATTEMPTS:
                break
            if not is_last_stage and cand["score"] < MATCH_STRONG_SCORE:
                break
            if youtube_blocked and _is_youtube_url(cand["url"]):
                continue
            tried.add(cand["url"])
            attempts += 1
            filepath = None
            try:
                logger.info("Match selected | %.0f | %s | %s | %s", cand["score"], cand["source"], cand["title"], cand["url"])
                if _is_youtube_url(cand["url"]):
                    _, _, files = _youtube_download(cand["url"], "bestaudio[ext=m4a]/bestaudio/best", True, progress_hook)
                else:
                    _, _, files = _download_with_selector(cand["url"], "bestaudio/best", True, progress_hook, noplaylist=True)
                filepath = files[0]
                actual = _audio_duration(filepath)
                if expected and actual and abs(actual - expected) > tolerance:
                    raise Exception(f"downloaded audio is {actual:.0f}s, expected {expected:.0f}s")
                tag_audio_file(
                    filepath,
                    title=track["name"],
                    artist=track.get("artists", ""),
                    album=track.get("album", ""),
                    cover_url=track.get("cover_url"),
                    album_artist=track.get("album_artist", ""),
                    release_date=track.get("release_date", ""),
                    track_number=track.get("track_number"),
                    total_tracks=track.get("total_tracks"),
                    disc_number=track.get("disc_number"),
                )
                return filepath
            except FileTooLargeError:
                raise
            except Exception as e:
                last_error = e
                logger.warning("Match candidate failed | %s | %s", cand["url"], str(e)[:300])
                if filepath:
                    remove_download_files([filepath])
                if _is_youtube_url(cand["url"]) and any(h in str(e).lower() for h in _YOUTUBE_BOT_CHECK_HINTS):
                    youtube_blocked = True

    raise last_error or Exception(f"No matching audio found for: {track.get('artists')} - {track.get('name')}")


def download_spotify_track(track: dict, progress_hook=None) -> str:
    """Find and download the matching audio for one Spotify track."""
    return download_matching_audio(track, progress_hook)


# ---------------------------------------------------------------------------
# Audio tagging
# ---------------------------------------------------------------------------

def _fetch_cover(cover_url: str):
    response = requests.get(cover_url, timeout=10, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    content_type = (response.headers.get("Content-Type") or "").split(";")[0].lower()
    if content_type not in {"image/jpeg", "image/png"}:
        content_type = "image/png" if cover_url.lower().endswith(".png") else "image/jpeg"
    return response.content, content_type


def tag_audio_file(
    filepath: str,
    title: str = "",
    artist: str = "",
    album: str = "",
    cover_url: str | None = None,
    album_artist: str = "",
    release_date: str = "",
    track_number: int | None = None,
    total_tracks: int | None = None,
    disc_number: int | None = None,
    total_discs: int | None = None,
) -> None:
    """Write metadata (ID3 for MP3, MP4 atoms for M4A). Never raises."""
    if not filepath:
        return
    lower_path = filepath.lower()

    try:
        if lower_path.endswith(".mp3"):
            from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK

            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()

            if title:
                tags["TIT2"] = TIT2(encoding=3, text=[title])
            if artist:
                tags["TPE1"] = TPE1(encoding=3, text=[artist])
            if album:
                tags["TALB"] = TALB(encoding=3, text=[album])
            if album_artist:
                tags["TPE2"] = TPE2(encoding=3, text=[album_artist])
            if release_date:
                tags["TDRC"] = TDRC(encoding=3, text=[str(release_date)])
            if track_number:
                text = f"{track_number}/{total_tracks}" if total_tracks else str(track_number)
                tags["TRCK"] = TRCK(encoding=3, text=[text])
            if disc_number:
                text = f"{disc_number}/{total_discs}" if total_discs else str(disc_number)
                tags["TPOS"] = TPOS(encoding=3, text=[text])
            if cover_url:
                try:
                    cover_bytes, mime = _fetch_cover(cover_url)
                    tags.delall("APIC")
                    tags["APIC"] = APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_bytes)
                except Exception as e:
                    logger.warning("Could not embed MP3 cover art into %s: %s", filepath, e)
            tags.save(filepath, v2_version=3)
            return

        if lower_path.endswith(".m4a"):
            from mutagen.mp4 import MP4, MP4Cover

            audio = MP4(filepath)
            if audio.tags is None:
                audio.add_tags()
            if title:
                audio.tags["\xa9nam"] = [title]
            if artist:
                audio.tags["\xa9ART"] = [artist]
            if album:
                audio.tags["\xa9alb"] = [album]
            if album_artist:
                audio.tags["aART"] = [album_artist]
            if release_date:
                audio.tags["\xa9day"] = [str(release_date)]
            if track_number:
                audio.tags["trkn"] = [(int(track_number), int(total_tracks or 0))]
            if disc_number:
                audio.tags["disk"] = [(int(disc_number), int(total_discs or 0))]
            if cover_url:
                try:
                    cover_bytes, mime = _fetch_cover(cover_url)
                    image_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
                    audio.tags["covr"] = [MP4Cover(cover_bytes, imageformat=image_format)]
                except Exception as e:
                    logger.warning("Could not embed M4A cover art into %s: %s", filepath, e)
            audio.save()
            return

        logger.info("Audio tagging skipped for unsupported file type: %s", filepath)
    except Exception as e:
        logger.exception("Audio tagging failed for %s: %s", filepath, e)


# ---------------------------------------------------------------------------
# Cache keys for re-sending already uploaded files
# ---------------------------------------------------------------------------

def media_cache_key(url: str, quality: str) -> str | None:
    """A stable key for "this exact media at this quality", so a link many
    people send (e.g. the Reel an influencer just posted) is downloaded once
    and then re-sent by Telegram file_id. Never makes network requests;
    returns None for anything whose content can change (a user's *current*
    stories) or that can't be keyed offline (share/short links)."""
    platform = _platform_of_url(url)
    parsed = urllib.parse.urlparse(url)
    try:
        if platform == "instagram":
            if parsed.path.lower().startswith("/share/"):
                return None
            canonical, wanted_pk = normalize_instagram_url(url)
            if "/stories/" in canonical and not wanted_pk and "/highlights/" not in canonical:
                return None
            if wanted_pk:
                canonical += f"#{wanted_pk}"
        elif platform == "youtube":
            query = urllib.parse.parse_qs(parsed.query)
            video_id = (query.get("v") or [""])[0]
            if not video_id:
                match = re.search(r"(?:youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})", url)
                video_id = match.group(1) if match else ""
            if not video_id:
                return None
            canonical = f"youtube:{video_id}"
        elif platform == "soundcloud":
            if _host(url) in _SOUNDCLOUD_SHORT_HOSTS:
                return None
            canonical = _clean_soundcloud_url(url)
        elif platform == "tiktok":
            if not re.search(r"/(?:video|photo)/\d+", parsed.path):
                return None  # vm./vt. short links: resolved later
            canonical = f"tiktok:{re.search(r'/(?:video|photo)/(\d+)', parsed.path).group(1)}"
        else:
            return None
    except UnsupportedLinkError:
        return None
    return f"{canonical}|{quality}"


# ---------------------------------------------------------------------------
# Error classification (used to pick a user-facing message)
# ---------------------------------------------------------------------------

def classify_error(platform: str, error: Exception) -> str:
    """Maps an internal download error to a message key. The raw yt-dlp text
    is only ever stored for the admin, never shown to the user."""
    if isinstance(error, UnsupportedLinkError):
        return error.kind
    if isinstance(error, FileTooLargeError):
        return "too_large"
    text = str(error).lower()

    if "drm" in text or isinstance(error, PreviewOnlyError):
        return "drm"
    if platform == "instagram":
        if "instagram media not found or unavailable" in text:
            return "ig_not_found"
        if "registered users who follow" in text or "private" in text:
            return "ig_private"
        if "429" in text or "too many requests" in text or "rate-limit" in text or "please wait a few minutes" in text:
            return "ig_rate_limited"
        if "not available to everyone" in text or "certain audiences" in text or "restricted" in text:
            return "ig_restricted"
        if "no longer available" in text or "expired" in text:
            return "ig_expired"
        if "stories/" in text or "story" in text:
            return "ig_story_login"
        if any(h in text for h in ("log in", "login", "cookies", "empty media response", "unreachable")):
            return "ig_login"
        if "404" in text or "not found" in text or "unavailable" in text:
            return "not_found"
        return "instagram_failed"
    if platform == "tiktok":
        if "ip address is blocked" in text or "not available in your" in text:
            return "tiktok_blocked"
        if "log in" in text or "login" in text or "comfortable for some audiences" in text:
            return "tiktok_login"
        if "404" in text or "not found" in text or "unavailable" in text or "deleted" in text:
            return "not_found"
        return "tiktok_failed"
    if platform == "youtube":
        if "private video" in text or "members-only" in text or "join this channel" in text:
            return "yt_private"
        if "video unavailable" in text or "has been removed" in text or "terminated" in text:
            return "not_found"
        if "age" in text and ("restricted" in text or "confirm your age" in text):
            return "yt_age"
        return "youtube_failed"
    if platform == "soundcloud":
        if "404" in text or "not found" in text:
            return "not_found"
        if "503" in text or "service unavailable" in text or "could not resolve soundcloud" in text:
            return "platform_unavailable"
        return "soundcloud_failed"
    if platform == "spotify":
        if "404" in text or "resource not found" in text or "invalid id" in text:
            return "spotify_not_found"
        return "spotify_failed"
    return "download_failed"
