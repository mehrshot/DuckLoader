"""
Which song is playing in a video ("🎵 Song in this video").

A few 12-second samples of the sound track are cut with ffmpeg and sent
to Shazam (the shazamio library: free, no key). If ACRCloud credentials
are configured (ACRCLOUD_HOST / ACRCLOUD_ACCESS_KEY / ACRCLOUD_ACCESS_SECRET)
it is asked too, when Shazam finds nothing or is unavailable. The caller
then downloads the full song with platforms.download_matching_audio, the
same matcher Spotify links use.

recognize() returns {"title", "artist", "album", "cover_url", "duration_ms"}
or None.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import time
import unicodedata

import requests

import platforms

logger = logging.getLogger(__name__)

SAMPLE_SECONDS = 12
MAX_SAMPLES = 3


_shazam_importable = None


def available() -> bool:
    """Whether any recognition engine can run (the button is hidden if not)."""
    global _shazam_importable
    if _shazam_importable is None:
        try:
            import shazamio
            _shazam_importable = shazamio is not None
        except Exception:
            logger.warning("shazamio is not installed (or fails to import); song recognition uses ACRCloud only")
            _shazam_importable = False
    return _shazam_importable or bool(_acrcloud_config())


def _duration(path):
    ffprobe = platforms._ffmpeg_exe("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", path],
            capture_output=True, text=True, timeout=60,
        )
        return float(json.loads(out.stdout or "{}").get("format", {}).get("duration") or 0) or None
    except Exception:
        return None


def _sample_offsets(duration):
    """Where to listen: music under a Reel usually runs throughout, but an
    intro or someone talking can hide it, so up to three spots."""
    if not duration or duration <= SAMPLE_SECONDS + 2:
        return [0.0]
    offsets = [duration * 0.25, duration * 0.6, 0.0]
    last_start = max(0.0, duration - SAMPLE_SECONDS)
    picked = []
    for offset in offsets:
        offset = round(min(offset, last_start), 1)
        if all(abs(offset - p) >= SAMPLE_SECONDS / 2 for p in picked):
            picked.append(offset)
    return picked[:MAX_SAMPLES]


def _cut_sample(path, offset, out_path):
    ffmpeg = platforms._ffmpeg_exe("ffmpeg")
    if not ffmpeg:
        raise Exception("ffmpeg not found.")
    result = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-ss", str(offset), "-t", str(SAMPLE_SECONDS), "-i", path,
         "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", out_path],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) < 16000:
        error = (result.stderr or "").strip()[-300:]
        if "matches no streams" in error or "does not contain any stream" in error:
            raise platforms.NoAudioError("This video has no sound track.")
        raise Exception(f"Could not cut an audio sample: {error}")
    return out_path


# --- engines ---

def _shazam(sample_path):
    try:
        from shazamio import Shazam
    except Exception:
        return None
    out = asyncio.run(Shazam().recognize(sample_path))
    track = out.get("track") if isinstance(out, dict) else None
    if not track or not track.get("title"):
        return None
    album = None
    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            if (item.get("title") or "").lower() == "album" and item.get("text"):
                album = item["text"]
    return {
        "title": track["title"].strip(),
        "artist": (track.get("subtitle") or "").strip(),
        "album": album,
        "cover_url": (track.get("images") or {}).get("coverarthq") or (track.get("images") or {}).get("coverart"),
        "duration_ms": None,
        "engine": "shazam",
    }


def _acrcloud_config():
    host = os.environ.get("ACRCLOUD_HOST", "").strip()
    key = os.environ.get("ACRCLOUD_ACCESS_KEY", "").strip()
    secret = os.environ.get("ACRCLOUD_ACCESS_SECRET", "").strip()
    return (host, key, secret) if host and key and secret else None


def _acrcloud(sample_path):
    config = _acrcloud_config()
    if not config:
        return None
    host, key, secret = config
    timestamp = str(int(time.time()))
    to_sign = "\n".join(["POST", "/v1/identify", key, "audio", "1", timestamp])
    signature = base64.b64encode(hmac.new(secret.encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
    with open(sample_path, "rb") as sample:
        response = requests.post(
            f"https://{host}/v1/identify",
            files={"sample": sample},
            data={
                "access_key": key,
                "sample_bytes": os.path.getsize(sample_path),
                "timestamp": timestamp,
                "signature": signature,
                "data_type": "audio",
                "signature_version": "1",
            },
            timeout=25,
        )
    payload = response.json()
    if (payload.get("status") or {}).get("code") != 0:
        return None
    music = ((payload.get("metadata") or {}).get("music") or [None])[0]
    if not music or not music.get("title"):
        return None
    return {
        "title": music["title"].strip(),
        "artist": ", ".join(a.get("name", "") for a in music.get("artists") or [] if a.get("name")),
        "album": (music.get("album") or {}).get("name"),
        "cover_url": None,
        "duration_ms": music.get("duration_ms"),
        "engine": "acrcloud",
    }


def song_key(song):
    """Same song, however it was found: normalized artist + title."""
    return platforms._normalize_text(song.get("artist") or "") + "|" + platforms._normalize_text(song.get("title") or "")


def recognize_all(media_path, exclude=(), spots=5, limit=4):
    """For "that's not the song": every distinct song heard at more spots of
    the file, by both engines, except the songs in `exclude` (song_key)."""
    if platforms.has_audio_stream(media_path) is False:
        raise platforms.NoAudioError("This video has no sound track.")
    duration = _duration(media_path)
    if not duration or duration <= SAMPLE_SECONDS + 2:
        offsets = [0.0]
    else:
        last_start = max(0.0, duration - SAMPLE_SECONDS)
        offsets = sorted({round(min(duration * f, last_start), 1) for f in (0.05, 0.25, 0.45, 0.65, 0.85)})[:spots]
    found, seen = [], set(exclude)
    job_dir = platforms._new_job_dir()
    try:
        for index, offset in enumerate(offsets):
            sample = _cut_sample(media_path, offset, os.path.join(job_dir, f"sample{index}.wav"))
            for engine in (_shazam, _acrcloud):
                try:
                    song = engine(sample)
                except Exception as e:
                    logger.warning("Song recognition (%s) failed: %s", engine.__name__.strip("_"), str(e)[:200])
                    continue
                if song and song_key(song) not in seen:
                    seen.add(song_key(song))
                    logger.info("Another song heard at %.0fs by %s: %s - %s",
                                offset, song["engine"], song["artist"], song["title"])
                    found.append(song)
            if len(found) >= limit:
                break
        return found
    finally:
        platforms._remove_tree(job_dir)


def recognize(media_path):
    """The song in an audio/video file, or None. Raises NoAudioError for a
    silent video."""
    if platforms.has_audio_stream(media_path) is False:
        raise platforms.NoAudioError("This video has no sound track.")
    job_dir = platforms._new_job_dir()
    try:
        for index, offset in enumerate(_sample_offsets(_duration(media_path))):
            sample = _cut_sample(media_path, offset, os.path.join(job_dir, f"sample{index}.wav"))
            for engine in (_shazam, _acrcloud):
                try:
                    song = engine(sample)
                except Exception as e:
                    logger.warning("Song recognition (%s) failed: %s", engine.__name__.strip("_"), str(e)[:200])
                    continue
                if song:
                    logger.info("Song recognized at %.0fs by %s: %s - %s",
                                offset, song["engine"], song["artist"], song["title"])
                    return song
        return None
    finally:
        platforms._remove_tree(job_dir)


# --- the song named in the post's caption --------------------------------
#
# Shazam matches recordings, so someone singing a song themselves is never
# recognized. Such posts often name the song in the caption ("کاور آهنگ X
# از Y", "cover of X by Y", "🎵 Y - X", hashtags). Candidates found there are
# searched on YouTube Music; a result is only accepted when it fits what the
# caption said, so a vague caption gives no answer rather than a wrong song.

_GENERIC_TAGS = {
    "cover", "covers", "coversong", "singing", "singer", "sing", "music", "song", "songs", "musician",
    "reels", "reel", "explore", "explorepage", "fyp", "foryou", "viral", "trending", "instagram", "live",
    "acoustic", "guitar", "piano", "vocal", "vocals", "karaoke", "iran", "tehran", "persian", "persianmusic",
    "کاور", "موسیقی", "موزیک", "آهنگ", "اهنگ", "خوانندگی", "خواننده", "اکسپلور", "ترند", "گیتار", "پیانو",
    "صدا", "آواز", "اجرا", "لایو", "ایران", "تهران",
}

_NAME = r"[^\n#«»\"“”'|،,.!؟?:]{2,60}"
_CAPTION_PATTERNS = [
    # Persian
    rf"کاور\s*(?:آهنگ|ترانه|موزیک)?\s*[«\"“']?(?P<title>{_NAME}?)[»\"”']?\s+(?:از|اثر|با\s*صدای)\s+(?P<artist>{_NAME})",
    rf"(?:آهنگ|ترانه)\s*[«\"“']?(?P<title>{_NAME}?)[»\"”']?\s+(?:از|اثر)\s+(?P<artist>{_NAME})",
    rf"[«\"“](?P<title>{_NAME})[»\"”]\s*(?:از|اثر)\s+(?P<artist>{_NAME})",
    # English
    rf"cover\s+of\s+[\"“']?(?P<title>{_NAME}?)[\"”']?\s+by\s+(?P<artist>{_NAME})",
    rf"[\"“](?P<title>{_NAME})[\"”]\s+by\s+(?P<artist>{_NAME})",
    rf"(?P<title>{_NAME}?)\s+by\s+(?P<artist>{_NAME}?)\s*\(?cover\)?",
]
_LABELED = r"(?:آهنگ|ترانه|موزیک|song|track|music)\s*[:：]\s*(?P<q>[^\n#]{3,80})"
_DASHED = r"^[\W_]*(?P<a>[^\n#\-–—|]{2,50}?)\s+[-–—|]\s+(?P<b>[^\n#\-–—|]{2,60}?)[\W_]*$"


def _clean(text):
    text = (text or "").replace("ي", "ی").replace("ك", "ک").replace("‌", " ")
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith(("S", "C")) or ch == "\n")
    return re.sub(r"[ \t]+", " ", text).strip(" -–—|:.,!،؟?")


def _is_persian(text):
    return bool(re.search(r"[؀-ۿ]", text or ""))


# Where an artist's name ends and the rest of the sentence starts:
# "از ابی رو خوندم", "by Adele hope you like it".
_ARTIST_STOP = re.compile(
    r"\s(?:رو|را|که|و|با|برای|توی|تو|خوندم|خوندیم|اجرا|امیدوارم|hope|i|im|i'm|lol|for|on|in|at|with|and|from|my|cover|covered)(?:\s|$)",
    re.IGNORECASE,
)


def _artist_part(text):
    artist = _ARTIST_STOP.split(" " + _clean(text) + " ", maxsplit=1)[0].strip()
    return " ".join(artist.split()[:4])


def _caption_candidates(text):
    """[(title or None, artist or None, query, strong)] from a caption."""
    text = text or ""
    found = []
    for line in text.splitlines():
        for pattern in _CAPTION_PATTERNS:
            for m in re.finditer(pattern, line, re.IGNORECASE):
                title, artist = _clean(m.group("title")), _artist_part(m.group("artist"))
                if len(title) >= 2 and len(artist) >= 2:
                    found.append((title, artist, f"{artist} {title}", True))
    for m in re.finditer(_LABELED, text, re.IGNORECASE):
        query = _clean(m.group("q"))
        if query:
            found.append((None, None, query, False))
    for m in re.finditer(_DASHED, text, re.MULTILINE):
        a, b = _clean(m.group("a")), _clean(m.group("b"))
        if a and b:
            found.append((None, None, f"{a} {b}", False))
    tags = [t.replace("_", " ") for t in re.findall(r"#([^\s#]+)", text)]
    tags = [t for t in tags if platforms._normalize_text(t).replace(" ", "") not in _GENERIC_TAGS and len(t) > 2]
    if tags:
        found.append((None, None, " ".join(tags[:4]), False))
    unique, seen = [], set()
    for item in found:
        key = platforms._normalize_text(item[2])
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:4]


def _fits(result, title, artist, caption_tokens):
    """Whether a YouTube Music result is the song the caption talks about."""
    result_title = platforms._normalize_text(result.get("title") or "").split()
    result_artists = [platforms._normalize_text(a.get("name") or "").split() for a in result.get("artists") or []]
    if not result_title or not result_artists:
        return False

    if title and artist:
        # What can be compared must match what the caption named: the title
        # both ways ("Perfect" is not "Perfect Strangers") and the artist.
        # Different scripts (caption "شادمهر عقیلی", result "Shadmehr
        # Aghili") can't be compared; a caption that clearly names both the
        # song and the artist is trusted there.
        result_artist_raw = " ".join(a.get("name") or "" for a in result.get("artists") or [])
        title_comparable = _is_persian(title) == _is_persian(result.get("title") or "")
        artist_comparable = _is_persian(artist) == _is_persian(result_artist_raw)
        if title_comparable:
            wanted = platforms._core_title(title).split()
            got = platforms._core_title(result.get("title") or "").split()
            if min(platforms._token_coverage(wanted, got), platforms._token_coverage(got, wanted)) < 0.6:
                return False
        if artist_comparable:
            wanted_artist = platforms._normalize_text(artist).split()
            if not any(platforms._token_coverage(wanted_artist, tokens) >= 0.6 or platforms._token_coverage(tokens, wanted_artist) >= 0.6
                       for tokens in result_artists if tokens):
                return False
        return True

    # A vague hint (hashtags, a "X - Y" line): both the title and an artist of
    # the result must appear in the caption itself.
    return (
        platforms._token_coverage(result_title, caption_tokens) >= 0.8
        and any(platforms._token_coverage(tokens, caption_tokens) >= 0.6 for tokens in result_artists if tokens)
    )


def from_caption(text):
    """The song a caption names, checked against YouTube Music, or None."""
    candidates = _caption_candidates(text)
    if not candidates or not platforms._ytmusic_available():
        return None
    from ytmusicapi import YTMusic

    caption_tokens = platforms._normalize_text(_clean(text).replace("_", " ")).split()
    ytmusic = YTMusic()
    for title, artist, query, strong in candidates:
        try:
            results = [r for r in ytmusic.search(query, filter="songs", limit=5) if r.get("videoId")][:5]
        except Exception as e:
            logger.warning("Caption song search failed | %s | %s", query, str(e)[:200])
            continue
        for result in results[: 3 if strong else 5]:
            if _fits(result, title if strong else None, artist if strong else None, caption_tokens):
                thumbnails = result.get("thumbnails") or []
                song = {
                    "title": result["title"].strip(),
                    "artist": ", ".join(a["name"] for a in result.get("artists") or [] if a.get("name")),
                    "album": (result.get("album") or {}).get("name"),
                    "cover_url": thumbnails[-1]["url"] if thumbnails else None,
                    "duration_ms": (result.get("duration_seconds") or 0) * 1000 or None,
                    "engine": "caption",
                }
                logger.info("Song found from the caption (%s): %s - %s", query, song["artist"], song["title"])
                return song
    return None


# --- search by name -------------------------------------------------------

def search(query, limit=5):
    """Songs on YouTube Music for a free-text query ("شادمهر تنها ماندم"),
    as song dicts with their video id, best match first."""
    if not platforms._ytmusic_available():
        return []
    from ytmusicapi import YTMusic

    try:
        results = YTMusic().search(query, filter="songs", limit=limit + 3)
    except Exception as e:
        logger.warning("Song search failed | %s | %s", query, str(e)[:200])
        return []
    songs = []
    for result in results:
        if not result.get("videoId") or not result.get("title"):
            continue
        thumbnails = result.get("thumbnails") or []
        songs.append({
            "title": result["title"].strip(),
            "artist": ", ".join(a["name"] for a in result.get("artists") or [] if a.get("name")),
            "album": (result.get("album") or {}).get("name"),
            "cover_url": thumbnails[-1]["url"] if thumbnails else None,
            "duration_ms": (result.get("duration_seconds") or 0) * 1000 or None,
            "video_id": result["videoId"],
            "engine": "search",
        })
        if len(songs) >= limit:
            break
    return songs


def download(song):
    """The full song as a tagged audio file: exactly the chosen YouTube
    Music track when its id is known, otherwise found by name and artist
    with the Spotify matcher."""
    if not song.get("video_id"):
        return platforms.download_matching_audio(as_track(song))
    _, _, files = platforms._youtube_download(
        f"https://www.youtube.com/watch?v={song['video_id']}", "bestaudio[ext=m4a]/bestaudio/best", True,
    )
    path = files[0]
    platforms.tag_audio_file(
        path, title=song["title"], artist=song.get("artist", ""), album=song.get("album") or "",
        cover_url=song.get("cover_url"),
    )
    return path


def as_track(song):
    """The shape platforms.download_matching_audio expects (a Spotify-like
    track). The length is only known from ACRCloud; without it the
    downloaded file's length isn't checked."""
    artists = [a.strip() for a in song.get("artist", "").replace(" & ", ", ").replace(" feat. ", ", ").split(",") if a.strip()]
    return {
        "name": song["title"],
        "artists": song.get("artist", ""),
        "artist_list": artists or [song.get("artist", "")],
        "album": song.get("album") or "",
        "cover_url": song.get("cover_url"),
        "duration_ms": song.get("duration_ms") or 0,
    }
