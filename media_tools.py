"""
"✂️ Cut tools" for videos: the user picks a tool and sends a time range
("0:15-0:45"); ffmpeg makes the result. Nothing here talks to Telegram -
bot_features handles the buttons and sending.

  * cut      - a part of the video. Stream-copied (fast, no quality loss)
               when a keyframe sits within half a second of the requested
               start, or when the part is long; otherwise re-encoded so it
               starts exactly where asked;
  * gif      - Telegram's "GIF": a silent, small MP4 (sent as an animation);
  * ringtone - for Android an MP3 (up to 40 s), for iPhone an M4A of at
               most 30 s (iOS 26+ sets it from the Files app: Share -> Use as
               Ringtone), with a short fade in/out.
"""

import json
import os
import re
import subprocess

import platforms

# 0 = a cut may be as long as the video (long cuts are copied, which is
# cheap); set MEDIA_CUT_MAX_SECONDS in .env to cap it.
MAX_CUT_SECONDS = int(os.environ.get("MEDIA_CUT_MAX_SECONDS", "0") or 0)
EXACT_CUT_SECONDS = 60          # up to this a cut must start exactly
KEYFRAME_TOLERANCE = 0.5        # a keyframe this close counts as "exact"
MAX_GIF_SECONDS = 15
RINGTONE_SECONDS = 30
MAX_RINGTONE_SECONDS = {"android": 40, "iphone": 30}   # iPhone ringtones: 30 s max

TOOLS = ("cut", "gif", "ring")

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "0123456789" * 2)


class RangeError(ValueError):
    """`key` names the message to show the user; `values` fills it."""

    def __init__(self, key, **values):
        super().__init__(key)
        self.key = key
        self.values = values


def _seconds(token: str) -> float:
    token = token.strip().rstrip("sS")
    if not token:
        raise RangeError("tool_bad_time")
    parts = token.split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        raise RangeError("tool_bad_time")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def limit_for(tool, variant=None):
    """Longest allowed range in seconds (0 = no limit)."""
    if tool == "cut":
        return MAX_CUT_SECONDS
    if tool == "gif":
        return MAX_GIF_SECONDS
    if tool == "ring":
        return MAX_RINGTONE_SECONDS.get(variant or "android", 40)
    return 0  # "range": a YouTube section, as long as the video


RANGE_RE = re.compile(
    r"^\s*(?:از|from)?\s*[\d۰-۹٠-٩:.]+\s*(?:-|–|—|_|تا|to)\s*[\d۰-۹٠-٩:.]+\s*$", re.IGNORECASE,
)


def looks_like_range(text: str) -> bool:
    return bool(RANGE_RE.match(text or ""))


def parse_range(text: str, tool: str, variant=None):
    """(start, end) in seconds from "0:15-0:45", "15 تا 45", "از 1:10 تا 1:30",
    or for a ringtone just a start ("0:40", 30 s from there)."""
    text = (text or "").translate(_DIGITS).strip().lower()
    text = re.sub(r"^(?:از|from)\s+", "", text)
    text = re.sub(r"\s*(?:تا|to|–|—|-|_)\s*", "-", text)
    text = text.replace("٫", ".").replace("،", ":").replace(" ", "")
    pieces = [p for p in text.split("-") if p != ""]

    limit = limit_for(tool, variant)
    if tool == "ring" and len(pieces) == 1:
        start = _seconds(pieces[0])
        return start, start + min(RINGTONE_SECONDS, limit)
    if len(pieces) != 2:
        raise RangeError("tool_bad_time")
    start, end = _seconds(pieces[0]), _seconds(pieces[1])
    if end <= start:
        raise RangeError("tool_end_before_start")
    if limit and end - start > limit:
        raise RangeError("tool_too_long", limit=limit)
    return start, end


def duration_of(path):
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


def fit_range(start, end, duration):
    """Clamps the range to the video; raises when it starts after the end."""
    if duration:
        if start >= duration - 0.5:
            raise RangeError("tool_after_end", duration=int(duration))
        end = min(end, duration)
    return start, end


def keyframe_near(path, start, tolerance=KEYFRAME_TOLERANCE):
    """The video keyframe closest to `start` if one is within `tolerance`
    seconds, else None. Only a small window around `start` is read."""
    ffprobe = platforms._ffmpeg_exe("ffprobe")
    if not ffprobe:
        return None
    window_start = max(0.0, start - tolerance - 1)
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-read_intervals", f"{window_start:.2f}%{start + tolerance + 1:.2f}",
             "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    best = None
    for line in (out.stdout or "").splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2 or "K" not in parts[1]:
            continue
        try:
            pts = float(parts[0])
        except ValueError:
            continue
        if abs(pts - start) <= tolerance and (best is None or abs(pts - start) < abs(best - start)):
            best = pts
    return best


def cut_plan(path, start, end):
    """("copy", start) or ("encode", start): copying is fast and lossless but
    can only start on a keyframe."""
    keyframe = keyframe_near(path, start)
    if keyframe is not None:
        return "copy", keyframe
    if end - start > EXACT_CUT_SECONDS:
        return "copy", start   # long part: starting at the previous keyframe is fine
    return "encode", start


def _run(args, timeout=900):
    ffmpeg = platforms._ffmpeg_exe("ffmpeg")
    if not ffmpeg:
        raise Exception("ffmpeg not found.")
    result = subprocess.run([ffmpeg, "-y", "-v", "error", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise Exception(f"ffmpeg failed: {(result.stderr or '').strip()[-300:]}")


def make(tool, source, start, end, variant=None):
    """Runs one tool. Returns the output file path."""
    job_dir = platforms._new_job_dir()
    length = end - start
    try:
        if tool == "cut":
            out = os.path.join(job_dir, "clip.mp4")
            mode, start = cut_plan(source, start, end)
            length = end - start
            if mode == "copy":
                _run(["-ss", f"{start:.3f}", "-i", source, "-t", f"{length:.3f}",
                      "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", out])
            else:
                _run(["-ss", f"{start:.3f}", "-i", source, "-t", f"{length:.3f}",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out])
            return out

        if tool == "gif":
            out = os.path.join(job_dir, "gif.mp4")
            _run(["-ss", f"{start:.2f}", "-i", source, "-t", f"{length:.2f}", "-an",
                  "-vf", "fps=15,scale='min(480,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
                  "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out])
            return out

        if tool == "ring":
            if platforms.has_audio_stream(source) is False:
                raise platforms.NoAudioError("This video has no sound track.")
            fade_out = max(0.0, length - 1.5)
            fades = f"afade=t=in:d=1,afade=t=out:st={fade_out:.2f}:d=1.5"
            common = ["-ss", f"{start:.2f}", "-i", source, "-t", f"{length:.2f}", "-vn", "-map", "0:a:0", "-af", fades]
            if variant == "iphone":
                out = os.path.join(job_dir, "ringtone.m4a")
                _run([*common, "-c:a", "aac", "-b:a", "192k", out])
            else:
                out = os.path.join(job_dir, "ringtone.mp3")
                _run([*common, "-c:a", "libmp3lame", "-b:a", "192k", out])
            return out

        raise ValueError(f"unknown tool {tool}")
    except BaseException:
        platforms._remove_tree(job_dir)
        raise
