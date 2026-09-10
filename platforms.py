"""
Platform detection, media extraction, and audio tagging.

Instagram / YouTube / SoundCloud are downloaded directly with yt-dlp, which
supports all three natively. YouTube also has a second, per-video path (see
probe_youtube_qualities / download_youtube_quality) that inspects a specific
video's actual available resolutions and their real file sizes, for the
thumbnail-plus-buttons quality picker.

Spotify is different: Spotify's own audio streams are DRM-protected, so
there is no direct "download from Spotify" here. Instead, track metadata
(title, artist, album, cover art, duration) is fetched from Spotify's
official Web API, and the matching audio is located on SoundCloud first
(usually more reliable, no YouTube bot-checks involved) and downloaded from
YouTube only if SoundCloud doesn't have a good match — a "good match" being
checked against Spotify's own track duration, not just whatever a search
happens to return first. This never touches Spotify's protected streams.
"""

import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
import urllib.parse
import urllib.request
import html
import requests

import yt_dlp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lightweight YouTube probe cache
# ---------------------------------------------------------------------------

YOUTUBE_PROBE_CACHE_TTL = 900  # 15 minutes
YOUTUBE_PROBE_CACHE_MAX = 64

_youtube_probe_cache = {}
_youtube_probe_cache_lock = threading.Lock()
_youtube_probe_semaphore = threading.Semaphore(1)

DOWNLOAD_DIR = "downloads"

PLATFORM_NAMES = {
    "instagram": "Instagram",
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "soundcloud": "SoundCloud",
    "spotify": "Spotify",
}

PLATFORM_PATTERNS = {
    "instagram": re.compile(r"instagram\.com/\S+", re.IGNORECASE),
    "youtube": re.compile(r"(youtube\.com|youtu\.be)/\S+", re.IGNORECASE),
    "tiktok": re.compile(
        r"(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)/\S+",
        re.IGNORECASE,
    ),
    "soundcloud": re.compile(r"soundcloud\.com/\S+", re.IGNORECASE),
    "spotify": re.compile(r"(open\.)?spotify\.com/\S+", re.IGNORECASE),
}

# Telegram's classic Bot API caps uploads at 50MB. Running your own Local Bot
# API Server (which you've done) raises that to 2000MB. Set MAX_UPLOAD_MB in
# .env if that ever changes; defaults to a hair under 2GB either way.
MAX_TELEGRAM_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "1990")) * 1024 * 1024

# Anything downloaded smaller than this is treated as a failed/corrupt
# attempt (worth retrying with a different client) rather than a real file
# — no legitimate video or song is this small.
MIN_VALID_FILE_BYTES = 20 * 1024

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov"}

QUALITY_LADDER = ["best", "720p", "audio"]

# Instagram/SoundCloud generally only ever offer progressive (already-muxed)
# formats, so a plain "best" is enough and safest. YouTube commonly splits
# high-resolution video and audio into separate DASH streams, so it needs an
# explicit bestvideo+bestaudio merge selector to get anything above ~720p.
QUALITY_FORMATS = {
    "best": "best/all",
    "720p": "best[height<=720]/best/all",
    "audio": "bestaudio/best",
}

INSTAGRAM_QUALITY_FORMATS = {
    "best": "best/all",
    "1080p": "best[height<=1080]/best/all",
    "720p": "best[height<=720]/best/all",
    "480p": "best[height<=480]/best/all",
    "360p": "best[height<=360]/best/all",
}

YOUTUBE_QUALITY_FORMATS = {
    "best": "bestvideo+bestaudio/best/all",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best/all",
    "audio": "bestaudio/best",
}

# Standard resolution tiers offered by the per-video YouTube quality picker.
YOUTUBE_RESOLUTION_TIERS = [2160, 1440, 1080, 720, 480, 360]

# If the richest format list we can get for a video tops out at or below
# this, it's worth a second attempt with a different client — some clients
# (the TV client in particular) sometimes expose a noticeably shorter format
# list than others for reasons that have nothing to do with what the video
# actually offers.
MIN_ACCEPTABLE_MAX_HEIGHT = 480
PROBE_RICH_FALLBACK_CLIENTS = ["default", "web_embedded"]

class FileTooLargeError(Exception):
    pass


def detect_platform(text: str):
    """Returns the platform key for the first recognized link in `text`, or None."""
    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(text):
            return platform
    return None


def extract_url(text: str) -> str | None:
    """Extract the first supported platform URL from arbitrary text."""

    if not text:
        return None

    patterns = (
        r"https?://(?:www\.)?(?:instagram\.com)/\S+",
        r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+",
        r"https?://(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)/\S+",
        r"https?://(?:www\.)?(?:soundcloud\.com|on\.soundcloud\.com)/\S+",
        r"https?://(?:(?:open\.)?spotify\.com)/\S+",
    )

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            return match.group(0).rstrip(
                ".,!?)]}>"
            )

    return None

def _is_youtube_url(url: str) -> bool:
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def cleanup_stray_downloads() -> None:
    """Removes anything left in DOWNLOAD_DIR from a previous run that crashed
    or errored before its own cleanup ran. Safe to call on every startup."""
    if not os.path.isdir(DOWNLOAD_DIR):
        return
    for name in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def _cleanup_id(entry_id) -> None:
    """Removes any leftover file(s) for one specific download id — used
    between retry attempts so a partial file from a failed client doesn't
    interfere with (or get mistaken for the result of) the next attempt."""
    if not entry_id or not os.path.isdir(DOWNLOAD_DIR):
        return
    prefix = f"{entry_id}."
    for name in os.listdir(DOWNLOAD_DIR):
        if name.startswith(prefix):
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, name))
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


def check_dependencies() -> None:
    """Logs the state of external dependencies once at startup, so problems
    show up immediately in `journalctl` instead of only surfacing as a
    confusing error the first time someone tries to download something."""
    import shutil

    _ffmpeg_location()  # logs its own found/not-found line

    if shutil.which("deno") or shutil.which("node") or shutil.which("bun") or shutil.which("qjs"):
        logger.info("YouTube JS challenge solver: an external JS runtime is available.")
    else:
        logger.warning(
            "YouTube JS challenge solver: no external JS runtime found (deno/node/bun/qjs). "
            "Since yt-dlp 2025.11, this is required for full YouTube support — without it, "
            "some formats are unavailable and downloads can fail with errors like "
            "'HTTP Error 403: Forbidden' even though metadata extraction succeeds. "
            "Install Deno (recommended): curl -fsSL https://deno.land/install.sh | sh "
            "then also run: pip install -U yt-dlp yt-dlp-ejs"
        )


def _ffmpeg_location():
    """Path to the ffmpeg/ffprobe folder, resolved once and reused.

    Preference order: an explicit FFMPEG_LOCATION in .env, then Python's own
    shutil.which() (not yt-dlp's internal PATH search) — this exists
    because "ffmpeg is installed but yt-dlp still can't find it" usually
    means the *process* yt-dlp is running in doesn't see it on PATH, even
    though an interactive SSH session does. This is common under systemd:
    a service's PATH is whatever systemd itself sets, not your shell's
    .bashrc/.profile PATH. Resolving it ourselves with shutil.which() (run
    in this exact process) and handing yt-dlp the answer directly sidesteps
    that mismatch entirely, and logs a clear, unambiguous line either way
    instead of leaving you to guess from yt-dlp's generic error."""
    global _FFMPEG_LOCATION_CACHE
    if _FFMPEG_LOCATION_CACHE is not None:
        return _FFMPEG_LOCATION_CACHE or None

    explicit = os.environ.get("FFMPEG_LOCATION")
    if explicit:
        logger.info("ffmpeg: using explicit FFMPEG_LOCATION=%s", explicit)
        _FFMPEG_LOCATION_CACHE = explicit
        return explicit

    import shutil
    found = shutil.which("ffmpeg")
    if found:
        folder = os.path.dirname(found)
        logger.info("ffmpeg: auto-detected at %s (this process's PATH)", found)
        _FFMPEG_LOCATION_CACHE = folder
        return folder

    logger.warning(
        "ffmpeg: shutil.which('ffmpeg') found nothing in this process's PATH (%s). "
        "This is the exact environment yt-dlp runs in — if `which ffmpeg` over SSH "
        "finds it but this log line doesn't, the process (systemd service) has a "
        "different PATH than your shell. Fix: set FFMPEG_LOCATION in .env to the "
        "folder containing the ffmpeg binary (e.g. /usr/bin).",
        os.environ.get("PATH", "<unset>"),
    )
    _FFMPEG_LOCATION_CACHE = ""
    return None


_FFMPEG_LOCATION_CACHE = None


def _get_random_proxy():
    """ROTATING_PROXIES in .env — one or more proxy URLs (comma-separated),
    e.g. a local rotating SOCKS5 gateway. Only used for probing/search calls
    (see use_proxy on the functions below) — NOT for the actual file
    transfer. If the proxy's exit IP changes mid-download (which is exactly
    what a "rotating" proxy is for), a single logical download can end up
    stitched together from two different connections and come out corrupt.
    Keeping it to the lightweight metadata calls still gets the main
    benefit (fewer identical-looking requests from your bare VPS IP) without
    that risk."""
    proxies_env = os.environ.get("ROTATING_PROXIES")
    if proxies_env:
        proxy_list = [p.strip() for p in proxies_env.split(",") if p.strip()]
        if proxy_list:
            return random.choice(proxy_list)
    return None


# YouTube's bot/token checks have been a genuinely unstable target through
# 2026 — yt-dlp's own issue tracker describes errors like "the page needs to
# be reloaded" as intermittent even with an otherwise-working setup (some
# reports: roughly 1 success in 10 tries with a single client identity). No
# single client choice is reliable enough on its own right now, so
# extraction tries several in turn and only gives up if all of them fail.
# Override with YTDLP_PLAYER_CLIENT in .env to pin one instead
# (comma-separated — that becomes a single attempt using all of them
# together, not tried separately).
PLAYER_CLIENT_ATTEMPTS = [
    ["default", "web_embedded"],
    ["web_safari"],
    ["ios"],
    ["mweb"],
]

# Substrings that mean "this looks like one of YouTube's bot/token checks,
# or a corrupted/incomplete result, worth retrying with a different client"
# rather than a real failure (link is private, deleted, etc.) that retrying
# won't fix.
_RETRYABLE_ERROR_HINTS = ("reload", "sign in", "not a bot", "confirm you", "unavailable", "incomplete download")


def _client_attempts():
    override = os.environ.get("YTDLP_PLAYER_CLIENT")
    if override:
        return [[c.strip() for c in override.split(",")]]
    return PLAYER_CLIENT_ATTEMPTS

def _browser_cookie_source(env_prefix: str):
    browser = os.environ.get(
        f"{env_prefix}_COOKIES_BROWSER",
        "",
    ).strip()

    if not browser:
        return None

    profile = os.environ.get(
        f"{env_prefix}_COOKIES_PROFILE",
        "",
    ).strip() or None

    return (
        browser,
        profile,
    )

def _youtube_extra_opts(
    clients,
    use_cookies: bool = True,
) -> dict:
    opts = {
        "extractor_args": {
            "youtube": {
                "player_client": clients
            }
        }
    }

    if not use_cookies:
        return opts

    cookiefile = os.environ.get(
        "YOUTUBE_COOKIE_FILE",
        "cookies.txt",
    )

    if cookiefile and os.path.exists(cookiefile):
        opts["cookiefile"] = (
            cookiefile
        )

    browser = os.environ.get(
        "YTDLP_COOKIES_BROWSER"
    )

    if browser:
        opts["cookiesfrombrowser"] = (
            browser,
        )

    return opts

def _is_tiktok_photo_url(
    url: str,
) -> bool:
    return bool(
        re.search(
            r"https?://(?:www\.)?tiktok\.com/"
            r"@[^/]+/photo/\d+",
            url,
            re.IGNORECASE,
        )
    )

def resolve_tiktok_url(
    url: str,
) -> str:
    """Resolve TikTok short/share URLs without downloading media."""

    try:
        response = requests.get(
            url,
            allow_redirects=True,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/151.0 Safari/537.36"
                ),
            },
        )

        response.raise_for_status()

        return response.url

    except Exception:
        return url

def _get_tiktok_item_id(
    url: str,
) -> str | None:
    match = re.search(
        r"/(?:photo|video)/"
        r"(?P<id>\d+)",
        url,
        re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(
        "id"
    )  

def _resolve_tiktok_photo(
    url: str
) -> dict:
    """
    Resolve a TikTok photo/slideshow post.

    Strategy:
    1. Resolve the URL.
    2. Convert /photo/{id} to /video/{id}.
    3. Load the public TikTok page.
    4. Extract TikTok's embedded item data.
    5. Read image_post_info.images.
    """

    resolved_url = resolve_tiktok_url(
        url
    )

    item_id = _get_tiktok_item_id(
        resolved_url
    )

    if not item_id:
        raise ValueError(
            "Could not determine the TikTok photo ID."
        )

    video_url = re.sub(
        r"/photo/(\d+)",
        r"/video/\1",
        resolved_url,
        flags=re.IGNORECASE,
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/151.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }

    session = requests.Session()

    try:
        response = session.get(
            video_url,
            headers=headers,
            timeout=30,
            allow_redirects=True,
        )

        response.raise_for_status()

        text = response.text

        # ---------------------------------------------------------------
        # Search all likely JSON blobs containing the TikTok item.
        # ---------------------------------------------------------------

        json_candidates = []

        patterns = [
            r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"'
            r'[^>]*>(.*?)</script>',

            r'<script[^>]+id="SIGI_STATE"'
            r'[^>]*>(.*?)</script>',
        ]

        for pattern in patterns:
            matches = re.findall(
                pattern,
                text,
                flags=re.DOTALL | re.IGNORECASE,
            )

            for raw_json in matches:
                raw_json = raw_json.strip()

                if raw_json:
                    try:
                        json_candidates.append(
                            json.loads(raw_json)
                        )
                    except json.JSONDecodeError:
                        continue

        # ---------------------------------------------------------------
        # Recursively search the JSON for the matching aweme/item.
        # ---------------------------------------------------------------

        def find_item(obj):
            if isinstance(
                obj,
                dict,
            ):
                # Direct aweme item.
                obj_id = (
                    obj.get("aweme_id")
                    or obj.get("id")
                )

                if (
                    obj_id
                    and str(obj_id) == str(item_id)
                ):
                    return obj

                # Search nested dictionaries.
                for value in obj.values():
                    found = find_item(
                        value
                    )

                    if found is not None:
                        return found

            elif isinstance(
                obj,
                list,
            ):
                for value in obj:
                    found = find_item(
                        value
                    )

                    if found is not None:
                        return found

            return None

        item = None

        for candidate in json_candidates:
            item = find_item(
                candidate
            )

            if item is not None:
                break

        # ---------------------------------------------------------------
        # Extract slideshow images.
        # ---------------------------------------------------------------

        if item:
            image_post_info = (
                item.get("image_post_info")
                or item.get("imagePostInfo")
                or item.get("imagePost")
                or {}
            )

            images = (
                image_post_info.get("images")
                or []
            )

            image_urls = []

            for image in images:
                if not isinstance(
                    image,
                    dict,
                ):
                    continue

                image_url = (
                    image.get("display_image")
                    or image.get("displayImage")
                    or image.get("imageURL")
                    or image.get("imageUrl")
                    or {}
                )

                if isinstance(
                    image_url,
                    dict,
                ):
                    urls = (
                        image_url.get("url_list")
                        or image_url.get("urlList")
                        or []
                    )

                    if urls:
                        image_urls.append(
                            urls[-1]
                        )

                elif isinstance(
                    image_url,
                    str,
                ):
                    image_urls.append(
                        image_url
                    )

            image_urls = list(
                dict.fromkeys(
                    image_urls
                )
            )

            if image_urls:
                author = (
                    item.get("author")
                    or {}
                )

                return {
                    "id": (
                        item.get("aweme_id")
                        or item.get("id")
                        or item_id
                    ),
                    "url": resolved_url,
                    "title": (
                        item.get("desc")
                        or ""
                    ),
                    "username": (
                        author.get("unique_id")
                        or author.get("uniqueId")
                        or ""
                    ),
                    "images": image_urls,
                    "item": item,
                }

    except Exception as e:
        logger.warning(
            "TikTok webpage photo resolver failed: %s",
            e,
        )

    raise ValueError(
        "Could not extract photos from this TikTok post."
    )

def _download_tiktok_photo_image(
    image_url: str,
    item_id: str,
    index: int,
) -> str:
    """Download one TikTok slideshow image."""

    response = requests.get(
        image_url,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/151.0 Safari/537.36"
            ),
            "Referer": "https://www.tiktok.com/",
        },
    )

    response.raise_for_status()

    content_type = (
        response.headers.get(
            "Content-Type",
            "",
        ).lower()
    )

    if "png" in content_type:
        extension = "png"
    elif "webp" in content_type:
        extension = "webp"
    else:
        extension = "jpg"

    filename = (
        f"{item_id}_{index}.{extension}"
    )

    filepath = os.path.join(
        DOWNLOAD_DIR,
        filename,
    )

    with open(
        filepath,
        "wb",
    ) as file:
        file.write(
            response.content
        )

    return filepath

def download_tiktok_photo(
    url: str,
    progress_hook=None,
):
    """
    Download every image in a TikTok photo/slideshow post.

    Returns the same general structure used by the bot:
        info, entries, filepaths
    """

    os.makedirs(
        DOWNLOAD_DIR,
        exist_ok=True,
    )

    resolved = _resolve_tiktok_photo(
        url
    )

    image_urls = (
        resolved.get("images")
        or []
    )

    if not image_urls:
        raise ValueError(
            "No images were found in this TikTok photo post."
        )

    filepaths = []
    entries = []

    for index, image_url in enumerate(
        image_urls,
        start=1,
    ):
        filepath = (
            _download_tiktok_photo_image(
                image_url,
                resolved["id"],
                index,
            )
        )

        filepaths.append(
            filepath
        )

        entries.append(
            {
                "id": (
                    f"{resolved['id']}_{index}"
                ),
                "extractor": "TikTok",
                "extractor_key": "TikTok",
                "url": image_url,
                "title": resolved.get(
                    "title",
                    "",
                ),
                "uploader": resolved.get(
                    "username",
                    "",
                ),
                "channel": resolved.get(
                    "username",
                    "",
                ),
                "thumbnail": image_url,
            }
        )

        if progress_hook:
            progress_hook(
                {
                    "status": "downloading",
                    "_percent_str": f"{round((index / len(image_urls)) * 100)}%",
                    "_eta_str": "N/A",
                    "_total_bytes_str": "N/A",
                }
            )

    info = {
        "id": resolved["id"],
        "extractor": "TikTok",
        "extractor_key": "TikTok",
        "webpage_url": resolved["url"],
        "title": resolved.get(
            "title",
            "",
        ),
        "uploader": resolved.get(
            "username",
            "",
        ),
        "channel": resolved.get(
            "username",
            "",
        ),
        "thumbnail": (
            image_urls[0]
            if image_urls
            else None
        ),
    }

    return (
        info,
        entries,
        filepaths,
    )



def _is_instagram_story_url(
    url: str,
) -> bool:
    return bool(
        re.search(
            r"https?://(?:www\.)?instagram\.com/"
            r"stories/[^/?#]+/\d+",
            url,
            re.IGNORECASE,
        )
    )

def _instagram_extra_opts() -> dict:
    """
    Instagram-specific authentication.

    Prefer the persistent browser profile when configured.
    Fall back to the legacy cookie file otherwise.
    """

    opts = {}

    browser_spec = _browser_cookie_source(
        "INSTAGRAM"
    )

    if browser_spec:
        opts["cookiesfrombrowser"] = browser_spec
    else:
        cookiefile = os.environ.get(
            "INSTAGRAM_COOKIE_FILE",
            "instagram_cookies.txt",
        )

        if cookiefile and os.path.exists(cookiefile):
            opts["cookiefile"] = cookiefile

    return opts

def _get_instagram_view_count(*objects):
    """
    Return Instagram's real Reel view count from any metadata shape
    returned by yt-dlp.

    Instagram/yt-dlp can expose the count as:
        view_count
        video_view_count

    Metadata can also be nested inside another dictionary, so this
    function searches recursively.

    Only positive integer counts are accepted here so a stale/empty
    zero cannot overwrite a real value found elsewhere.
    """

    def _search(obj):
        if obj is None:
            return None

        if isinstance(obj, dict):
            # Prefer the normalized yt-dlp field.
            for key in (
                "view_count",
                "video_view_count",
            ):
                value = obj.get(key)

                if value is None or isinstance(value, bool):
                    continue

                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue

                if value > 0:
                    return value

            # Some Instagram responses keep the media object nested.
            for value in obj.values():
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

def _instagram_clip_play_count(
    ydl,
    entry: dict,
) -> int | None:
    """
    Recover an Instagram Reel's play count using Instagram's
    Clips GraphQL connection.

    Instagram's current post metadata endpoint can return
    no view_count/video_view_count for public Reels. The Clips
    connection can still expose the Reel's play_count.

    This follows the current Instaloader fallback strategy.
    """

    if not isinstance(entry, dict):
        return None

    extractor = str(
        entry.get("extractor_key")
        or entry.get("extractor")
        or ""
    ).lower()

    if "instagram" not in extractor:
        return None

    shortcode = (
        entry.get("id")
        or entry.get("shortcode")
    )

    if not shortcode:
        logger.warning(
            "Instagram play-count fallback: no shortcode found."
        )
        return None

    user_id = (
        entry.get("uploader_id")
        or entry.get("channel_id")
    )

    if not user_id:
        logger.warning(
            "Instagram play-count fallback: no user id "
            "for shortcode=%s",
            shortcode,
        )
        return None

    try:
        # ---------------------------------------------------------------
        # Find the CSRF token from yt-dlp's existing cookie jar.
        # The same yt-dlp opener contains the Instagram cookies loaded
        # from INSTAGRAM_COOKIE_FILE / cookiesfrombrowser.
        # ---------------------------------------------------------------

        csrf_token = None
        cookie_jar = None

        try:
            for handler in ydl._opener.handlers:
                if hasattr(handler, "cookiejar"):
                    cookie_jar = handler.cookiejar
                    break
        except Exception:
            cookie_jar = None

        if cookie_jar is not None:
            for cookie in cookie_jar:
                if (
                    cookie.name == "csrftoken"
                    and cookie.value
                    and "instagram.com" in (
                        cookie.domain or ""
                    )
                ):
                    csrf_token = cookie.value
                    break

        # Instagram's current GraphQL endpoint requires a CSRF token.
        # If the cookie jar does not already contain one, visit the
        # Instagram homepage once using the same authenticated opener.
        if not csrf_token:
            try:
                bootstrap_request = urllib.request.Request(
                    "https://www.instagram.com/",
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (X11; Linux x86_64) "
                            "AppleWebKit/537.36 "
                            "(KHTML, like Gecko) "
                            "Chrome/151.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )

                ydl._opener.open(
                    bootstrap_request,
                    timeout=30,
                ).read(1)

            except Exception as bootstrap_error:
                logger.warning(
                    "Instagram CSRF bootstrap failed for %s: %s",
                    shortcode,
                    bootstrap_error,
                )

            if cookie_jar is not None:
                for cookie in cookie_jar:
                    if (
                        cookie.name == "csrftoken"
                        and cookie.value
                        and "instagram.com" in (
                            cookie.domain or ""
                        )
                    ):
                        csrf_token = cookie.value
                        break

        if not csrf_token:
            logger.warning(
                "Instagram play-count fallback: no csrftoken "
                "available for shortcode=%s",
                shortcode,
            )
            return None

        # ---------------------------------------------------------------
        # Current Instagram Clips GraphQL query.
        #
        # This is the same query currently used by Instaloader.
        # ---------------------------------------------------------------

        variables = {
            "data": {
                "include_feed_video": True,
                "page_size": 12,
                "target_user_id": str(user_id),
            }
        }

        post_data = urllib.parse.urlencode(
            {
                "variables": json.dumps(
                    variables,
                    separators=(",", ":"),
                ),
                "doc_id": "27234427476213202",
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            "https://www.instagram.com/graphql/query",
            data=post_data,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Content-Type": (
                    "application/x-www-form-urlencoded"
                ),
                "X-CSRFToken": csrf_token,
                "Referer": (
                    f"https://www.instagram.com/reel/"
                    f"{shortcode}/"
                ),
            },
            method="POST",
        )

        response = ydl._opener.open(
            request,
            timeout=30,
        )

        raw_response = response.read()

        payload = json.loads(
            raw_response.decode("utf-8")
        )

        # ---------------------------------------------------------------
        # Extract the Clips connection.
        # ---------------------------------------------------------------

        clips_connection = (
            (payload.get("data") or {})
            .get(
                "xdt_api__v1__clips__user__connection_v2"
            )
            or {}
        )

        edges = (
            clips_connection.get("edges")
            or []
        )

        logger.info(
            "Instagram Clips query: shortcode=%s "
            "user_id=%s returned_edges=%d",
            shortcode,
            user_id,
            len(edges),
        )

        # ---------------------------------------------------------------
        # Find our Reel by its shortcode.
        # ---------------------------------------------------------------

        for edge in edges:

            node = (
                edge.get("node")
                or {}
            )

            media = (
                node.get("media")
                or {}
            )

            media_code = (
                media.get("code")
                or ""
            )

            if str(media_code) != str(shortcode):
                continue

            play_count = media.get(
                "play_count"
            )

            logger.info(
                "Instagram Clips matched Reel: "
                "shortcode=%s play_count=%r",
                shortcode,
                play_count,
            )

            if play_count is None:
                return None

            try:
                play_count = int(
                    play_count
                )
            except (
                TypeError,
                ValueError,
            ):
                return None

            if play_count > 0:
                logger.info(
                    "Instagram Reel play count recovered: "
                    "shortcode=%s user_id=%s count=%s",
                    shortcode,
                    user_id,
                    play_count,
                )

                return play_count

            return None

        logger.warning(
            "Instagram Clips response did not contain "
            "shortcode=%s for user_id=%s",
            shortcode,
            user_id,
        )

    except Exception as e:
        logger.exception(
            "Instagram Reel play-count fallback failed "
            "for shortcode=%s: %s",
            shortcode,
            e,
        )

    return None

def _extract_resilient(
    ydl_opts_base: dict,
    target: str,
    download: bool,
    process: bool = True,
    use_proxy: bool = True,
):
    """
    Lightweight yt-dlp extraction with platform-specific
    authentication.

    Logs each YouTube client attempt and its duration so
    slow extraction/retry behavior can be distinguished
    from actual file-transfer or FFmpeg time.
    """

    last_error = None

    is_youtube = _is_youtube_url(
        target
    )

    is_instagram = (
        "instagram.com" in target.lower()
    )

    if is_youtube:
        attempts = _client_attempts()
    else:
        attempts = [None]

    for attempt_number, clients in enumerate(
        attempts,
        start=1,
    ):

        started = time.monotonic()

        opts = dict(
            ydl_opts_base
        )

        if is_youtube and clients:
            opts.update(
                _youtube_extra_opts(
                    clients
                )
            )

        elif is_instagram:
            opts.update(
                _instagram_extra_opts()
            )

        if use_proxy:
            proxy = _get_random_proxy()

            if proxy:
                opts["proxy"] = proxy

        logger.info(
            "yt-dlp attempt %d/%d started | youtube=%s | clients=%s | target=%s",
            attempt_number,
            len(attempts),
            is_youtube,
            clients,
            target,
        )

        try:

            with yt_dlp.YoutubeDL(
                opts
            ) as ydl:

                info = ydl.extract_info(
                    target,
                    download=download,
                    process=process,
                )

            elapsed = (
                time.monotonic()
                - started
            )

            logger.info(
                "yt-dlp attempt %d/%d succeeded | clients=%s | elapsed=%.2fs | download=%s",
                attempt_number,
                len(attempts),
                clients,
                elapsed,
                download,
            )

            return info, ydl

        except Exception as e:

            elapsed = (
                time.monotonic()
                - started
            )

            last_error = e

            retryable = (
                is_youtube
                and any(
                    hint in str(e).lower()
                    for hint in _RETRYABLE_ERROR_HINTS
                )
            )

            if retryable:

                logger.warning(
                    "yt-dlp attempt %d/%d failed; retrying | clients=%s | elapsed=%.2fs | error=%s",
                    attempt_number,
                    len(attempts),
                    clients,
                    elapsed,
                    str(e)[:500],
                )

                continue

            logger.error(
                "yt-dlp attempt %d/%d failed permanently | clients=%s | elapsed=%.2fs | error=%s",
                attempt_number,
                len(attempts),
                clients,
                elapsed,
                str(e)[:500],
            )

            raise

    raise last_error

def _probe_size(url: str, format_selector: str):
    """
    Probe the exact yt-dlp format selector that will be downloaded.

    For merged video+audio formats, sum the sizes of the exact
    requested video/audio streams rather than estimating them from
    unrelated formats.
    """

    probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "noplaylist": False,
        "format": format_selector,
        "ignoreerrors": False,
    }

    ffmpeg_location = _ffmpeg_location()

    if ffmpeg_location:
        probe_opts["ffmpeg_location"] = ffmpeg_location

    info, _ = _extract_resilient(
        probe_opts,
        url,
        download=False,
        process=True,
    )

    if not info:
        raise Exception("Media info not found.")

    entries = info.get("entries") or [info]

    total = 0
    all_known = True

    for entry in entries:

        if not entry:
            continue

        # When yt-dlp selected separate video + audio streams,
        # they are exposed here.
        requested_formats = entry.get("requested_formats") or []

        if requested_formats:
            entry_total = 0
            entry_known = True

            for fmt in requested_formats:

                size = (
                    fmt.get("filesize")
                    or fmt.get("filesize_approx")
                )

                if size:
                    entry_total += int(size)
                    continue

                # If filesize metadata isn't available, estimate
                # this EXACT selected stream from its bitrate.
                duration = (
                    entry.get("duration")
                    or info.get("duration")
                    or 0
                )

                bitrate = (
                    fmt.get("vbr")
                    or fmt.get("abr")
                    or fmt.get("tbr")
                )

                if bitrate and duration:
                    entry_total += int(
                        float(bitrate) * 1000 / 8 * duration
                    )
                else:
                    entry_known = False

            if entry_known and entry_total > 0:
                total += entry_total
            else:
                all_known = False

            continue

        # Progressive format: one selected file.
        size = (
            entry.get("filesize")
            or entry.get("filesize_approx")
        )

        if size:
            total += int(size)
            continue

        duration = (
            entry.get("duration")
            or info.get("duration")
            or 0
        )

        bitrate = (
            entry.get("tbr")
            or entry.get("vbr")
            or entry.get("abr")
        )

        if bitrate and duration:
            total += int(
                float(bitrate) * 1000 / 8 * duration
            )
        else:
            all_known = False

    if total > MAX_TELEGRAM_BYTES:
        raise FileTooLargeError(format_size(total))

    if total <= 0:
        return None

    # Return the real/estimated size of the exact selector.
    return total


def _is_probably_photo_entry(info_raw: dict, raw_entry: dict) -> bool:
    """True only for Instagram-style carousel items that are stills, not
    videos. Deliberately scoped to Instagram and to entries with no video
    codec/format list at all — this used to be a catch-all for ANY
    processing failure on ANY platform, which is what made a failed YouTube
    video download silently turn into "here's the thumbnail instead." A
    real download failure should be retried, never quietly swapped for an
    image."""
    extractor = (raw_entry.get("extractor") or info_raw.get("extractor") or "").lower()
    extractor_key = (raw_entry.get("extractor_key") or info_raw.get("extractor_key") or "").lower()
    if "instagram" not in extractor and "instagram" not in extractor_key:
        return False
    if raw_entry.get("vcodec") not in (None, "none"):
        return False
    if raw_entry.get("formats"):
        return False
    return bool(raw_entry.get("url") or raw_entry.get("thumbnails"))


def _download_image_entry(raw_entry: dict) -> str | None:
    img_url = raw_entry.get("url")
    if not img_url and raw_entry.get("thumbnails"):
        img_url = raw_entry["thumbnails"][-1]["url"]
    if not img_url:
        return None

    ext = "jpg"
    if ".png" in img_url:
        ext = "png"
    elif ".webp" in img_url:
        ext = "webp"

    safe_id = raw_entry.get("id") or uuid.uuid4().hex[:8]
    path = f"{DOWNLOAD_DIR}/{safe_id}.{ext}"
    import urllib.request
    urllib.request.urlretrieve(img_url, path)
    return path

def _ensure_h264_mp4(
    filepath: str,
    ffmpeg_location: str | None = None,
) -> str:
    """
    Prepare an MP4 for Telegram/iPhone streaming.

    Cases:

    1. H.264 video + AAC audio:
       -> stream-copy both and apply faststart.

    2. H.264 video + non-AAC audio:
       -> copy video, convert audio to AAC, apply faststart.

    3. Non-H.264 video:
       -> convert video to H.264 + audio to AAC
       -> apply faststart in THE SAME FFmpeg pass.

    This deliberately avoids the old:
        transcode -> second faststart
    pipeline.
    """

    if not filepath:
        return filepath

    if not os.path.exists(filepath):
        return filepath

    if not filepath.lower().endswith(".mp4"):
        return filepath

    ffmpeg_exe = None
    ffprobe_exe = None

    # ---------------------------------------------------------------
    # Resolve ffmpeg / ffprobe
    # ---------------------------------------------------------------

    if ffmpeg_location:

        ffmpeg_candidate = os.path.join(
            ffmpeg_location,
            "ffmpeg.exe"
            if os.name == "nt"
            else "ffmpeg",
        )

        ffprobe_candidate = os.path.join(
            ffmpeg_location,
            "ffprobe.exe"
            if os.name == "nt"
            else "ffprobe",
        )

        if os.path.isfile(ffmpeg_candidate):
            ffmpeg_exe = ffmpeg_candidate

        if os.path.isfile(ffprobe_candidate):
            ffprobe_exe = ffprobe_candidate

    if not ffmpeg_exe:
        ffmpeg_exe = shutil.which("ffmpeg")

    if not ffprobe_exe:
        ffprobe_exe = shutil.which("ffprobe")

    if not ffmpeg_exe:
        logger.warning(
            "ffmpeg not found; MP4 preparation skipped: %s",
            filepath,
        )
        return filepath

    if not ffprobe_exe:
        logger.warning(
            "ffprobe not found; MP4 codec inspection skipped: %s",
            filepath,
        )
        return filepath

    # ---------------------------------------------------------------
    # Inspect codecs once
    # ---------------------------------------------------------------

    try:

        result = subprocess.run(
            [
                ffprobe_exe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                filepath,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )

        probe_data = json.loads(
            result.stdout or "{}"
        )

    except Exception as e:

        logger.warning(
            "Could not inspect MP4 codecs for %s: %s",
            filepath,
            e,
        )

        return filepath

    streams = probe_data.get("streams") or []

    video_stream = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
        ),
        None,
    )

    audio_stream = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "audio"
        ),
        None,
    )

    video_codec = (
        (video_stream or {}).get("codec_name")
        or ""
    ).lower()

    audio_codec = (
        (audio_stream or {}).get("codec_name")
        or ""
    ).lower()

    # ---------------------------------------------------------------
    # Decide whether transcoding is actually necessary
    # ---------------------------------------------------------------

    video_is_h264 = video_codec == "h264"
    audio_is_aac = audio_codec in {
        "aac",
        "mp4a",
    }

    temp_path = filepath + ".telegram.mp4"

    try:

        # -----------------------------------------------------------
        # CASE 1:
        # Already H.264 + AAC
        #
        # No re-encoding at all.
        # One FFmpeg pass only to put moov at the front.
        # -----------------------------------------------------------

        if video_is_h264 and audio_is_aac:

            command = [
                ffmpeg_exe,
                "-y",
                "-i",
                filepath,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                temp_path,
            ]

        # -----------------------------------------------------------
        # CASE 2:
        # H.264 video but audio is not AAC.
        #
        # Keep the video untouched.
        # Only convert audio.
        # -----------------------------------------------------------

        elif video_is_h264:

            command = [
                ffmpeg_exe,
                "-y",
                "-i",
                filepath,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                temp_path,
            ]

        # -----------------------------------------------------------
        # CASE 3:
        # VP9 / AV1 / anything else.
        #
        # This is the expensive case.
        # It happens only when YouTube did not provide a suitable
        # H.264 stream for the selected resolution.
        # -----------------------------------------------------------

        else:

            command = [
                ffmpeg_exe,
                "-y",
                "-i",
                filepath,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "2",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                temp_path,
            ]

            logger.info(
                "H.264 transcode required for %s (source codec: %s)",
                filepath,
                video_codec or "unknown",
            )

        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        os.replace(
            temp_path,
            filepath,
        )

    except Exception as e:

        logger.warning(
            "MP4 Telegram preparation failed for %s: %s",
            filepath,
            e,
        )

        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    return filepath

def _download_with_selector(
    url: str,
    format_selector: str,
    extract_audio: bool,
    progress_hook=None,
    use_proxy: bool = False,
    youtube_client_attempts=None,
    youtube_use_browser_cookies: bool = True,
):
    """
    Download media with yt-dlp.

    Video:
        - Prefer MP4 output
        - Merge video/audio
        - Move MP4 metadata (moov atom) to the front with faststart

    Audio:
        - Convert to MP3
        - Embed yt-dlp metadata
        - Embed downloaded thumbnail
    """

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    is_instagram_story = bool(
        re.search(
            r"https?://(?:www\.)?instagram\.com/"
            r"stories/[^/?#]+/\d+",
            url,
            re.IGNORECASE,
        )
    )

    ydl_opts = {
        "format": format_selector,
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "color": "never",
        "noplaylist": is_instagram_story,
        "socket_timeout": 30,
    }

    # Use the correct authentication source for each platform.
    if "instagram.com" in url.lower():

        instagram_opts = (
            _instagram_extra_opts()
        )
        ydl_opts.update(
            instagram_opts
        )

    elif _is_youtube_url(url):
        pass

    else:

        cookiefile = os.environ.get(
            "YOUTUBE_COOKIE_FILE",
            "cookies.txt",
        )

        if (
            cookiefile
            and os.path.exists(
                cookiefile
            )
        ):
            ydl_opts["cookiefile"] = (
                cookiefile
            )

        # For YouTube video downloads, prefer:
    #
    #   H.264 video
    #   M4A/AAC audio
    #
    # but DO NOT require them.
    #
    # This preserves 1440p/2160p options when YouTube only
    # offers VP9/AV1 at those resolutions.
    if _is_youtube_url(url) and not extract_audio:
        ydl_opts["format_sort"] = [
            "res",
            "fps",
            "codec:avc:m4a",
            "size",
        ]

    if extract_audio:
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            },
        ]
    ffmpeg_location = _ffmpeg_location()

    if ffmpeg_location:
        ydl_opts["ffmpeg_location"] = ffmpeg_location

    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    
    def _attempt(ydl):
        info_raw = ydl.extract_info(
            url,
            download=False,
            process=False,
        )

        if not info_raw:
            raise Exception(
                "Media info not found."
            )

        entries_raw = (
            info_raw.get(
                "entries"
            )
            or [info_raw]
        )

        filepaths = []
        valid_entries = []

        for raw_entry in entries_raw:
            if not raw_entry:
                continue

            for key in (
                "extractor",
                "extractor_key",
                "webpage_url",
            ):
                if (
                    key not in raw_entry
                    and key in info_raw
                ):
                    raw_entry[key] = info_raw[key]

            # -----------------------------------------------------------
            # Photo entry
            # -----------------------------------------------------------
            if _is_probably_photo_entry(
                info_raw,
                raw_entry,
            ):
                path = _download_image_entry(
                    raw_entry
                )

                if path:
                    if not os.path.exists(
                        path
                    ):
                        raise Exception(
                            f"downloaded image does not exist: {path}"
                        )

                    actual_size = (
                        os.path.getsize(path)
                    )

                    if (
                        actual_size
                        < MIN_VALID_FILE_BYTES
                    ):
                        try:
                            os.remove(path)
                        except OSError:
                            pass

                        raise Exception(
                            f"incomplete download: {path}"
                        )

                    if (
                        actual_size
                        > MAX_TELEGRAM_BYTES
                    ):
                        try:
                            os.remove(path)
                        except OSError:
                            pass

                        raise FileTooLargeError(
                            format_size(
                                actual_size
                            )
                        )

                    filepaths.append(
                        path
                    )

                    metadata_entry = dict(
                        raw_entry
                    )

                    valid_entries.append(
                        metadata_entry
                    )

                continue

            # -----------------------------------------------------------
            # Video / audio entry
            # -----------------------------------------------------------
            entry_id = raw_entry.get(
                "id"
            )

            processed = (
                ydl.process_ie_result(
                    raw_entry,
                    download=True,
                )
            )

            if not processed:
                continue

            # -----------------------------------------------------------
            # Preserve metadata that yt-dlp may expose on the original
            # Instagram result but not on the processed media result.
            # -----------------------------------------------------------
            if isinstance(
                processed,
                dict,
            ):
                for key in (
                    "id",
                    "title",
                    "description",
                    "thumbnail",
                    "timestamp",
                    "upload_date",
                    "uploader",
                    "uploader_id",
                    "channel",
                    "channel_id",
                    "view_count",
                    "like_count",
                    "comment_count",
                    "duration",
                    "width",
                    "height",
                    "webpage_url",
                    "extractor",
                    "extractor_key",
                ):
                    if (
                        processed.get(
                            key
                        ) is None
                        and raw_entry.get(
                            key
                        ) is not None
                    ):
                        processed[key] = (
                            raw_entry[key]
                        )

                # Some Instagram extraction paths use
                # video_view_count internally.
                if (
                    processed.get(
                        "view_count"
                    ) is None
                    and raw_entry.get(
                        "video_view_count"
                    ) is not None
                ):
                    processed[
                        "view_count"
                    ] = raw_entry[
                        "video_view_count"
                    ]

            raw_path = (
                ydl.prepare_filename(
                    processed
                )
            )

            if extract_audio:
                mp3_path = (
                    os.path.splitext(
                        raw_path
                    )[0]
                    + ".mp3"
                )

                path = (
                    mp3_path
                    if os.path.exists(
                        mp3_path
                    )
                    else raw_path
                )

            elif (
                _is_youtube_url(url)
                and format_selector.startswith(
                    "bestaudio"
                )
            ):
                path = raw_path

            else:
                # After yt-dlp merges video + audio,
                # locate the final MP4 explicitly.
                mp4_candidates = []

                if (
                    entry_id
                    and os.path.isdir(
                        DOWNLOAD_DIR
                    )
                ):
                    prefix = (
                        f"{entry_id}."
                    )

                    for name in os.listdir(
                        DOWNLOAD_DIR
                    ):
                        if (
                            name.startswith(
                                prefix
                            )
                            and name.lower().endswith(
                                ".mp4"
                            )
                        ):
                            mp4_candidates.append(
                                os.path.join(
                                    DOWNLOAD_DIR,
                                    name,
                                )
                            )

                if mp4_candidates:
                    path = max(
                        mp4_candidates,
                        key=os.path.getmtime,
                    )
                else:
                    path = raw_path

                path = _ensure_h264_mp4(
                    path,
                    ffmpeg_location,
                )

            if not os.path.exists(
                path
            ):
                _cleanup_id(
                    entry_id
                )

                raise Exception(
                    f"downloaded file does not exist: {path}"
                )

            actual_size = (
                os.path.getsize(
                    path
                )
            )

            if (
                actual_size
                < MIN_VALID_FILE_BYTES
            ):
                _cleanup_id(
                    entry_id
                )

                raise Exception(
                    f"incomplete download: {path}"
                )

            if (
                actual_size
                > MAX_TELEGRAM_BYTES
            ):
                _cleanup_id(
                    entry_id
                )

                raise FileTooLargeError(
                    format_size(
                        actual_size
                    )
                )

            # IMPORTANT:
            # Append every downloaded entry immediately.
            filepaths.append(
                path
            )

            # -----------------------------------------------------------
            # Build metadata for THIS entry.
            # -----------------------------------------------------------
            metadata_entry = dict(
                raw_entry
            )

            if isinstance(
                processed,
                dict,
            ):
                metadata_entry.update(
                    processed
                )

            extractor_name = str(
                metadata_entry.get(
                    "extractor_key"
                )
                or metadata_entry.get(
                    "extractor"
                )
                or raw_entry.get(
                    "extractor_key"
                )
                or raw_entry.get(
                    "extractor"
                )
                or ""
            ).lower()

            # -----------------------------------------------------------
            # Instagram Reel view/play count recovery.
            # -----------------------------------------------------------
            if (
                "instagram" in extractor_name
                and not _is_instagram_story_url(
                    url
                )
            ):
                logger.info(
                    "Instagram metadata before recovery: "
                    "id=%s extractor=%s "
                    "view_count=%r "
                    "video_view_count=%r "
                    "video_play_count=%r "
                    "uploader_id=%r "
                    "channel_id=%r",
                    metadata_entry.get(
                        "id"
                    ),
                    extractor_name,
                    metadata_entry.get(
                        "view_count"
                    ),
                    metadata_entry.get(
                        "video_view_count"
                    ),
                    metadata_entry.get(
                        "video_play_count"
                    ),
                    metadata_entry.get(
                        "uploader_id"
                    ),
                    metadata_entry.get(
                        "channel_id"
                    ),
                )

                instagram_views = (
                    _get_instagram_view_count(
                        processed,
                        raw_entry,
                        info_raw,
                    )
                )

                if instagram_views is not None:
                    metadata_entry[
                        "view_count"
                    ] = (
                        instagram_views
                    )

                    metadata_entry[
                        "video_view_count"
                    ] = (
                        instagram_views
                    )

                else:
                    instagram_play_count = (
                        _instagram_clip_play_count(
                            ydl,
                            metadata_entry,
                        )
                    )

                    if (
                        instagram_play_count
                        is not None
                    ):
                        metadata_entry[
                            "view_count"
                        ] = (
                            instagram_play_count
                        )

                        metadata_entry[
                            "video_view_count"
                        ] = (
                            instagram_play_count
                        )

                        metadata_entry[
                            "video_play_count"
                        ] = (
                            instagram_play_count
                        )

                        metadata_entry[
                            "play_count"
                        ] = (
                            instagram_play_count
                        )

                        logger.info(
                            "Instagram final view count: "
                            "id=%s count=%s",
                            metadata_entry.get(
                                "id"
                            ),
                            instagram_play_count,
                        )

                    else:
                        logger.warning(
                            "Instagram view count could not "
                            "be recovered: id=%s",
                            metadata_entry.get(
                                "id"
                            ),
                        )

            valid_entries.append(
                metadata_entry
            )

        if not filepaths:
            raise Exception(
                "Nothing could be downloaded from this link."
            )

        return (
            info_raw,
            valid_entries,
            filepaths,
        )

    last_error = None

    # ---------------------------------------------------------------
    # Use platform-specific authentication.
    # YouTube gets YouTube client/cookie settings.
    # Instagram gets Instagram cookie settings.
    # Other platforms get the base options only.
    # ---------------------------------------------------------------

    if _is_youtube_url(url):
        attempts = (
            youtube_client_attempts
            if youtube_client_attempts is not None
            else _client_attempts()
        )
    else:
        attempts = [None]

    for clients in attempts:

        opts = dict(ydl_opts)

        if _is_youtube_url(url) and clients:
            if youtube_use_browser_cookies:
                opts.update(
                    _youtube_extra_opts(
                        clients
                    )
                )
            else:
                extractor_args = dict(
                    opts.get(
                        "extractor_args"
                    )
                    or {}
                )

                youtube_args = dict(
                    extractor_args.get(
                        "youtube"
                    )
                    or {}
                )

                youtube_args[
                    "player_client"
                ] = clients

                extractor_args[
                    "youtube"
                ] = youtube_args

                opts[
                    "extractor_args"
                ] = extractor_args

        elif "instagram.com" in url.lower():
            opts.update(
                _instagram_extra_opts()
            )

        # -----------------------------------------------------------
        # IMPORTANT:
        # Do NOT use the rotating proxy for authenticated Instagram
        # sessions. A changing exit IP can invalidate or confuse
        # Instagram's session/authentication checks.
        # -----------------------------------------------------------

        if (
            use_proxy
            and "instagram.com" not in url.lower()
        ):
            proxy = _get_random_proxy()

            if proxy:
                opts["proxy"] = proxy

        attempt_number = attempts.index(clients) + 1
        started = time.monotonic()

        logger.info(
            "YouTube quality download attempt %d/%d started | clients=%s | selector=%s | extract_audio=%s",
            attempt_number,
            len(attempts),
            clients,
            format_selector,
            extract_audio,
        )

        try:

            with yt_dlp.YoutubeDL(opts) as ydl:
                result = _attempt(ydl)

            elapsed = (
                time.monotonic()
                - started
            )

            logger.info(
                "YouTube quality download attempt %d/%d succeeded | clients=%s | elapsed=%.2fs",
                attempt_number,
                len(attempts),
                clients,
                elapsed,
            )

            return result

        except Exception as e:

            elapsed = (
                time.monotonic()
                - started
            )

            retryable = (
                _is_youtube_url(url)
                and any(
                    hint in str(e).lower()
                    for hint in _RETRYABLE_ERROR_HINTS
                )
            )

            if retryable:
                logger.warning(
                    "YouTube quality download attempt %d/%d failed AFTER/AROUND download; retrying | clients=%s | elapsed=%.2fs | error=%s",
                    attempt_number,
                    len(attempts),
                    clients,
                    elapsed,
                    str(e)[:500],
                )

                last_error = e
                continue

            logger.error(
                "YouTube quality download attempt %d/%d failed permanently | clients=%s | elapsed=%.2fs | error=%s",
                attempt_number,
                len(attempts),
                clients,
                elapsed,
                str(e)[:500],
            )

            raise

    raise last_error

def _format_for(url: str, tier: str) -> str:
    table = YOUTUBE_QUALITY_FORMATS if _is_youtube_url(url) else QUALITY_FORMATS
    return table[tier]

def download_direct(
    url: str,
    quality: str = "best",
    allow_fallback: bool = False,
    progress_hook=None,
):
    """
    Download media from Instagram / YouTube / SoundCloud / TikTok.

    TikTok photo/slideshow posts are handled separately because
    yt-dlp does not currently extract TikTok /photo/ URLs directly.

    Size is checked AFTER the real file exists instead of performing
    another yt-dlp extraction beforehand.
    """

    os.makedirs(
        DOWNLOAD_DIR,
        exist_ok=True,
    )

    is_soundcloud = (
        "soundcloud.com" in url.lower()
    )

    # ---------------------------------------------------------------
    # TikTok
    # ---------------------------------------------------------------

    resolved_url = url

    if "tiktok.com" in url.lower():
        resolved_url = resolve_tiktok_url(
            url
        )

        if _is_tiktok_photo_url(
            resolved_url
        ):
            info, entries, filepaths = (
                download_tiktok_photo(
                    resolved_url,
                    progress_hook,
                )
            )

            return (
                info,
                entries,
                filepaths,
                "best",
            )

    # ---------------------------------------------------------------
    # SoundCloud
    # ---------------------------------------------------------------

    if is_soundcloud:

        fmt = "bestaudio/best"

        return _download_with_selector(
            url,
            fmt,
            True,
            progress_hook,
        ) + (
            "audio",
        )

    # ---------------------------------------------------------------
    # Normal direct-download path
    # ---------------------------------------------------------------

    # ---------------------------------------------------------------
    # Instagram-specific quality path
    # ---------------------------------------------------------------
    if "instagram.com" in url.lower():

        instagram_quality = (
            quality
            if quality in INSTAGRAM_QUALITY_FORMATS
            else "best"
        )

        fmt = INSTAGRAM_QUALITY_FORMATS[
            instagram_quality
        ]

        info, entries, filepaths = (
            _download_with_selector(
                url,
                fmt,
                False,
                progress_hook,
            )
        )

        return (
            info,
            entries,
            filepaths,
            instagram_quality,
        )

    start = (
        QUALITY_LADDER.index(
            quality
        )
        if quality in QUALITY_LADDER
        else 0
    )

    tiers_to_try = (
        QUALITY_LADDER[start:]
        if allow_fallback
        else [quality]
    )

    last_error = None

    for tier in tiers_to_try:

        fmt = _format_for(
            url,
            tier,
        )

        try:

            info, entries, filepaths = (
                _download_with_selector(
                    url,
                    fmt,
                    tier == "audio",
                    progress_hook,
                )
            )

            return (
                info,
                entries,
                filepaths,
                tier,
            )

        except FileTooLargeError as e:

            last_error = e
            continue

        except Exception as e:

            last_error = e
            continue

    raise last_error or Exception(
        "Download failed."
    )

# --- per-video YouTube quality picker (thumbnail + buttons) ---

def _bucket_youtube_formats(info: dict) -> list:
    """
    Build the YouTube quality buttons with a realistic size estimate.

    Priority:
    1. Exact filesize
    2. Approximate filesize
    3. Video bitrate (vbr) × duration
    4. Audio bitrate (abr) × duration

    The calculation is based on the same MP4/M4A types that the actual
    downloader prefers.
    """

    formats = info.get("formats") or []
    duration = info.get("duration") or 0

    video_formats = [
        f for f in formats
        if f.get("vcodec") not in (None, "none")
        and f.get("height")
    ]

    audio_formats = [
        f for f in formats
        if f.get("vcodec") in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]

    def estimate_size(fmt: dict) -> int:
        if not fmt:
            return 0

        exact = (
            fmt.get("filesize")
            or fmt.get("filesize_approx")
        )

        if exact:
            return int(exact)

        if duration:
            bitrate = (
                fmt.get("vbr")
                or fmt.get("abr")
                or fmt.get("tbr")
            )

            if bitrate:
                return int(
                    float(bitrate)
                    * 1000
                    / 8
                    * duration
                )

        return 0

    def _audio_format_has_usable_size(
        fmt: dict,
    ) -> bool:
        if not fmt:
            return False

        if (
            fmt.get("filesize")
            or fmt.get("filesize_approx")
        ):
            return True

        if duration and (
            fmt.get("abr")
            or fmt.get("tbr")
        ):
            return True

        return False

    # ---------------------------------------------------------------
    # Recover standalone audio when the primary video client only
    # exposes HLS/SABR audio entries without usable size metadata.
    # ---------------------------------------------------------------
    if not any(
        _audio_format_has_usable_size(fmt)
        for fmt in audio_formats
    ):
        try:
            audio_probe_opts = {
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 20,
                "noplaylist": True,
                "extractor_args": {
                    "youtube": {
                        "player_client": [
                            "default"
                        ]
                    }
                },
            }

            with yt_dlp.YoutubeDL(
                audio_probe_opts
            ) as audio_ydl:
                audio_info = audio_ydl.extract_info(
                    url,
                    download=False,
                    process=False,
                )

            fallback_formats = (
                audio_info.get("formats")
                or []
            )

            usable_fallback = [
                f
                for f in fallback_formats
                if (
                    f.get("vcodec")
                    in (None, "none")
                    and f.get("acodec")
                    not in (None, "none")
                    and (
                        f.get("filesize")
                        or f.get("filesize_approx")
                        or (
                            duration
                            and (
                                f.get("abr")
                                or f.get("tbr")
                            )
                        )
                    )
                )
            ]

            if usable_fallback:
                audio_formats = (
                    usable_fallback
                )

                logger.info(
                    "YouTube audio fallback succeeded | "
                    "url=%s | formats=%d",
                    url,
                    len(audio_formats),
                )
            else:
                logger.warning(
                    "YouTube audio fallback returned "
                    "no usable standalone audio | url=%s",
                    url,
                )

        except Exception as e:
            logger.warning(
                "YouTube audio fallback probe failed | "
                "url=%s | error=%s",
                url,
                str(e)[:300],
            )

    # ---------------------------------------------------------------
    # Select best audio
    # ---------------------------------------------------------------
    # Prefer the M4A audio stream because that is what the downloader uses.
    preferred_audio = [
        f for f in audio_formats
        if f.get("ext") == "m4a"
    ]

    if preferred_audio:
        audio_formats = preferred_audio

    best_audio = None

    if audio_formats:
        best_audio = max(
            audio_formats,
            key=lambda f: (
                f.get("abr") or 0,
                f.get("asr") or 0,
                f.get("filesize") or f.get("filesize_approx") or 0,
            ),
        )

    best_audio_size = estimate_size(best_audio)

    results = []
    seen_heights = set()

    for tier in YOUTUBE_RESOLUTION_TIERS:

        candidates = [
            f for f in video_formats
            if f.get("height", 0) <= tier
        ]

        if not candidates:
            continue

        # Prefer MP4 video streams because the final output is MP4.
        mp4_candidates = [
            f for f in candidates
            if f.get("ext") == "mp4"
        ]

        if mp4_candidates:
            candidates = mp4_candidates

        best = max(
            candidates,
            key=lambda f: (
                f.get("height") or 0,
                f.get("fps") or 0,
                f.get("vbr") or 0,
                f.get("tbr") or 0,
            ),
        )

        height = best.get("height")

        if not height or height in seen_heights:
            continue

        seen_heights.add(height)

        video_size = estimate_size(best)

        # If the selected video stream already contains audio,
        # don't add audio again.
        has_audio = best.get("acodec") not in (None, "none")

        if has_audio:
            total_size = video_size
        else:
            total_size = video_size + best_audio_size

        # Unknown size: don't show "0 MB".
        if total_size <= 0:
            continue

        results.append({
            "kind": "video",
            "label": f"{height}p",
            "height": height,
            "size_bytes": total_size,
        })

    if best_audio:
        if best_audio_size > 0:
            results.append({
                "kind": "audio",
                "label": "Audio",
                "height": 0,
                "size_bytes": best_audio_size,
            })

    return results

def probe_youtube_qualities(url: str) -> dict:
    """
    Probe a YouTube URL once and build all quality buttons locally.

    No per-resolution yt-dlp requests are made.

    Format selection mirrors the actual downloader:
      - resolution first
      - FPS second
      - H.264/M4A preferred
      - bitrate/size used as tie breakers

    Results are cached briefly so repeated requests for the same URL
    do not cause another YouTube extraction.
    """

    cache_key = url.strip().split("&")[0]

    # ---------------------------------------------------------------
    # Check cache
    # ---------------------------------------------------------------

    def _get_cached_probe():
        now = time.monotonic()

        with _youtube_probe_cache_lock:
            cached = _youtube_probe_cache.get(
                cache_key
            )

            if not cached:
                return None

            cached_time, cached_result = cached

            if (
                now - cached_time
                < YOUTUBE_PROBE_CACHE_TTL
            ):
                return cached_result

            del _youtube_probe_cache[
                cache_key
            ]

        return None

    cached_result = _get_cached_probe()

    if cached_result is not None:
        return cached_result

    probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "noplaylist": True,
    }

    info = None

    # ---------------------------------------------------------------
    # First try public YouTube extraction.
    #
    # Ordinary public videos can expose a much richer format list
    # without the authenticated browser session. This is especially
    # important when YouTube gives the logged-in session SABR-only
    # formats.
    # ---------------------------------------------------------------
    try:
        public_probe_opts = dict(
            probe_opts
        )

        public_probe_opts.update(
            _youtube_extra_opts(
                ["default"],
                use_cookies=False,
            )
        )

        with _youtube_probe_semaphore:
            with yt_dlp.YoutubeDL(
                public_probe_opts
            ) as public_ydl:
                public_info = (
                    public_ydl.extract_info(
                        url,
                        download=False,
                        process=False,
                    )
                )

        public_formats = (
            public_info.get("formats")
            if public_info
            else []
        ) or []

        public_video_formats = [
            f
            for f in public_formats
            if (
                f.get("vcodec")
                not in (None, "none")
                and f.get("height")
            )
        ]

        public_max_height = max(
            (
                f.get("height") or 0
                for f in public_video_formats
            ),
            default=0,
        )

        if (
            public_max_height
            <= MIN_ACCEPTABLE_MAX_HEIGHT
        ):
            raise Exception(
                "Public YouTube probe returned "
                f"only {public_max_height}p."
            )

        info = public_info

        logger.info(
            "YouTube public probe succeeded | "
            "video_id=%s | max_height=%sp",
            public_info.get("id"),
            public_max_height,
        )

    except Exception as public_error:
        logger.info(
            "YouTube public probe was not rich enough; "
            "falling back to authenticated extraction | "
            "url=%s | error=%s",
            url,
            str(public_error)[:300],
        )

        with _youtube_probe_semaphore:
            info, _ = _extract_resilient(
                probe_opts,
                url,
                download=False,
                process=False,
            )
    if not info:
        raise Exception("Media info not found.")

    video_id = info.get("id")
    title = info.get("title", "")
    thumbnail = info.get("thumbnail")
    duration = info.get("duration") or 0

    formats = info.get("formats") or []

    video_formats = [
        f
        for f in formats
        if f.get("vcodec") not in (None, "none")
        and f.get("height")
    ]

    audio_formats = [
        f for f in formats
        if f.get("vcodec") in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]

    # The video probe intentionally uses default + web_safari because
    # that combination gives us the high-resolution video formats.
    # Depending on the account/client response, it may expose only
    # combined HLS video+audio formats and no standalone audio stream.
    #
    # In that case, do one small secondary probe using yt-dlp's normal
    # default client WITHOUT browser cookies. Public YouTube videos
    # commonly expose the standalone M4A audio formats through this path.
    if not audio_formats:
        try:
            audio_probe_opts = {
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 30,
                "noplaylist": True,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["default"],
                    }
                },
            }

            with yt_dlp.YoutubeDL(
                audio_probe_opts
            ) as audio_ydl:
                audio_info = audio_ydl.extract_info(
                    url,
                    download=False,
                    process=False,
                )

            audio_probe_formats = (
                audio_info.get("formats")
                or []
            )

            audio_formats = [
                f
                for f in audio_probe_formats
                if f.get("vcodec") in (None, "none")
                and f.get("acodec") not in (None, "none")
            ]

            logger.info(
                "YouTube audio fallback probe | "
                "url=%s | audio_formats=%d",
                url,
                len(audio_formats),
            )

        except Exception as e:
            logger.warning(
                "YouTube audio fallback probe failed | "
                "url=%s | error=%s",
                url,
                str(e)[:300],
            )

    def codec_rank(fmt: dict) -> int:
        """
        Prefer codecs in roughly the same order as:

            H.264 > H.265 > VP9 > AV1

        This does NOT remove other codecs.
        It only makes H.264 preferred when the resolution is equal.
        """

        codec = (fmt.get("vcodec") or "").lower()

        if codec.startswith("avc1"):
            return 50

        if codec.startswith(("hev1", "hvc1")):
            return 40

        if codec.startswith("vp9"):
            return 30

        if codec.startswith("av01"):
            return 20

        return 10

    def audio_codec_rank(fmt: dict) -> int:
        codec = (fmt.get("acodec") or "").lower()

        if codec.startswith("mp4a"):
            return 50

        if codec.startswith("aac"):
            return 40

        if codec.startswith("opus"):
            return 30

        if codec.startswith("vorbis"):
            return 20

        return 10

    def extension_rank(fmt: dict) -> int:
        ext = (fmt.get("ext") or "").lower()

        if ext == "mp4":
            return 20

        if ext == "webm":
            return 10

        return 5

    def estimate_size(fmt: dict) -> int:
        if not fmt:
            return 0

        exact = (
            fmt.get("filesize")
            or fmt.get("filesize_approx")
        )

        if exact:
            return int(exact)

        if duration:
            bitrate = (
                fmt.get("vbr")
                or fmt.get("abr")
                or fmt.get("tbr")
            )

            if bitrate:
                return int(
                    float(bitrate)
                    * 1000
                    / 8
                    * duration
                )

        return 0

    # ---------------------------------------------------------------
    # Select best audio
    # ---------------------------------------------------------------

    best_audio = None

    if audio_formats:
        m4a_audio = [
            f
            for f in audio_formats
            if (f.get("ext") or "").lower() == "m4a"
        ]

        if m4a_audio:
            audio_formats = m4a_audio

        best_audio = max(
            audio_formats,
            key=lambda f: (
                f.get("abr") or 0,
                audio_codec_rank(f),
                f.get("asr") or 0,
                estimate_size(f),
            ),
        )

    audio_size = estimate_size(best_audio)

    # ---------------------------------------------------------------
    # Build video quality buttons
    # ---------------------------------------------------------------

    options = []
    seen_heights = set()

    for target_height in YOUTUBE_RESOLUTION_TIERS:

        candidates = [
            f
            for f in video_formats
            if (f.get("height") or 0) <= target_height
        ]

        if not candidates:
            continue

        best = max(
            candidates,
            key=lambda f: (
                f.get("height") or 0,
                f.get("fps") or 0,
                codec_rank(f),
                extension_rank(f),
                f.get("vbr") or 0,
                f.get("tbr") or 0,
                estimate_size(f),
            ),
        )

        height = best.get("height")

        if not height:
            continue

        if height in seen_heights:
            continue

        video_size = estimate_size(best)

        has_audio = (
            best.get("acodec")
            not in (None, "none")
        )

        if has_audio:
            total_size = video_size
        else:
            total_size = video_size + audio_size

        if total_size <= 0:
            continue

        if total_size > MAX_TELEGRAM_BYTES:
            continue

        seen_heights.add(height)

        options.append(
            {
                "kind": "video",
                "label": f"{height}p",
                "height": height,
                "size_bytes": total_size,
            }
        )

    # ---------------------------------------------------------------
    # Audio button
    # ---------------------------------------------------------------

    if (
        best_audio
        and (
            audio_size <= 0
            or audio_size <= MAX_TELEGRAM_BYTES
        )
    ):
        options.append(
            {
                "kind": "audio",
                "label": "Audio",
                "height": 0,
                "size_bytes": max(
                    audio_size,
                    0,
                ),
            }
        )

    result = {
        "id": video_id,
        "title": title,
        "thumbnail": thumbnail,
        "options": options,
    }

    # ---------------------------------------------------------------
    # Store cache
    # ---------------------------------------------------------------

    with _youtube_probe_cache_lock:

        if len(_youtube_probe_cache) >= YOUTUBE_PROBE_CACHE_MAX:

            oldest_key = min(
                _youtube_probe_cache,
                key=lambda k: _youtube_probe_cache[k][0],
            )

            del _youtube_probe_cache[oldest_key]

        _youtube_probe_cache[cache_key] = (
            time.monotonic(),
            result,
        )

    return result

def download_youtube_quality(
    video_id: str,
    height_or_audio: str,
    progress_hook=None,
):
    """
    Download one specific YouTube resolution.

    H.264/AAC is preferred through yt-dlp format sorting,
    but other codecs remain available when necessary.
    """

    url = f"https://www.youtube.com/watch?v={video_id}"

    if height_or_audio == "audio":

        selector = (
            "bestaudio[ext=m4a]"
            "/bestaudio"
            "/best"
        )

        extract_audio = True

        youtube_client_attempts = [
            ["default"]
        ]

        youtube_use_browser_cookies = False

    else:
        height = int(
            height_or_audio
        )

        selector = (
            f"bestvideo[height<={height}]"
            f"+bestaudio/"
            f"best[height<={height}]"
            f"/best"
        )

        extract_audio = False

        youtube_client_attempts = None
        youtube_use_browser_cookies = True

    # ---------------------------------------------------------------
    # Public YouTube extraction first.
    # This is the path that successfully exposed 1080p+ for videos
    # where the authenticated session was restricted to 360p.
    # ---------------------------------------------------------------
    try:

        return _download_with_selector(
            url,
            selector,
            extract_audio,
            progress_hook,
            youtube_client_attempts=[
                ["default"]
            ],
            youtube_use_browser_cookies=False,
        )

    except Exception as public_error:

        logger.warning(
            "Public YouTube quality download failed; "
            "retrying with authenticated cookies | "
            "video_id=%s | quality=%s | error=%s",
            video_id,
            height_or_audio,
            str(public_error)[:300],
        )

        return _download_with_selector(
            url,
            selector,
            extract_audio,
            progress_hook,
        )

def get_spotify_client():
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials

    auth = SpotifyClientCredentials(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
    )
    return spotipy.Spotify(client_credentials_manager=auth)


def _track_info(
    t: dict,
    album: dict | None = None,
) -> dict:
    album_data = album or t.get("album") or {}

    images = album_data.get("images") or []

    album_artists = ", ".join(
        a["name"]
        for a in album_data.get("artists", [])
        if a.get("name")
    )

    return {
        "name": t["name"],
        "artists": ", ".join(
            a["name"]
            for a in t.get("artists", [])
            if a.get("name")
        ),
        "album": album_data.get("name", ""),
        "album_artist": album_artists,
        "cover_url": images[0]["url"] if images else None,
        "duration_ms": t.get("duration_ms", 0),
        "track_number": t.get("track_number"),
        "disc_number": t.get("disc_number"),
        "total_tracks": album_data.get("total_tracks"),
        "release_date": album_data.get("release_date", ""),
    }


def resolve_spotify_tracks(url: str) -> list:
    """Track/album/playlist metadata only, from Spotify's official Web API —
    no audio here yet."""
    sp = get_spotify_client()
    if "track/" in url:
        return [_track_info(sp.track(url))]
    if "album/" in url:
        album = sp.album(url)
        return [_track_info(t, album=album) for t in album["tracks"]["items"]]
    if "playlist/" in url:
        items = sp.playlist_items(url)["items"]
        return [_track_info(i["track"]) for i in items if i.get("track")]
    raise ValueError("Unsupported Spotify link — send a track, album, or playlist link.")


def _duration_close_enough(candidate_seconds, expected_ms, tolerance_seconds=20) -> bool:
    if not candidate_seconds or not expected_ms:
        return True  # can't compare — don't block a match just because duration is missing
    return abs(candidate_seconds - (expected_ms / 1000)) <= tolerance_seconds


def download_spotify_track(
    track: dict,
    progress_hook=None,
) -> str:
    """
    Find and download the best matching audio for a Spotify track.

    Strategy:
    1. Search SoundCloud first.
    2. Search YouTube if SoundCloud has no usable candidate.
    3. Keep search extraction lightweight with process=False so a bad/DRM
       candidate cannot make the entire search appear empty.
    4. Compare candidates against Spotify's official duration.
    5. Try matching candidates one by one until one downloads successfully.
    6. Apply Spotify metadata and album artwork to the downloaded file.
    """

    query_text = (
        f"{track['artists']} - {track['name']}"
    )

    artist_text = track["artists"].strip()
    title_text = track["name"].strip()

    search_attempts = [
        (
            f"scsearch5:{artist_text} - {title_text}",
            "soundcloud",
            False,
        ),
        (
            f"scsearch5:{title_text} - {artist_text}",
            "soundcloud",
            False,
        ),
        (
            f"scsearch5:{title_text}",
            "soundcloud",
            False,
        ),
        (
            f"ytsearch5:{artist_text} - {title_text}",
            "youtube",
            True,
        ),
        (
            f"ytsearch5:{title_text} - {artist_text}",
            "youtube",
            True,
        ),
        (
            f"ytsearch5:{title_text}",
            "youtube",
            True,
        ),
        (
            f"ytsearch5:{artist_text} {title_text}",
            "youtube",
            True,
        ),
    ]

    last_error = None

    for (
        query,
        source_type,
        needs_proxy_for_probe,
    ) in search_attempts:

        try:
            search_opts = {
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 30,
                "extract_flat": True,
                "playlistend": 5,
                "ignoreerrors": True,
            }

            if needs_proxy_for_probe:
                proxy = _get_random_proxy()

                if proxy:
                    search_opts["proxy"] = proxy

            # -----------------------------------------------------------
            # Search only.
            #
            # process=False is important here:
            # it prevents yt-dlp from fully extracting each search result
            # during the search stage. This is especially important for
            # SoundCloud, where a DRM-protected result can otherwise cause
            # the entry to disappear entirely.
            # -----------------------------------------------------------
            with yt_dlp.YoutubeDL(
                search_opts
            ) as search_ydl:
                info = search_ydl.extract_info(
                    query,
                    download=False,
                    process=False,
                )

            if not info:
                logger.warning(
                    "Spotify search returned no info | "
                    "source=%s | query=%s",
                    source_type,
                    query,
                )
                continue

            entries = [
                entry
                for entry in (
                    info.get("entries")
                    or []
                )
                if entry
            ]

            if not entries:
                logger.warning(
                    "Spotify search returned zero entries | "
                    "source=%s | query=%s | info_type=%s",
                    source_type,
                    query,
                    info.get("_type"),
                )
                continue

            logger.info(
                "Spotify search returned %d entries | "
                "source=%s | query=%s",
                len(entries),
                source_type,
                query,
            )

            # -----------------------------------------------------------
            # Try every duration-compatible candidate.
            #
            # This is intentionally different from the old behavior,
            # which picked the first matching candidate and stopped.
            # A SoundCloud result can have the right duration but still
            # be DRM-protected, so a later candidate may be usable.
            # -----------------------------------------------------------
            matching_entries = []

            for entry in entries:
                if _duration_close_enough(
                    entry.get("duration"),
                    track.get("duration_ms"),
                ):
                    matching_entries.append(entry)

            if not matching_entries:
                logger.warning(
                    "Spotify search returned entries but none matched "
                    "Spotify duration | source=%s | candidates=%d | query=%s",
                    source_type,
                    len(entries),
                    query,
                )
                continue

            logger.info(
                "Spotify found %d duration-compatible candidates | "
                "source=%s | query=%s",
                len(matching_entries),
                source_type,
                query,
            )

            for matched_entry in matching_entries:
                try:
                    source_url = (
                        matched_entry.get("webpage_url")
                        or matched_entry.get("original_url")
                    )

                    if (
                        not source_url
                        and source_type == "youtube"
                    ):
                        video_id = matched_entry.get("id")

                        if video_id:
                            source_url = (
                                "https://www.youtube.com/watch?v="
                                f"{video_id}"
                            )

                    if not source_url:
                        source_url = matched_entry.get("url")

                    if not source_url:
                        logger.warning(
                            "Spotify candidate has no usable URL | "
                            "source=%s | title=%s",
                            source_type,
                            matched_entry.get("title"),
                        )
                        continue

                    logger.info(
                        "Spotify candidate selected | "
                        "source=%s | title=%s | duration=%s | url=%s",
                        source_type,
                        matched_entry.get("title"),
                        matched_entry.get("duration"),
                        source_url,
                    )

                    if (
                        source_type == "youtube"
                        and _is_youtube_url(source_url)
                    ):
                        (
                            _,
                            _,
                            filepaths,
                        ) = _download_with_selector(
                            source_url,
                            "bestaudio[ext=m4a]/bestaudio/best",
                            True,
                            progress_hook,
                            youtube_client_attempts=[
                                ["default"]
                            ],
                            youtube_use_browser_cookies=False,
                        )
                    else:
                        (
                            _,
                            _,
                            filepaths,
                        ) = _download_with_selector(
                            source_url,
                            "bestaudio/best",
                            True,
                            progress_hook,
                        )

                    if not filepaths:
                        raise Exception(
                            "Matched source downloaded no file."
                        )

                    filepath = filepaths[0]

                    if not filepath:
                        raise Exception(
                            "Matched source returned an empty filepath."
                        )

                    tag_audio_file(
                        filepath,
                        title=track["name"],
                        artist=track["artists"],
                        album=track["album"],
                        cover_url=track["cover_url"],
                        album_artist=track.get(
                            "album_artist",
                            "",
                        ),
                        release_date=track.get(
                            "release_date",
                            "",
                        ),
                        track_number=track.get(
                            "track_number"
                        ),
                        total_tracks=track.get(
                            "total_tracks"
                        ),
                        disc_number=track.get(
                            "disc_number"
                        ),
                    )

                    return filepath

                except Exception as candidate_error:
                    last_error = candidate_error

                    logger.warning(
                        "Spotify candidate failed | "
                        "source=%s | title=%s | error=%s",
                        source_type,
                        matched_entry.get("title"),
                        str(candidate_error)[:300],
                    )

                    continue

            if not matching_entries:
                logger.warning(
                    "Spotify search returned entries but none matched "
                    "Spotify duration | source=%s | candidates=%d | query=%s",
                    source_type,
                    len(entries),
                    query,
                )
                continue

            logger.info(
                "Spotify found %d duration-compatible candidates | "
                "source=%s",
                len(matching_entries),
                source_type,
            )

            # -----------------------------------------------------------
            # Try matching candidates one by one.
            # -----------------------------------------------------------
            for matched_entry in matching_entries:

                try:
                    # ---------------------------------------------------
                    # Resolve the actual webpage URL.
                    # ---------------------------------------------------
                    source_url = (
                        matched_entry.get(
                            "webpage_url"
                        )
                        or matched_entry.get(
                            "original_url"
                        )
                    )

                    if (
                        not source_url
                        and source_type == "youtube"
                    ):
                        video_id = matched_entry.get(
                            "id"
                        )

                        if video_id:
                            source_url = (
                                "https://www.youtube.com/watch?v="
                                f"{video_id}"
                            )

                    if not source_url:
                        source_url = matched_entry.get(
                            "url"
                        )

                    if not source_url:
                        logger.warning(
                            "Spotify candidate has no usable URL | "
                            "source=%s | title=%s",
                            source_type,
                            matched_entry.get("title"),
                        )
                        continue

                    logger.info(
                        "Spotify candidate selected | "
                        "source=%s | title=%s | duration=%s | url=%s",
                        source_type,
                        matched_entry.get("title"),
                        matched_entry.get("duration"),
                        source_url,
                    )

                    # ---------------------------------------------------
                    # Download exact candidate.
                    # ---------------------------------------------------
                    if (
                        source_type == "youtube"
                        and _is_youtube_url(source_url)
                    ):
                        (
                            _,
                            _,
                            filepaths,
                        ) = _download_with_selector(
                            source_url,
                            "bestaudio[ext=m4a]/bestaudio/best",
                            True,
                            progress_hook,
                            youtube_client_attempts=[
                                ["default"]
                            ],
                            youtube_use_browser_cookies=False,
                        )

                    else:
                        (
                            _,
                            _,
                            filepaths,
                        ) = _download_with_selector(
                            source_url,
                            "bestaudio/best",
                            True,
                            progress_hook,
                        )

                    if not filepaths:
                        raise Exception(
                            "Matched source downloaded no file."
                        )

                    filepath = filepaths[0]

                    if not filepath:
                        raise Exception(
                            "Matched source returned an empty filepath."
                        )

                    # ---------------------------------------------------
                    # Apply Spotify metadata and album artwork.
                    # ---------------------------------------------------
                    tag_audio_file(
                        filepath,
                        title=track["name"],
                        artist=track["artists"],
                        album=track["album"],
                        cover_url=track["cover_url"],
                        album_artist=track.get(
                            "album_artist",
                            "",
                        ),
                        release_date=track.get(
                            "release_date",
                            "",
                        ),
                        track_number=track.get(
                            "track_number"
                        ),
                        total_tracks=track.get(
                            "total_tracks"
                        ),
                        disc_number=track.get(
                            "disc_number"
                        ),
                    )

                    return filepath

                except Exception as candidate_error:
                    last_error = candidate_error

                    logger.warning(
                        "Spotify candidate failed | "
                        "source=%s | title=%s | url=%s | error=%s",
                        source_type,
                        matched_entry.get("title"),
                        source_url if "source_url" in locals() else None,
                        str(candidate_error)[:300],
                    )

                    # Try the next candidate instead of abandoning
                    # the entire source.
                    continue

        except Exception as e:
            last_error = e

            logger.warning(
                "Spotify source search failed | "
                "source=%s | query=%s | error=%s",
                source_type,
                query,
                str(e)[:300],
            )

            # Move to the next source (SoundCloud -> YouTube).
            continue

    raise (
        last_error
        or Exception(
            "No matching audio found on SoundCloud or YouTube."
        )
    )



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
    """
    Write metadata to MP3 or M4A files.

    MP3:
        Uses ID3 tags.

    M4A:
        Uses MP4/M4A atoms directly.
    """

    if not filepath:
        return

    lower_path = filepath.lower()

    try:
        if lower_path.endswith(".mp3"):
            import mimetypes
            import urllib.request

            from mutagen.id3 import (
                ID3,
                ID3NoHeaderError,
                TIT2,
                TPE1,
                TALB,
                TPE2,
                TDRC,
                TRCK,
                TPOS,
                APIC,
            )

            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()

            if title:
                tags["TIT2"] = TIT2(
                    encoding=3,
                    text=[title],
                )

            if artist:
                tags["TPE1"] = TPE1(
                    encoding=3,
                    text=[artist],
                )

            if album:
                tags["TALB"] = TALB(
                    encoding=3,
                    text=[album],
                )

            if album_artist:
                tags["TPE2"] = TPE2(
                    encoding=3,
                    text=[album_artist],
                )

            if release_date:
                tags["TDRC"] = TDRC(
                    encoding=3,
                    text=[str(release_date)],
                )

            if track_number:
                track_text = str(track_number)

                if total_tracks:
                    track_text = (
                        f"{track_number}/{total_tracks}"
                    )

                tags["TRCK"] = TRCK(
                    encoding=3,
                    text=[track_text],
                )

            if disc_number:
                disc_text = str(disc_number)

                if total_discs:
                    disc_text = (
                        f"{disc_number}/{total_discs}"
                    )

                tags["TPOS"] = TPOS(
                    encoding=3,
                    text=[disc_text],
                )

            if cover_url:
                try:
                    request = urllib.request.Request(
                        cover_url,
                        headers={
                            "User-Agent": (
                                "Mozilla/5.0 "
                                "(Windows NT 10.0; Win64; x64)"
                            )
                        },
                    )

                    with urllib.request.urlopen(
                        request,
                        timeout=10,
                    ) as response:
                        cover_bytes = response.read()
                        content_type = (
                            response.headers.get(
                                "Content-Type"
                            )
                            or ""
                        ).split(";")[0].lower()

                    if content_type not in {
                        "image/jpeg",
                        "image/png",
                    }:
                        guessed_type, _ = (
                            mimetypes.guess_type(
                                cover_url
                            )
                        )

                        if guessed_type in {
                            "image/jpeg",
                            "image/png",
                        }:
                            content_type = guessed_type
                        else:
                            content_type = "image/jpeg"

                    tags.delall("APIC")

                    tags["APIC"] = APIC(
                        encoding=3,
                        mime=content_type,
                        type=3,
                        desc="Cover",
                        data=cover_bytes,
                    )

                except Exception as e:
                    logger.warning(
                        "Could not embed MP3 cover art into %s: %s",
                        filepath,
                        e,
                    )

            tags.save(
                filepath,
                v2_version=3,
            )

            return

        if lower_path.endswith(".m4a"):
            from mutagen.mp4 import (
                MP4,
                MP4Cover,
            )
            import mimetypes
            import urllib.request

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
                audio.tags["\xa9day"] = [
                    str(release_date)
                ]

            if track_number:
                audio.tags["trkn"] = [
                    (
                        int(track_number),
                        int(total_tracks or 0),
                    )
                ]

            if disc_number:
                audio.tags["disk"] = [
                    (
                        int(disc_number),
                        int(total_discs or 0),
                    )
                ]

            if cover_url:
                try:
                    request = urllib.request.Request(
                        cover_url,
                        headers={
                            "User-Agent": (
                                "Mozilla/5.0 "
                                "(Windows NT 10.0; Win64; x64)"
                            )
                        },
                    )

                    with urllib.request.urlopen(
                        request,
                        timeout=10,
                    ) as response:
                        cover_bytes = response.read()
                        content_type = (
                            response.headers.get(
                                "Content-Type"
                            )
                            or ""
                        ).split(";")[0].lower()

                    if (
                        content_type == "image/png"
                        or ".png" in cover_url.lower()
                    ):
                        image_format = (
                            MP4Cover.FORMAT_PNG
                        )
                    else:
                        image_format = (
                            MP4Cover.FORMAT_JPEG
                        )

                    audio.tags["covr"] = [
                        MP4Cover(
                            cover_bytes,
                            imageformat=image_format,
                        )
                    ]

                except Exception as e:
                    logger.warning(
                        "Could not embed M4A cover art into %s: %s",
                        filepath,
                        e,
                    )

            audio.save()

            return

        logger.warning(
            "Audio tagging skipped for unsupported file type: %s",
            filepath,
        )

    except Exception as e:
        logger.exception(
            "Audio tagging failed for %s: %s",
            filepath,
            e,
        )