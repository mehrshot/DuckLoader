"""
All persisted bot state, in one place: per-user settings (language +
download quality), feature flags (platform locks + bot-wide toggles),
known users (for /broadcast), banned users, and usage stats. Each is a
small JSON file next to the bot — plenty for this scale, no real database
needed.
"""

import json
import logging
import os
import threading
import time
import uuid

SETTINGS_FILE = "user_settings.json"
FLAGS_FILE = "feature_flags.json"
USERS_FILE = "known_users.json"
BANNED_FILE = "banned_users.json"
STATS_FILE = "stats.json"
ERROR_LOG_FILE = "error_log.json"
ERROR_LOG_MAX = 200

AD_REQUESTS_FILE = "ad_requests.json"

DUCK_REACTIONS_FILE = "duck_reactions.json"

DUCK_REACTION_KEYS = {
    "start",
    "downloading",
    "failed",
    "complete",
}

_error_log_lock = threading.Lock()
_ad_requests_lock = threading.Lock()

DEFAULT_QUALITY = "best"  # YouTube / general default quality
DEFAULT_INSTAGRAM_QUALITY = "best"
DEFAULT_LOW_DATA_MODE = False
DEFAULT_LANGUAGE = "en"

# True = enabled. Platform keys gate downloads (admin: /lock, /unlock).
# auto_quality_fallback is a bot-wide behavior toggle, not a platform
# (admin: /toggle) — kept in the same dict/file since it's the same shape
# of "on/off setting the owner flips."
DEFAULT_FLAGS = {
    "instagram": True,
    "soundcloud": True,
    "spotify": True,
    "youtube": False,
    "tiktok": True,
    "auto_quality_fallback": False,
    "ad_requests_button": True,
    "sponsor_channel_gate": True,
}


# Handlers run on several threads at once. Every read-modify-write of a
# JSON file happens under this lock, and every write goes to a unique temp
# file first — two threads used to share "<file>.tmp" and could replace
# each other's half-written data.
_io_lock = threading.RLock()


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        # A corrupted file must not take the whole bot down; keep a copy
        # for inspection and start from the default.
        logging.getLogger(__name__).exception("Could not read %s; using defaults", path)
        try:
            os.replace(path, f"{path}.corrupt-{int(time.time())}")
        except OSError:
            pass
        return default


def _save(path, data) -> None:
    with _io_lock:
        temp_path = f"{path}.{uuid.uuid4().hex}.tmp"

        try:
            with open(
                temp_path,
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    data,
                    f,
                    ensure_ascii=False,
                    separators=(
                        ",",
                        ":",
                    ),
                )
                f.flush()
                os.fsync(
                    f.fileno()
                )

            os.replace(
                temp_path,
                path,
            )
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


# --- feature flags (platform locks + bot-wide toggles) ---

def load_flags() -> dict:
    flags = DEFAULT_FLAGS.copy()
    flags.update(_load(FLAGS_FILE, {}))
    return flags


def save_flags(flags: dict) -> None:
    _save(FLAGS_FILE, flags)

# --- advertising requests ---

def load_ad_requests() -> dict:
    raw = _load(
        AD_REQUESTS_FILE,
        {},
    )

    if not isinstance(raw, dict):
        return {}

    return raw


def save_ad_requests(
    requests: dict,
) -> None:
    with _ad_requests_lock:
        _save(
            AD_REQUESTS_FILE,
            requests,
        )


def create_ad_request(
    user_id,
    telegram_username: str,
    telegram_name: str,
) -> dict:
    with _ad_requests_lock:
        requests = _load(
            AD_REQUESTS_FILE,
            {},
        )

        request_id = None

        while request_id is None or request_id in requests:
            request_id = uuid.uuid4().hex[:8].upper()

        created_at = time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        request = {
            "request_id": request_id,
            "ad_form_lang": "fa",
            "status": "draft",
            "step": "channel",
            "form_lang": "fa",
            "user_id": user_id,
            "telegram_username": telegram_username,
            "telegram_name": telegram_name,
            "channel": "",
            "display_name": "",
            "ad_type": "",
            "duration": "",
            "notes": "",
            "created_at": created_at,
            "updated_at": created_at,
            "admin_message_id": None,
        }

        requests[request_id] = request

        _save(
            AD_REQUESTS_FILE,
            requests,
        )

        return request


def get_ad_request(
    request_id: str,
):
    requests = load_ad_requests()

    return requests.get(
        str(request_id)
    )


def update_ad_request(
    request_id: str,
    **changes,
):
    with _ad_requests_lock:
        requests = _load(
            AD_REQUESTS_FILE,
            {},
        )

        request = requests.get(
            str(request_id)
        )

        if not isinstance(
            request,
            dict,
        ):
            return None

        request.update(
            changes
        )

        request["updated_at"] = (
            time.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

        requests[str(request_id)] = (
            request
        )

        _save(
            AD_REQUESTS_FILE,
            requests,
        )

        return request


def list_ad_requests(
    status: str = None,
) -> list:
    requests = load_ad_requests()

    result = []

    for request in requests.values():
        if not isinstance(
            request,
            dict,
        ):
            continue

        if (
            status is not None
            and request.get(
                "status"
            ) != status
        ):
            continue

        result.append(
            request
        )

    result.sort(
        key=lambda item: item.get(
            "created_at",
            "",
        ),
        reverse=True,
    )

    return result


def get_user_ad_request(
    user_id,
    status: str = None,
):
    requests = list_ad_requests()

    for request in requests:
        if request.get(
            "user_id"
        ) != user_id:
            continue

        if (
            status is not None
            and request.get(
                "status"
            ) != status
        ):
            continue

        return request

    return None

# --- per-user settings ---
# Old format (pre-v0.6) was {"chat_id": "fa"} — just a language code.
# New format is {"chat_id": {"lang": "fa", "quality": "best"}}.
# load_user_settings migrates old entries on the fly so nobody's saved
# language preference gets lost.

def load_user_settings() -> dict:
    raw = _load(
        SETTINGS_FILE,
        {},
    )

    migrated = {}

    for chat_id, value in raw.items():

        if isinstance(value, str):
            value = {
                "lang": value,
                "quality": DEFAULT_QUALITY,
            }
        elif not isinstance(value, dict):
            value = {}

        value.setdefault(
            "lang",
            DEFAULT_LANGUAGE,
        )

        value.setdefault(
            "quality",
            DEFAULT_QUALITY,
        )

        value.setdefault(
            "instagram_quality",
            DEFAULT_INSTAGRAM_QUALITY,
        )

        value.setdefault(
            "low_data_mode",
            DEFAULT_LOW_DATA_MODE,
        )

        if value.get("lang") not in {
            "fa",
            "en",
        }:
            value["lang"] = DEFAULT_LANGUAGE

        if value.get("quality") not in {
            "best",
            "720p",
            "audio",
        }:
            value["quality"] = DEFAULT_QUALITY

        if value.get("instagram_quality") not in {
            "best",
            "1080p",
            "720p",
            "480p",
            "360p",
        }:
            value["instagram_quality"] = (
                DEFAULT_INSTAGRAM_QUALITY
            )

        value["low_data_mode"] = bool(
            value.get(
                "low_data_mode",
                DEFAULT_LOW_DATA_MODE,
            )
        )

        migrated[str(chat_id)] = value

    return migrated


def save_user_settings(settings: dict) -> None:
    # dict() is an atomic snapshot, so another thread adding a user while
    # this one serializes can't raise "dictionary changed size".
    _save(SETTINGS_FILE, dict(settings))


def get_user(
    settings: dict,
    chat_id,
) -> dict:
    return settings.get(
        str(chat_id),
        {
            "lang": DEFAULT_LANGUAGE,
            "quality": DEFAULT_QUALITY,
            "instagram_quality": DEFAULT_INSTAGRAM_QUALITY,
            "low_data_mode": DEFAULT_LOW_DATA_MODE,
        },
    )


# --- known users, for /broadcast ---

_known_users_cache = None
_known_users_lock = threading.Lock()


def load_known_users() -> list:
    global _known_users_cache

    with _known_users_lock:
        if _known_users_cache is None:
            _known_users_cache = _load(
                USERS_FILE,
                [],
            )

        return _known_users_cache


def track_user(chat_id) -> None:
    global _known_users_cache

    with _known_users_lock:

        if _known_users_cache is None:
            _known_users_cache = _load(
                USERS_FILE,
                [],
            )

        if chat_id in _known_users_cache:
            return

        _known_users_cache.append(
            chat_id
        )

        _save(
            USERS_FILE,
            _known_users_cache,
        )

    with _io_lock:
        stats = load_stats()
        _daily_bucket(stats)["new_users"] += 1
        _save(STATS_FILE, stats)

# --- bans ---

_banned_users_cache = None
_banned_users_lock = threading.Lock()


def load_banned() -> list:
    global _banned_users_cache

    with _banned_users_lock:

        if _banned_users_cache is None:
            _banned_users_cache = _load(
                BANNED_FILE,
                [],
            )

        return _banned_users_cache


def is_banned(user_id) -> bool:
    global _banned_users_cache

    with _banned_users_lock:
        if _banned_users_cache is None:
            _banned_users_cache = _load(
                BANNED_FILE,
                [],
            )

        return user_id in _banned_users_cache


def ban_user(user_id) -> None:
    global _banned_users_cache

    with _banned_users_lock:

        if _banned_users_cache is None:
            _banned_users_cache = _load(
                BANNED_FILE,
                [],
            )

        if user_id in _banned_users_cache:
            return

        _banned_users_cache.append(
            user_id
        )

        _save(
            BANNED_FILE,
            _banned_users_cache,
        )


def unban_user(user_id) -> bool:
    global _banned_users_cache

    with _banned_users_lock:

        if _banned_users_cache is None:
            _banned_users_cache = _load(
                BANNED_FILE,
                [],
            )

        if user_id not in _banned_users_cache:
            return False

        _banned_users_cache.remove(
            user_id
        )

        _save(
            BANNED_FILE,
            _banned_users_cache,
        )

        return True


# --- usage stats ---

def load_stats() -> dict:
    stats = _load(STATS_FILE, {})
    if not isinstance(stats, dict):
        stats = {}
    stats.setdefault("downloads", {})
    stats.setdefault("errors", 0)
    return stats


# --- user feedback (suggestions / problem reports) ---

FEEDBACK_FILE = "feedback.json"
FEEDBACK_MAX = 1000


def add_feedback(
    *,
    user_id,
    username: str,
    name: str,
    kind: str,
    text: str,
    content_type: str,
) -> int:
    """Stores one suggestion/problem report and returns its number."""
    with _io_lock:
        data = _load(FEEDBACK_FILE, {})
        if not isinstance(data, dict):
            data = {}
        items = data.setdefault("items", [])
        feedback_id = int(data.get("last_id", 0)) + 1
        data["last_id"] = feedback_id
        items.append({
            "id": feedback_id,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user_id": user_id,
            "username": username,
            "name": name,
            "kind": kind,
            "text": text[:4000],
            "content_type": content_type,
            "replied": False,
        })
        data["items"] = items[-FEEDBACK_MAX:]
        _save(FEEDBACK_FILE, data)
        return feedback_id


def mark_feedback_replied(feedback_id) -> None:
    with _io_lock:
        data = _load(FEEDBACK_FILE, {})
        for item in (data.get("items") or []) if isinstance(data, dict) else []:
            if str(item.get("id")) == str(feedback_id):
                item["replied"] = True
                _save(FEEDBACK_FILE, data)
                return


# --- learned job durations (for the time-based progress bar) ---

TIMINGS_FILE = "timings.json"


def load_timings() -> dict:
    data = _load(TIMINGS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_timings(timings: dict) -> None:
    _save(TIMINGS_FILE, timings)


# Daily numbers (new users, active users, downloads) are what an advertiser
# asks for; keep about two months of them.
DAILY_STATS_KEEP_DAYS = 60


def _daily_bucket(stats: dict) -> dict:
    daily = stats.setdefault("daily", {})
    day = daily.setdefault(
        time.strftime("%Y-%m-%d"),
        {"new_users": 0, "downloads": 0, "active_users": []},
    )
    for old_day in sorted(daily)[:-DAILY_STATS_KEEP_DAYS]:
        del daily[old_day]
    return day


def record_download(platform: str, user_id=None) -> None:
    with _io_lock:
        stats = load_stats()
        stats["downloads"][platform] = stats["downloads"].get(platform, 0) + 1
        day = _daily_bucket(stats)
        day["downloads"] += 1
        if user_id is not None and user_id not in day["active_users"]:
            day["active_users"].append(user_id)
        _save(STATS_FILE, stats)


def record_source(source: str) -> None:
    """Counts a new user who arrived through a t.me/<bot>?start=<source>
    link (e.g. one link per influencer campaign)."""
    with _io_lock:
        stats = load_stats()
        sources = stats.setdefault("sources", {})
        sources[source] = sources.get(source, 0) + 1
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
    with _io_lock:
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

def load_duck_reactions() -> dict:
    """
    Load DuckLoader reactions.

    New format:

        {
            "start": [
                {
                    "type": "sticker",
                    "file_id": "..."
                },
                {
                    "type": "animation",
                    "file_id": "..."
                }
            ],
            "downloading": [],
            "failed": [],
            "complete": []
        }

    This function also accepts the old one-reaction format and
    automatically converts it to the new list format in memory.
    """

    raw = _load(
        DUCK_REACTIONS_FILE,
        {},
    )

    if not isinstance(raw, dict):
        raw = {}

    normalized = {}

    for event in DUCK_REACTION_KEYS:
        value = raw.get(
            event,
            [],
        )

        if isinstance(value, dict):
            if (
                value.get("type")
                and value.get("file_id")
            ):
                normalized[event] = [
                    {
                        "type": value["type"],
                        "file_id": value["file_id"],
                    }
                ]
            else:
                normalized[event] = []

        elif isinstance(value, list):
            cleaned = []

            for item in value:
                if not isinstance(item, dict):
                    continue

                media_type = item.get(
                    "type"
                )
                file_id = item.get(
                    "file_id"
                )

                if (
                    media_type in {
                        "sticker",
                        "animation",
                    }
                    and file_id
                ):
                    cleaned.append(
                        {
                            "type": media_type,
                            "file_id": file_id,
                        }
                    )

            normalized[event] = cleaned

        else:
            normalized[event] = []

    return normalized


def save_duck_reaction(
    event: str,
    media_type: str,
    file_id: str,
) -> None:
    """
    Append one reaction to a category.

    Multiple reactions are allowed for every category.
    """

    if event not in DUCK_REACTION_KEYS:
        raise ValueError(
            f"Invalid duck reaction event: {event}"
        )

    if media_type not in {
        "sticker",
        "animation",
    }:
        raise ValueError(
            f"Invalid duck reaction type: {media_type}"
        )

    with _io_lock:
        reactions = load_duck_reactions()

        reactions.setdefault(
            event,
            [],
        )

        reactions[event].append(
            {
                "type": media_type,
                "file_id": file_id,
            }
        )

        _save(
            DUCK_REACTIONS_FILE,
            reactions,
        )


def clear_duck_reaction(
    event: str,
) -> None:
    """
    Remove every reaction from one category.
    """

    if event not in DUCK_REACTION_KEYS:
        return

    with _io_lock:
        reactions = load_duck_reactions()

        reactions[event] = []

        _save(
            DUCK_REACTIONS_FILE,
            reactions,
        )


def clear_all_duck_reactions() -> None:
    """
    Remove every configured DuckLoader reaction.
    """

    reactions = {
        event: []
        for event in DUCK_REACTION_KEYS
    }

    _save(
        DUCK_REACTIONS_FILE,
        reactions,
    )


EXEMPT_FILE = "exempt_users.json"

def load_exempt_users() -> list:
    return _load(EXEMPT_FILE, [])

def is_exempt(user_id) -> bool:
    return int(user_id) in load_exempt_users()

def add_exempt_user(user_id) -> None:
    with _io_lock:
        users = load_exempt_users()
        if int(user_id) not in users:
            users.append(int(user_id))
            _save(EXEMPT_FILE, users)

def remove_exempt_user(user_id) -> bool:
    with _io_lock:
        users = load_exempt_users()
        if int(user_id) in users:
            users.remove(int(user_id))
            _save(EXEMPT_FILE, users)
            return True
        return False