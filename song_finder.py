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
import subprocess
import time

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


def recognize(media_path):
    """The song in an audio/video file, or None. Raises NoAudioError for a
    silent video."""
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
