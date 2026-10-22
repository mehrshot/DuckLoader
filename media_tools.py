"""
"✂️ Cut / GIF / ringtone" for videos: the user picks a tool and sends a
time range ("0:15-0:45"); ffmpeg makes the clip. Nothing here talks to
Telegram - bot_features handles the buttons and sending.

  * cut      - a part of the video (re-encoded for an exact start when
               short, stream-copied when long so it stays fast);
  * gif      - Telegram's "GIF": a silent, small MP4 (sent as an animation);
  * ringtone - an MP3 for any phone plus an .m4r for iPhone, with a short
               fade in/out.
"""

import json
import os
import re
import subprocess

import platforms

MAX_CUT_SECONDS = 180
EXACT_CUT_SECONDS = 60     # up to this the cut is re-encoded (exact start)
MAX_GIF_SECONDS = 15
RINGTONE_SECONDS = 30
MAX_RINGTONE_SECONDS = 40

TOOLS = ("cut", "gif", "ring")

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "0123456789" * 2)


class RangeError(ValueError):
    """`key` names the message to show the user; `args` fills it."""

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


def parse_range(text: str, tool: str):
    """(start, end) in seconds from "0:15-0:45", "15 تا 45", "از 1:10 تا 1:30",
    or for a ringtone just a start ("0:40", 30 s from there)."""
    text = (text or "").translate(_DIGITS).strip().lower()
    text = re.sub(r"^(?:از|from)\s+", "", text)
    text = re.sub(r"\s*(?:تا|to|–|—|-|_)\s*", "-", text)
    text = text.replace("٫", ".").replace("،", ":").replace(" ", "")
    pieces = [p for p in text.split("-") if p != ""]

    if tool == "ring" and len(pieces) == 1:
        start = _seconds(pieces[0])
        return start, start + RINGTONE_SECONDS
    if len(pieces) != 2:
        raise RangeError("tool_bad_time")
    start, end = _seconds(pieces[0]), _seconds(pieces[1])
    if end <= start:
        raise RangeError("tool_end_before_start")

    limit = {"cut": MAX_CUT_SECONDS, "gif": MAX_GIF_SECONDS, "ring": MAX_RINGTONE_SECONDS}[tool]
    if end - start > limit:
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


def _run(args, timeout=900):
    ffmpeg = platforms._ffmpeg_exe("ffmpeg")
    if not ffmpeg:
        raise Exception("ffmpeg not found.")
    result = subprocess.run([ffmpeg, "-y", "-v", "error", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise Exception(f"ffmpeg failed: {(result.stderr or '').strip()[-300:]}")


def make(tool, source, start, end):
    """Runs one tool. Returns the output file paths (a ringtone has two)."""
    job_dir = platforms._new_job_dir()
    length = end - start
    try:
        if tool == "cut":
            out = os.path.join(job_dir, "clip.mp4")
            if length <= EXACT_CUT_SECONDS:
                _run(["-ss", f"{start:.2f}", "-i", source, "-t", f"{length:.2f}",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out])
            else:
                # Long cuts: no re-encoding (fast), starting at the nearest
                # keyframe.
                _run(["-ss", f"{start:.2f}", "-i", source, "-t", f"{length:.2f}",
                      "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", out])
            return [out]

        if tool == "gif":
            out = os.path.join(job_dir, "gif.mp4")
            _run(["-ss", f"{start:.2f}", "-i", source, "-t", f"{length:.2f}", "-an",
                  "-vf", "fps=15,scale='min(480,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
                  "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out])
            return [out]

        if tool == "ring":
            if platforms.has_audio_stream(source) is False:
                raise platforms.NoAudioError("This video has no sound track.")
            fade_out = max(0.0, length - 1.5)
            fades = f"afade=t=in:d=1,afade=t=out:st={fade_out:.2f}:d=1.5"
            mp3 = os.path.join(job_dir, "ringtone.mp3")
            m4r = os.path.join(job_dir, "ringtone.m4r")
            common = ["-ss", f"{start:.2f}", "-i", source, "-t", f"{length:.2f}", "-vn", "-map", "0:a:0", "-af", fades]
            _run([*common, "-c:a", "libmp3lame", "-b:a", "192k", mp3])
            _run([*common, "-c:a", "aac", "-b:a", "192k", "-f", "ipod", m4r])
            return [mp3, m4r]

        raise ValueError(f"unknown tool {tool}")
    except BaseException:
        platforms._remove_tree(job_dir)
        raise
