"""
Platform detection, media extraction, and audio tagging.

Instagram / YouTube / SoundCloud are downloaded directly with yt-dlp, which
supports all three natively. YouTube also has a second, per-video path (see
probe_youtube_qualities / download_youtube_quality) that inspects a specific
video's actual available resolutions and their real file sizes, for the
thumbnail-plus-buttons quality picker.

Spotify is different: Spotify's own audio streams are DRM-protected, so
there is no direct "download from Spotify" here. Instead, track metadata
(title, artist, album, cover art) is fetched from Spotify's official Web
API, and the matching audio is located and downloaded from YouTube. This is
the same approach the popular open-source spotDL project uses, and it never
touches Spotify's protected streams.
"""

import json
import os
import re

import yt_dlp

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

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov"}

QUALITY_LADDER = ["best", "720p", "audio"]

QUALITY_FORMATS = {
    "best": "best",
    "720p": "best[height<=720]/best",
    "audio": "bestaudio/best",
}

# Standard resolution tiers offered by the per-video YouTube quality picker.
YOUTUBE_RESOLUTION_TIERS = [2160, 1440, 1080, 720, 480, 360]


class FileTooLargeError(Exception):
    pass


def detect_platform(text: str):
    """Returns the platform key for the first recognized link in `text`, or None."""
    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(text):
            return platform
    return None


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


def _ffmpeg_location():
    """Optional explicit path to ffmpeg/ffprobe (set FFMPEG_LOCATION in .env).
    yt-dlp normally finds ffmpeg on PATH by itself; this is only needed when
    that lookup fails (e.g. PATH was updated but the terminal running the
    bot wasn't restarted)."""
    return os.environ.get("FFMPEG_LOCATION") or None


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
# worth retrying with a different client" rather than a real failure (link
# is private, deleted, etc.) that retrying won't fix.
_RETRYABLE_ERROR_HINTS = ("reload", "sign in", "not a bot", "confirm you", "unavailable")


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


def _extract_resilient(ydl_opts_base: dict, target: str, download: bool):
    """Runs yt-dlp's extraction, retrying with different YouTube
    player-client identities if an attempt fails with what looks like one
    of YouTube's bot/token checks. Non-YouTube extractions (Instagram,
    SoundCloud) just succeed on the first attempt, since player_client only
    affects youtube.com/youtu.be URLs and ytsearch: queries — this wrapper
    is used for every extraction rather than only YouTube ones so there's
    one code path to reason about."""
    last_error = None
    for clients in _client_attempts():
        opts = dict(ydl_opts_base)
        opts.update(_youtube_extra_opts(clients))
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=download)
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
        "noplaylist": False, "format": format_selector,
    }
    info, _ = _extract_resilient(probe_opts, url, download=False)

    entries = info.get("entries") or [info]
    total, known = 0, False
    for entry in entries:
        size = entry.get("filesize") or entry.get("filesize_approx")
        if size:
            total += size
            known = True

    if known and total > MAX_TELEGRAM_BYTES:
        raise FileTooLargeError(format_size(total))
    return total if known else None


def _download_with_selector(url: str, format_selector: str, extract_audio: bool, progress_hook=None):
    ydl_opts = {
        "format": format_selector,
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "color": "never",
        "noplaylist": False,
        "socket_timeout": 30,
    }
    if extract_audio:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    ffmpeg_location = _ffmpeg_location()
    if ffmpeg_location:
        ydl_opts["ffmpeg_location"] = ffmpeg_location
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    info, ydl = _extract_resilient(ydl_opts, url, download=True)
    entries = info["entries"] if info.get("_type") == "playlist" else [info]

    filepaths = []
    for entry in entries:
        raw_path = ydl.prepare_filename(entry)
        if extract_audio:
            mp3_path = os.path.splitext(raw_path)[0] + ".mp3"
            filepaths.append(mp3_path if os.path.exists(mp3_path) else raw_path)
        else:
            filepaths.append(raw_path)

    return info, entries, filepaths


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
        try:
            _probe_size(url, QUALITY_FORMATS[tier])
        except FileTooLargeError as e:
            last_error = e
            continue
        info, entries, filepaths = _download_with_selector(url, QUALITY_FORMATS[tier], tier == "audio", progress_hook)
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
    (options over MAX_TELEGRAM_BYTES are left out entirely)."""
    probe_opts = {"quiet": True, "no_warnings": True, "socket_timeout": 30, "noplaylist": True}
    info, _ = _extract_resilient(probe_opts, url, download=False)

    options = [o for o in _bucket_youtube_formats(info) if o["size_bytes"] <= MAX_TELEGRAM_BYTES]
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
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"

    if height_or_audio == "audio":
        selector = "bestaudio/best"
        extract_audio = True
    else:
        height = int(height_or_audio)
        selector = f"best[height<={height}]/bestvideo[height<={height}]+bestaudio/best"
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


def download_spotify_track(track: dict, progress_hook=None) -> str:
    """Finds and downloads the best-matching YouTube audio for a Spotify
    track, converts it to mp3, and tags it with Spotify's own metadata
    (more accurate than whatever title YouTube has). Returns the file path."""
    query = f"ytsearch1:{track['artists']} - {track['name']} audio"
    _, _, filepaths = _download_with_selector(query, "bestaudio/best", True, progress_hook)
    filepath = filepaths[0]

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
