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
import uuid

import yt_dlp

logger = logging.getLogger(__name__)

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
PROBE_RICH_FALLBACK_CLIENTS = ["android", "web"]


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
    ["tv", "web_safari"],
    ["android_vr"],
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
    opts = {"extractor_args": {"youtube": {"player_client": clients}}}
    browser = os.environ.get("YTDLP_COOKIES_BROWSER")
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def _extract_resilient(ydl_opts_base: dict, target: str, download: bool, process: bool = True, use_proxy: bool = True):
    """Runs yt-dlp's extraction, retrying with different YouTube
    player-client identities if an attempt fails with what looks like one
    of YouTube's bot/token checks. For lightweight probing/search calls
    only — see _download_with_selector for actual file downloads, which
    intentionally does not share this function (it needs its own retry loop
    that covers the download itself, not just the metadata fetch)."""
    last_error = None
    for clients in _client_attempts():
        opts = dict(ydl_opts_base)
        opts.update(_youtube_extra_opts(clients))
        if use_proxy:
            proxy = _get_random_proxy()
            if proxy:
                opts["proxy"] = proxy
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=download, process=process)
                return info, ydl
        except Exception as e:
            if any(hint in str(e).lower() for hint in _RETRYABLE_ERROR_HINTS):
                last_error = e
                continue
            raise
    raise last_error


def _probe_size(url: str, format_selector: str):
    """Returns the estimated size in bytes for this format selector, or None
    if yt-dlp can't tell in advance. Raises FileTooLargeError if it's
    definitely over the configured cap."""
    probe_opts = {
        "quiet": True, "no_warnings": True, "socket_timeout": 30,
        "noplaylist": False, "format": format_selector, "ignoreerrors": True,
    }
    ffmpeg_location = _ffmpeg_location()
    if ffmpeg_location:
        probe_opts["ffmpeg_location"] = ffmpeg_location

    info, _ = _extract_resilient(probe_opts, url, download=False, process=True)
    if not info:
        raise Exception("Media info not found.")

    entries = info.get("entries") or [info]
    total, known = 0, False
    for entry in entries:
        if not entry:
            continue
        size = entry.get("filesize") or entry.get("filesize_approx")
        if size:
            total += size
            known = True

    if known and total > MAX_TELEGRAM_BYTES:
        raise FileTooLargeError(format_size(total))
    return total if known else None


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


def _download_with_selector(url: str, format_selector: str, extract_audio: bool, progress_hook=None, use_proxy: bool = False):
    """Downloads `url` (or a search query like 'ytsearch1:...') at the given
    format selector. Handles multi-entry posts (Instagram carousels can mix
    photos and videos in one post) and retries the WHOLE operation — raw
    fetch, per-entry download, and postprocessing — with a different
    YouTube client identity if any part of it fails. use_proxy defaults to
    False here on purpose: see _get_random_proxy's docstring for why the
    actual file transfer avoids the rotating proxy even though probing uses
    it."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    ydl_opts = {
        "format": format_selector,
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "quiet": True, "no_warnings": True, "color": "never",
        "noplaylist": False, "socket_timeout": 30,
    }
    if extract_audio:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192",
        }]
    ffmpeg_location = _ffmpeg_location()
    if ffmpeg_location:
        ydl_opts["ffmpeg_location"] = ffmpeg_location
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    def _attempt(ydl):
        info_raw = ydl.extract_info(url, download=False, process=False)
        if not info_raw:
            raise Exception("Media info not found.")
        entries_raw = info_raw.get("entries") or [info_raw]

        filepaths, valid_entries = [], []
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
            processed = ydl.process_ie_result(raw_entry, download=True)
            if not processed:
                continue

            raw_path = ydl.prepare_filename(processed)
            if extract_audio:
                mp3_path = os.path.splitext(raw_path)[0] + ".mp3"
                path = mp3_path if os.path.exists(mp3_path) else raw_path
            else:
                path = raw_path

            if not os.path.exists(path) or os.path.getsize(path) < MIN_VALID_FILE_BYTES:
                _cleanup_id(entry_id)
                raise Exception(f"incomplete download: {path}")

            filepaths.append(path)
            valid_entries.append(processed)

        if not filepaths:
            raise Exception("Nothing could be downloaded from this link.")
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
            if any(hint in str(e).lower() for hint in _RETRYABLE_ERROR_HINTS):
                last_error = e
                continue
            raise
    raise last_error


def _format_for(url: str, tier: str) -> str:
    table = YOUTUBE_QUALITY_FORMATS if _is_youtube_url(url) else QUALITY_FORMATS
    return table[tier]


def download_direct(url: str, quality: str = "best", allow_fallback: bool = False, progress_hook=None):
    """Instagram / YouTube / SoundCloud — yt-dlp handles these natively.
    Returns (info, entries, filepaths, quality_used). If allow_fallback is
    True and the requested quality is too large, steps down the ladder
    (best -> 720p -> audio) until one fits, and reports which tier it
    actually used."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    start = QUALITY_LADDER.index(quality) if quality in QUALITY_LADDER else 0
    tiers_to_try = QUALITY_LADDER[start:] if allow_fallback else [quality]

    last_error = None
    for tier in tiers_to_try:
        fmt = _format_for(url, tier)
        try:
            _probe_size(url, fmt)
        except FileTooLargeError as e:
            last_error = e
            continue
        info, entries, filepaths = _download_with_selector(url, fmt, tier == "audio", progress_hook)
        return info, entries, filepaths, tier

    raise last_error or FileTooLargeError("unknown size")


# --- per-video YouTube quality picker (thumbnail + buttons) ---

def _bucket_youtube_formats(info: dict) -> list:
    """From one extract_info() result, builds a list of {label, height,
    size_bytes} for the best available format at each standard resolution
    the video actually has, plus an audio-only entry — skipping tiers that
    don't exist or whose size can't be estimated at all. This is only an
    estimate for the button labels; the real download still uses yt-dlp's
    own format selection."""
    formats = info.get("formats") or []
    video_formats = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("height")]
    audio_formats = [f for f in formats if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]

    audio_sizes = [f.get("filesize") or f.get("filesize_approx") or 0 for f in audio_formats]
    best_audio_size = max(audio_sizes, default=0)

    results = []
    seen_heights = set()
    for tier in YOUTUBE_RESOLUTION_TIERS:
        candidates = [f for f in video_formats if f["height"] <= tier]
        if not candidates:
            continue
        best = max(candidates, key=lambda f: f["height"])
        height = best["height"]
        if height in seen_heights:
            continue
        seen_heights.add(height)

        video_size = best.get("filesize") or best.get("filesize_approx") or 0
        has_audio = best.get("acodec") not in (None, "none")
        total_size = video_size if has_audio else (video_size + best_audio_size)
        if not total_size:
            continue  # can't estimate — leave it out rather than show a misleading button

        results.append({"kind": "video", "label": f"{height}p", "height": height, "size_bytes": total_size})

    if best_audio_size:
        results.append({"kind": "audio", "label": "صوت", "height": 0, "size_bytes": best_audio_size})

    return results


def probe_youtube_qualities(url: str) -> dict:
    """Fetches metadata for one YouTube video and returns its title,
    thumbnail, id, and the list of quality options small enough to send
    (options over MAX_TELEGRAM_BYTES are left out entirely). If the first
    successful client's format list looks unusually thin (some clients,
    the TV client especially, sometimes expose fewer formats than the
    video actually has), tries once more with a client that typically
    reports the fuller list."""
    probe_opts = {"quiet": True, "no_warnings": True, "socket_timeout": 30, "noplaylist": True}
    info, _ = _extract_resilient(probe_opts, url, download=False)
    options = _bucket_youtube_formats(info)

    max_height = max((o["height"] for o in options if o["kind"] == "video"), default=0)
    if 0 < max_height <= MIN_ACCEPTABLE_MAX_HEIGHT:
        try:
            richer_opts = dict(probe_opts)
            richer_opts.update(_youtube_extra_opts(PROBE_RICH_FALLBACK_CLIENTS))
            proxy = _get_random_proxy()
            if proxy:
                richer_opts["proxy"] = proxy
            with yt_dlp.YoutubeDL(richer_opts) as ydl:
                richer_info = ydl.extract_info(url, download=False)
            richer_options = _bucket_youtube_formats(richer_info)
            if len(richer_options) > len(options):
                info, options = richer_info, richer_options
        except Exception:
            pass  # keep whatever we already had — better than nothing

    options = [o for o in options if o["size_bytes"] <= MAX_TELEGRAM_BYTES]
    return {
        "id": info.get("id"),
        "title": info.get("title", ""),
        "thumbnail": info.get("thumbnail"),
        "options": options,
    }


def download_youtube_quality(video_id: str, height_or_audio: str, progress_hook=None):
    """Downloads one specific resolution (or "audio") for a YouTube video,
    identified by video_id — used by the quality-picker buttons. Returns
    (info, entries, filepaths)."""
    url = f"https://www.youtube.com/watch?v={video_id}"

    if height_or_audio == "audio":
        selector = "bestaudio/best"
        extract_audio = True
    else:
        height = int(height_or_audio)
        selector = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best/all"
        extract_audio = False

    return _download_with_selector(url, selector, extract_audio, progress_hook)


def get_spotify_client():
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials

    auth = SpotifyClientCredentials(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
    )
    return spotipy.Spotify(client_credentials_manager=auth)


def _track_info(t: dict, album: dict | None = None) -> dict:
    album_data = album or t.get("album") or {}
    images = album_data.get("images") or []
    return {
        "name": t["name"],
        "artists": ", ".join(a["name"] for a in t.get("artists", [])),
        "album": album_data.get("name", ""),
        "cover_url": images[0]["url"] if images else None,
        "duration_ms": t.get("duration_ms", 0),
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

    tag_audio_file(filepath, title=track["name"], artist=track["artists"],
                    album=track["album"], cover_url=track["cover_url"])
    return filepath


def tag_audio_file(filepath: str, title: str = "", artist: str = "", album: str = "", cover_url: str | None = None) -> None:
    """Writes ID3 tags (and cover art, if a URL is given) directly into an
    mp3 file. Best-effort: tagging failures are swallowed rather than
    breaking a download that otherwise succeeded."""
    if not filepath.lower().endswith(".mp3"):
        return
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, APIC

        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            tags = ID3()

        if title:
            tags["TIT2"] = TIT2(encoding=3, text=title)
        if artist:
            tags["TPE1"] = TPE1(encoding=3, text=artist)
        if album:
            tags["TALB"] = TALB(encoding=3, text=album)

        if cover_url:
            import urllib.request
            with urllib.request.urlopen(cover_url, timeout=10) as resp:
                cover_bytes = resp.read()
            tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes)

        tags.save(filepath)
    except Exception:
        pass