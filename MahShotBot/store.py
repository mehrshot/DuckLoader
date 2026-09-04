"""
All persisted bot state, in one place: per-user settings (language +
download quality), feature flags (platform locks + bot-wide toggles),
known users (for /broadcast), banned users, and usage stats. Each is a
small JSON file next to the bot — plenty for this scale, no real database
needed.
"""

import json
import os
import threading
import time

SETTINGS_FILE = "user_settings.json"
FLAGS_FILE = "feature_flags.json"
USERS_FILE = "known_users.json"
BANNED_FILE = "banned_users.json"
STATS_FILE = "stats.json"
ERROR_LOG_FILE = "error_log.json"
ERROR_LOG_MAX = 200

_error_log_lock = threading.Lock()

DEFAULT_QUALITY = "best"  # "best" | "720p" | "audio"

# True = enabled. Platform keys gate downloads (admin: /lock, /unlock).
# auto_quality_fallback is a bot-wide behavior toggle, not a platform
# (admin: /toggle) — kept in the same dict/file since it's the same shape
# of "on/off setting the owner flips."
DEFAULT_FLAGS = {
    "instagram": True,
    "soundcloud": True,
    "spotify": True,
    "youtube": False,
    "auto_quality_fallback": False,
}


def _load(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def _save(path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


# --- feature flags (platform locks + bot-wide toggles) ---

def load_flags() -> dict:
    flags = DEFAULT_FLAGS.copy()
    flags.update(_load(FLAGS_FILE, {}))
    return flags


def save_flags(flags: dict) -> None:
    _save(FLAGS_FILE, flags)


# --- per-user settings ---
# Old format (pre-v0.6) was {"chat_id": "fa"} — just a language code.
# New format is {"chat_id": {"lang": "fa", "quality": "best"}}.
# load_user_settings migrates old entries on the fly so nobody's saved
# language preference gets lost.

def load_user_settings() -> dict:
    raw = _load(SETTINGS_FILE, {})
    migrated = {}
    for chat_id, value in raw.items():
        if isinstance(value, str):
            migrated[chat_id] = {"lang": value, "quality": DEFAULT_QUALITY}
        else:
            value.setdefault(
                "lang",
                "en",
            )

            value.setdefault(
                "quality",
                DEFAULT_QUALITY,
            )
            migrated[chat_id] = value
    return migrated


def save_user_settings(settings: dict) -> None:
    _save(SETTINGS_FILE, settings)


def get_user(
    settings: dict,
    chat_id,
) -> dict:
    return settings.get(
        str(chat_id),
        {
            "lang": "en",
            "quality": DEFAULT_QUALITY,
        },
    )


# --- known users, for /broadcast ---

def load_known_users() -> list:
    return _load(USERS_FILE, [])


def track_user(chat_id) -> None:
    users = load_known_users()
    if chat_id not in users:
        users.append(chat_id)
        _save(USERS_FILE, users)


# --- bans ---

def load_banned() -> list:
    return _load(BANNED_FILE, [])


def is_banned(user_id) -> bool:
    return user_id in load_banned()


def ban_user(user_id) -> None:
    banned = load_banned()
    if user_id not in banned:
        banned.append(user_id)
        _save(BANNED_FILE, banned)


def unban_user(user_id) -> bool:
    banned = load_banned()
    if user_id in banned:
        banned.remove(user_id)
        _save(BANNED_FILE, banned)
        return True
    return False


# --- usage stats ---

def load_stats() -> dict:
    return _load(STATS_FILE, {"downloads": {}, "errors": 0})


def record_download(platform: str) -> None:
    stats = load_stats()
    stats["downloads"][platform] = stats["downloads"].get(platform, 0) + 1
    _save(STATS_FILE, stats)


def record_error(
    *,
    platform: str = "unknown",
    url: str = "",
    user_id=None,
    error: str = "",
) -> None:
    """
    Record the total error count and keep a bounded persistent
    history of the actual failures for the administrator.

    Technical yt-dlp errors are never shown to end users.
    """

    # Keep the existing global error counter.
    stats = load_stats()
    stats["errors"] = (
        stats.get("errors", 0) + 1
    )
    _save(STATS_FILE, stats)

    event = {
        "time": time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "platform": platform,
        "user_id": user_id,
        "url": url,
        "error": str(error)[:4000],
    }

    with _error_log_lock:

        errors = _load(
            ERROR_LOG_FILE,
            [],
        )

        errors.append(event)

        if len(errors) > ERROR_LOG_MAX:
            errors = errors[
                -ERROR_LOG_MAX:
            ]

        _save(
            ERROR_LOG_FILE,
            errors,
        )


def load_error_log() -> list:
    """
    Return the most recent detailed download errors.
    """

    return _load(
        ERROR_LOG_FILE,
        [],
    )