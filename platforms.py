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

import yt_dlp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lightweight YouTube probe cache
# ---------------------------------------------------------------------------

YOUTUBE_PROBE_CACHE_TTL = 300  # 5 minutes
YOUTUBE_PROBE_CACHE_MAX = 64

_youtube_probe_cache = {}
_youtube_probe_cache_lock = threading.Lock()
_youtube_probe_semaphore = threading.Semaphore(1)

DOWNLOAD_DIR = "downloads"

PLATFORM_NAMES = {
    "instagram": "Instagram",
    "youtube": "YouTube",
    "soundcloud": "SoundCloud",
    "spotify": "Spotify",
}

PLATFORM_PATTERNS = {
    "instagram": re.compile(r"instagram\.com/\S+", re.IGNORECASE),
    "youtube": re.compile(r"(youtube\.com|youtu\.be)/\S+", re.IGNORECASE),
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


def _youtube_extra_opts(clients) -> dict:
    opts = {
        "extractor_args": {
            "youtube": {
                "player_client": clients
            }
        }
    }

    cookiefile = os.environ.get(
        "YOUTUBE_COOKIE_FILE",
        "cookies.txt",
    )

    if cookiefile and os.path.exists(cookiefile):
        opts["cookiefile"] = cookiefile

    browser = os.environ.get(
        "YTDLP_COOKIES_BROWSER"
    )

    if browser:
        opts["cookiesfrombrowser"] = (
            browser,
        )

    return opts


def _instagram_extra_opts() -> dict:
    """
    Instagram-specific authentication.

    Instagram stories frequently require an authenticated
    Instagram session. Keep these cookies separate from the
    YouTube cookies.
    """

    opts = {}

    cookiefile = os.environ.get(
        "INSTAGRAM_COOKIE_FILE",
        "instagram_cookies.txt",
    )

    if cookiefile and os.path.exists(cookiefile):
        opts["cookiefile"] = cookiefile

    browser = os.environ.get(
        "INSTAGRAM_COOKIES_BROWSER"
    )

    if browser:
        opts["cookiesfrombrowser"] = (
            browser,
        )

    return opts

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

    YouTube:
        Uses the configured YouTube player clients/cookies.

    Instagram:
        Uses the dedicated Instagram cookie file/browser session.

    Other platforms:
        Use the base options unchanged.
    """

    last_error = None

    is_youtube = _is_youtube_url(target)

    is_instagram = (
        "instagram.com" in target.lower()
    )

    if is_youtube:
        attempts = _client_attempts()
    else:
        attempts = [None]

    for clients in attempts:

        opts = dict(ydl_opts_base)

        if is_youtube and clients:
            opts.update(
                _youtube_extra_opts(clients)
            )

        elif is_instagram:
            opts.update(
                _instagram_extra_opts()
            )

        if use_proxy:
            proxy = _get_random_proxy()

            if proxy:
                opts["proxy"] = proxy

        try:

            with yt_dlp.YoutubeDL(opts) as ydl:

                info = ydl.extract_info(
                    target,
                    download=download,
                    process=process,
                )

                return info, ydl

        except Exception as e:

            last_error = e

            if is_youtube:
                retryable = any(
                    hint in str(e).lower()
                    for hint in _RETRYABLE_ERROR_HINTS
                )

                if retryable:
                    continue

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

    ydl_opts = {
        "format": format_selector,
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "color": "never",
        "noplaylist": False,
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

    else:

        cookiefile = os.environ.get(
            "YOUTUBE_COOKIE_FILE",
            "cookies.txt",
        )

        if cookiefile and os.path.exists(cookiefile):
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
        # Download the original thumbnail so it can be embedded
        # into the final MP3.
        ydl_opts["writethumbnail"] = True

        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            },
            {
                "key": "FFmpegMetadata",
            },
            {
                "key": "EmbedThumbnail",
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
            raise Exception("Media info not found.")

        entries_raw = info_raw.get("entries") or [info_raw]

        filepaths = []
        valid_entries = []

        for raw_entry in entries_raw:
            if not raw_entry:
                continue

            for key in ("extractor", "extractor_key", "webpage_url"):
                if key not in raw_entry and key in info_raw:
                    raw_entry[key] = info_raw[key]

            if _is_probably_photo_entry(info_raw, raw_entry):
                path = _download_image_entry(raw_entry)

                if path:
                    filepaths.append(path)
                    valid_entries.append(raw_entry)

                continue

            entry_id = raw_entry.get("id")

            processed = ydl.process_ie_result(
                raw_entry,
                download=True,
            )

            if not processed:
                continue

            raw_path = ydl.prepare_filename(processed)

            if extract_audio:
                mp3_path = os.path.splitext(raw_path)[0] + ".mp3"

                path = (
                    mp3_path
                    if os.path.exists(mp3_path)
                    else raw_path
                )

            else:
                # After yt-dlp merges video + audio, the final file
                # should be MP4. Find it explicitly.
                mp4_candidates = []

                if entry_id and os.path.isdir(DOWNLOAD_DIR):
                    prefix = f"{entry_id}."

                    for name in os.listdir(DOWNLOAD_DIR):
                        if (
                            name.startswith(prefix)
                            and name.lower().endswith(".mp4")
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

                # Prepare the MP4 for Telegram/iPhone streaming.
                #
                # _ensure_h264_mp4() performs the codec conversion
                # when necessary AND applies faststart in the same
                # FFmpeg operation, so no second FFmpeg pass is needed.
                path = _ensure_h264_mp4(
                    path,
                    ffmpeg_location,
                )

            if not os.path.exists(path):
                _cleanup_id(entry_id)

                raise Exception(
                    f"downloaded file does not exist: {path}"
                )

            actual_size = os.path.getsize(path)

            if actual_size < MIN_VALID_FILE_BYTES:
                _cleanup_id(entry_id)

                raise Exception(
                    f"incomplete download: {path}"
                )

            if actual_size > MAX_TELEGRAM_BYTES:
                _cleanup_id(entry_id)

                raise FileTooLargeError(
                    format_size(actual_size)
                )
            filepaths.append(path)
            valid_entries.append(processed)

        if not filepaths:
            raise Exception(
                "Nothing could be downloaded from this link."
            )

        return info_raw, valid_entries, filepaths

    last_error = None

    for clients in _client_attempts():

        opts = dict(ydl_opts)
        opts.update(_youtube_extra_opts(clients))

        if use_proxy:
            proxy = _get_random_proxy()

            if proxy:
                opts["proxy"] = proxy

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return _attempt(ydl)

        except Exception as e:

            if any(
                hint in str(e).lower()
                for hint in _RETRYABLE_ERROR_HINTS
            ):
                last_error = e
                continue

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
    Download media from Instagram / YouTube / SoundCloud.

    Size is checked AFTER the real file exists instead of performing
    another yt-dlp extraction beforehand.
    """

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    is_soundcloud = "soundcloud.com" in url.lower()

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
        ) + ("audio",)

    # ---------------------------------------------------------------
    # Normal direct-download path
    # ---------------------------------------------------------------

    start = (
        QUALITY_LADDER.index(quality)
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

        fmt = _format_for(url, tier)

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

        exact = fmt.get("filesize")
        if exact:
            return int(exact)

        approximate = fmt.get("filesize_approx")
        if approximate:
            return int(approximate)

        if duration:
            # Video-only streams: use VIDEO bitrate.
            vbr = fmt.get("vbr")
            if vbr:
                return int(float(vbr) * 1000 / 8 * duration)

            # Audio-only streams: use AUDIO bitrate.
            abr = fmt.get("abr")
            if abr:
                return int(float(abr) * 1000 / 8 * duration)

            # Final fallback.
            tbr = fmt.get("tbr")
            if tbr:
                return int(float(tbr) * 1000 / 8 * duration)

        return 0

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
    now = time.monotonic()

    with _youtube_probe_cache_lock:
        cached = _youtube_probe_cache.get(cache_key)

        if cached:
            cached_time, cached_result = cached

            if now - cached_time < YOUTUBE_PROBE_CACHE_TTL:
                return cached_result

            del _youtube_probe_cache[cache_key]

    probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "noplaylist": True,
    }

    # Only allow one metadata probe at a time on the 2-core VPS.
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
        f
        for f in formats
        if f.get("vcodec") in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]

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
        audio_size > 0
        and audio_size <= MAX_TELEGRAM_BYTES
    ):
        options.append(
            {
                "kind": "audio",
                "label": "Audio",
                "height": 0,
                "size_bytes": audio_size,
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

        selector = "bestaudio/best"
        extract_audio = True

    else:

        height = int(height_or_audio)

        selector = (
            f"bestvideo[height<={height}]"
            f"+bestaudio/"
            f"best[height<={height}]"
            f"/best"
        )

        extract_audio = False

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


def download_spotify_track(track: dict, progress_hook=None) -> str:
    """Finds and downloads the best-matching audio for a Spotify track.
    Tries SoundCloud first — it's generally more reliable since it doesn't
    involve YouTube's bot checks at all — and falls back to YouTube search
    only if SoundCloud doesn't have a good match. "Good match" is checked by
    comparing the found track's length against Spotify's own duration
    rather than trusting whatever a search returns first, since search
    engines rarely return zero results even for a bad match."""
    query_text = f"{track['artists']} - {track['name']}"
    attempts = [
        (f"scsearch1:{query_text}", False),
        (f"ytsearch1:{query_text} audio", True),
    ]

    filepath = None
    last_error = None
    for query, needs_proxy_for_probe in attempts:
        try:
            probe_opts = {"quiet": True, "no_warnings": True, "socket_timeout": 30, "noplaylist": True}
            info, _ = _extract_resilient(probe_opts, query, download=False, use_proxy=needs_proxy_for_probe)
            entry = info["entries"][0] if info.get("_type") == "playlist" else info
            if not entry:
                continue
            if not _duration_close_enough(entry.get("duration"), track.get("duration_ms")):
                continue  # this source's top result doesn't look like the right track

            _, _, filepaths = _download_with_selector(query, "bestaudio/best", True, progress_hook)
            filepath = filepaths[0]
            break
        except Exception as e:
            last_error = e
            continue

    if not filepath:
        raise last_error or Exception("No matching audio found on SoundCloud or YouTube.")

    tag_audio_file(
        filepath,
        title=track["name"],
        artist=track["artists"],
        album=track["album"],
        cover_url=track["cover_url"],
        album_artist=track.get("album_artist", ""),
        release_date=track.get("release_date", ""),
        track_number=track.get("track_number"),
        total_tracks=track.get("total_tracks"),
        disc_number=track.get("disc_number"),
    )
    return filepath


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
    Write professional ID3 metadata and embedded cover art
    into an MP3 file.
    """

    if not filepath.lower().endswith(".mp3"):
        return

    try:
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
                track_text = f"{track_number}/{total_tracks}"

            tags["TRCK"] = TRCK(
                encoding=3,
                text=[track_text],
            )

        if disc_number:
            disc_text = str(disc_number)

            if total_discs:
                disc_text = f"{disc_number}/{total_discs}"

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
                    timeout=15,
                ) as response:
                    cover_bytes = response.read()
                    content_type = (
                        response.headers.get("Content-Type")
                        or ""
                    ).split(";")[0].lower()

                if content_type not in {
                    "image/jpeg",
                    "image/png",
                }:
                    guessed_type, _ = mimetypes.guess_type(
                        cover_url
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
                    "Could not embed cover art into %s: %s",
                    filepath,
                    e,
                )

        tags.save(
            filepath,
            v2_version=3,
        )

    except Exception as e:
        logger.exception(
            "MP3 tagging failed for %s: %s",
            filepath,
            e,
        )