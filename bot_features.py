import contextlib
import copy
import json
import logging
import math
import os
import secrets
import re
import random
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime

import requests
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaAudio,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ForceReply,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultsButton,
    InputTextMessageContent,
    ReactionTypeEmoji,
    SwitchInlineQueryChosenChat,
)
from telebot import apihelper
import admin
import ads
import campaigns
import platforms
import store

# ---------------------------------------------------------------------------
# DuckLoader
# ---------------------------------------------------------------------------
# 🦆 A friendly duck that fetches media for the user.
# Keep the personality light in user-facing messages while keeping
# technical logs and admin messages precise.
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

BOT_SIGNATURE = "🦆 Downloaded with @DuckDownloader_Bot"

# Telegram captions are limited to 1024 characters.
MAX_CAPTION_LENGTH = 1000


class _BoundedCache(OrderedDict):
    """A dict that forgets its oldest entries past `max_items`, so the
    per-post button caches can't grow for as long as the bot stays up."""

    def __init__(self, max_items=5000):
        super().__init__()
        self._max_items = max_items
        self._lock = threading.Lock()

    def __setitem__(self, key, value):
        with self._lock:
            if key in self:
                self.move_to_end(key)
            super().__setitem__(key, value)
            while len(self) > self._max_items:
                self.popitem(last=False)


thumb_cache = _BoundedCache()
audio_source_cache = _BoundedCache()  # post_id -> original url, for the "get audio" button under video posts
last_link_messages = _BoundedCache()
caption_cache = _BoundedCache()

# cache_key -> what was sent last time (Telegram file_ids + caption data).
# When an influencer posts a Reel, hundreds of people send the same link;
# after the first download everyone else gets the already-uploaded file
# instantly, with zero extra requests to Instagram.
MEDIA_CACHE_TTL = int(os.environ.get("MEDIA_CACHE_TTL_HOURS", "72")) * 3600
media_cache = _BoundedCache(max_items=3000)

# (chat_id, link) pairs currently being downloaded, so a double-tap or an
# impatient resend doesn't start a second identical download.
_in_flight = set()
_in_flight_lock = threading.Lock()


def _claim_in_flight(key) -> bool:
    with _in_flight_lock:
        if key in _in_flight:
            return False
        _in_flight.add(key)
        return True


def _release_in_flight(key) -> None:
    with _in_flight_lock:
        _in_flight.discard(key)


# One lock per cache_key: when many people send the same fresh link at the
# same moment, the first one downloads it and the rest wait for that and
# are then served from media_cache instead of each downloading it again.
_cache_key_locks = _BoundedCache(max_items=2000)
_cache_key_locks_guard = threading.Lock()


def _cache_key_lock(key):
    with _cache_key_locks_guard:
        lock = _cache_key_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _cache_key_locks[key] = lock
        return lock


# One download at a time per group: someone pasting 20 links shouldn't fill
# the queue for everyone else.
_group_locks = _BoundedCache(max_items=2000)
_group_locks_guard = threading.Lock()


def _group_lock(chat_id):
    with _group_locks_guard:
        lock = _group_locks.get(chat_id)
        if lock is None:
            lock = threading.Lock()
            _group_locks[chat_id] = lock
        return lock


# YouTube picker callbacks: "yq_<requester id>_<video id>_<choice>".
YT_PICK_PREFIX = "yq_"
# (chat_id, requester_id, video_id) -> {"link_message_id": ...}
_yt_pick_context = _BoundedCache(max_items=2000)

# Inline mode
INLINE_GATE_CACHE_SECONDS = 600
_inline_tokens = _BoundedCache(max_items=5000)   # token -> what to download
_inline_gate_cache = {}                          # user_id -> (monotonic, passed)
_bot_admin_cache = {}                            # chat_id -> (monotonic, is_admin)
_bot_identity = {}


class _SwitchInlineButton(InlineKeyboardButton):
    """pyTelegramBotAPI drops switch_inline_query / _current_chat when they
    are "" (it tests truthiness), turning the button into a plain text
    button — which Telegram rejects in inline keyboards, so the whole inline
    answer failed and the user only saw a spinner. "" is the valid value for
    "just type @bot", so always send these fields when they're set."""

    def to_dict(self):
        json_dict = super().to_dict()
        if self.switch_inline_query is not None:
            json_dict["switch_inline_query"] = self.switch_inline_query
        if self.switch_inline_query_current_chat is not None:
            json_dict["switch_inline_query_current_chat"] = self.switch_inline_query_current_chat
        return json_dict


def _recent_title(metadata: dict, platform: str) -> str:
    """A short label for the "recent downloads" list in inline mode."""
    title = (metadata.get("track") or metadata.get("title") or "").strip()
    artist = (metadata.get("artist") or "").strip()
    channel = (metadata.get("channel") or metadata.get("uploader") or "").strip()

    if platform == "spotify" or (artist and title and platform == "soundcloud"):
        return f"{artist} - {title}" if artist else title
    if platform in ("instagram", "tiktok"):
        description = " ".join((metadata.get("description") or "").split())[:50]
        label = f"@{channel}" if channel else platform.title()
        return f"{label}: {description}" if description else label
    return title or channel or platform.title()


def _new_inline_token(data: dict) -> str:
    token = secrets.token_urlsafe(6)
    _inline_tokens[token] = dict(data, created=time.time())
    return token


def _as_int(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _file_ref(message):
    """(kind, file_id) of the media in a sent Telegram message."""
    if message is None:
        return None
    if getattr(message, "photo", None):
        return ("photo", message.photo[-1].file_id)
    for kind in ("video", "audio", "animation", "document"):
        media = getattr(message, kind, None)
        if media is not None and getattr(media, "file_id", None):
            return (kind, media.file_id)
    return None

user_settings = store.load_user_settings()

# --- rate limiting + concurrency cap ---
RATE_LIMIT_COUNT = 5          # max downloads...
RATE_LIMIT_WINDOW = 60        # ...per this many seconds, per user

# How many downloads run at once, bot-wide, plus a tighter cap per platform.
# Instagram gets its own limit because every Instagram request comes from
# the same server IP and logged-in session: too many in parallel is what
# gets a session rate-limited (HTTP 429) or challenged.
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "4"))
PLATFORM_CONCURRENCY = {
    "instagram": int(os.environ.get("INSTAGRAM_MAX_CONCURRENT", "3")),
}

_recent_downloads = defaultdict(list)  # user_id -> [timestamps]
_download_semaphore = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)
_platform_semaphores = {
    platform_key: threading.Semaphore(limit)
    for platform_key, limit in PLATFORM_CONCURRENCY.items()
}
AD_BUTTON_TEXT = "📣 تبلیغات در ربات"
FEEDBACK_BUTTON_TEXT = "💬 پیشنهاد و گزارش مشکل"
FEEDBACK_BUTTON_TEXTS = {FEEDBACK_BUTTON_TEXT, "💬 Feedback"}

# Feedback: user_id -> {"kind": "idea"|"bug", "since": monotonic}
FEEDBACK_WAIT_SECONDS = 15 * 60
FEEDBACK_LIMIT_PER_HOUR = 5
_feedback_waiting = {}
_feedback_recent = defaultdict(list)  # user_id -> [timestamps]
_feedback_lock = threading.Lock()
# owner_id -> (user_id, feedback_id) while the owner is typing a reply
_owner_reply_target = {}

_duck_status_messages = {}
_duck_complete_messages = {}

_duck_message_lock = threading.Lock()

# If this many requests are already waiting for a download slot, new ones
# get turned away immediately instead of growing an unbounded queue. Each
# waiting request occupies one handler thread, so BOT_WORKER_THREADS (bot.py)
# must stay well above MAX_CONCURRENT_DOWNLOADS + MAX_QUEUE_WAITING.
MAX_QUEUE_WAITING = int(os.environ.get("MAX_QUEUE_WAITING", "40"))
_queue_waiting_count = 0
_queue_lock = threading.Lock()

def _is_private_chat(chat_type) -> bool:
    return chat_type == "private"


class _DownloadSlot:
    """The semaphores one download holds; release() is safe to call twice."""

    def __init__(self, semaphores):
        self._semaphores = semaphores
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        for semaphore in reversed(self._semaphores):
            semaphore.release()


def _acquire_download_slot(
    bot,
    chat_id_int,
    status_msg,
    t,
    show_ui=True,
    platform=None,
):
    """Returns a _DownloadSlot once both the platform slot (if that platform
    has its own cap) and a bot-wide slot are held, or None if the queue is
    full. Always acquired platform-first, so there is no lock-order deadlock.

    In private chats the user sees their place in the queue.
    In groups/supergroups those messages are suppressed.
    """
    global _queue_waiting_count

    semaphores = []
    if platform in _platform_semaphores:
        semaphores.append(_platform_semaphores[platform])
    semaphores.append(_download_semaphore)

    acquired = []
    for semaphore in semaphores:
        if not semaphore.acquire(blocking=False):
            break
        acquired.append(semaphore)

    if len(acquired) == len(semaphores):
        return _DownloadSlot(acquired)

    with _queue_lock:
        if _queue_waiting_count >= MAX_QUEUE_WAITING:
            for semaphore in reversed(acquired):
                semaphore.release()

            if show_ui and status_msg is not None:
                try:
                    bot.edit_message_text(
                        t['server_busy'],
                        chat_id_int,
                        status_msg.message_id,
                    )
                except Exception:
                    pass
            return None

        _queue_waiting_count += 1
        position = _queue_waiting_count

    if show_ui and status_msg is not None:
        try:
            bot.edit_message_text(
                t['queued'].format(position=position),
                chat_id_int,
                status_msg.message_id,
            )
        except Exception:
            pass

    try:
        for semaphore in semaphores[len(acquired):]:
            semaphore.acquire()
            acquired.append(semaphore)
    finally:
        with _queue_lock:
            _queue_waiting_count -= 1

    return _DownloadSlot(acquired)

def _is_rate_limited(user_id) -> bool:
    now = time.time()
    recent = [ts for ts in _recent_downloads[user_id] if now - ts < RATE_LIMIT_WINDOW]
    recent.append(now)
    _recent_downloads[user_id] = recent
    return len(recent) > RATE_LIMIT_COUNT


def _render_bar(percent_str: str, width: int = 10) -> str:
    """Turns yt-dlp's '_percent_str' (e.g. ' 42.3%') into a block-character bar."""
    try:
        pct = float(percent_str.strip().rstrip('%'))
    except (ValueError, AttributeError):
        return "░" * width
    filled = max(0, min(width, round(width * pct / 100)))
    return "▓" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# Time-based progress
# ---------------------------------------------------------------------------
#
# On a fast server yt-dlp's byte progress jumps from 0% to done in a second,
# and it never covered the upload to Telegram at all — so the old bar sat at
# 0-3% and then the file just appeared. Instead, the bot learns how long each
# kind of job really takes (download slot acquired -> file delivered) and a
# ticker moves the bar and the "time left" along that estimate.

PROGRESS_EDIT_INTERVAL = 2.5  # seconds between edits of the status message

# First guesses, used until a kind of job has been measured a few times.
DEFAULT_EXPECTED_SECONDS = {
    "instagram": 8,
    "tiktok": 8,
    "soundcloud": 12,
    "youtube": 25,
    "spotify": 20,
}


class _DurationModel:
    """Exponential moving average of real job durations per job kind
    (e.g. "instagram:media", "youtube:video", "spotify:track"), persisted in
    timings.json. For jobs whose size is known up front (the YouTube quality
    picker) it also learns seconds-per-megabyte."""

    ALPHA = 0.25  # weight of the newest measurement

    def __init__(self):
        self._lock = threading.Lock()
        self._data = store.load_timings()

    def expected(self, key, size_bytes=None) -> float:
        with self._lock:
            if size_bytes:
                per_mb = self._data.get(f"{key}:per_mb")
                if per_mb and per_mb.get("n", 0) >= 2:
                    return max(4.0, per_mb["avg"] * size_bytes / (1024 * 1024))
            entry = self._data.get(key)
            if entry and entry.get("n", 0) >= 1:
                return max(2.0, entry["avg"])
        return float(DEFAULT_EXPECTED_SECONDS.get(key.split(":")[0], 12))

    def record(self, key, seconds, size_bytes=None) -> None:
        seconds = min(max(float(seconds), 0.5), 1800.0)
        with self._lock:
            self._update(key, seconds)
            if size_bytes and size_bytes > 512 * 1024:
                self._update(f"{key}:per_mb", seconds / (size_bytes / (1024 * 1024)))
            snapshot = {k: dict(v) for k, v in self._data.items()}
        try:
            store.save_timings(snapshot)
        except Exception:
            logger.exception("Could not save job timings")

    def _update(self, key, value) -> None:
        entry = self._data.get(key)
        if entry is None:
            self._data[key] = {"avg": value, "n": 1}
        else:
            entry["avg"] = entry["avg"] * (1 - self.ALPHA) + value * self.ALPHA
            entry["n"] = entry.get("n", 0) + 1


duration_model = _DurationModel()


def _format_eta(t: dict, seconds: float) -> str:
    seconds = int(math.ceil(seconds))
    if seconds < 60:
        return t["eta_seconds"].format(n=seconds)
    return t["eta_minutes"].format(m=seconds // 60, s=seconds % 60)


class _ProgressTicker:
    """Keeps the status message's bar moving on a timer, using the learned
    expected duration. Real yt-dlp byte progress can only push the bar
    forward, never back. It stays below 100% until the file is delivered,
    then the status message is deleted as before."""

    def __init__(self, bot, chat_id, status_msg, t, show_ui, expected_seconds):
        self._bot = bot
        self._chat_id = chat_id
        self._status_msg = status_msg
        self._t = t
        self.enabled = bool(show_ui and status_msg is not None)
        self._expected = max(float(expected_seconds), 2.0)
        self._started = time.monotonic()
        self._phase = "downloading"
        self._note = ""
        self._real_fraction = 0.0
        self._shown_percent = 0
        self._last_text = None
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        self._started = time.monotonic()
        if not self.enabled:
            return self
        self._render()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def set_expected(self, seconds):
        with self._lock:
            self._expected = max(float(seconds), 2.0)

    def set_phase(self, phase):
        with self._lock:
            self._phase = phase
        self._render()

    def set_note(self, note):
        with self._lock:
            self._note = note or ""
        self._render()

    def hook(self, d):
        """yt-dlp progress hook: real byte progress, if it's ahead of time."""
        if d.get("status") != "downloading":
            return
        try:
            fraction = float(str(d.get("_percent_str") or "0").strip().rstrip("%")) / 100
        except ValueError:
            return
        with self._lock:
            self._real_fraction = max(self._real_fraction, min(fraction, 1.0))

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _percent(self) -> int:
        ratio = self.elapsed() / self._expected
        if ratio <= 1:
            by_time = 90 * ratio
        else:
            # Running late: keep creeping toward (never reaching) 100%.
            by_time = 90 + 8 * (1 - math.exp(-(ratio - 1) * 1.5))
        # Downloading is roughly the first ~70% of a job; uploading the rest.
        by_bytes = 70 * self._real_fraction if self._phase == "downloading" else 0
        percent = int(min(max(by_time, by_bytes), 98))
        self._shown_percent = max(self._shown_percent, percent)  # never go backwards
        return self._shown_percent

    def _render(self):
        if not self.enabled or self._stop.is_set():
            return
        with self._lock:
            percent = self._percent()
            remaining = self._expected - self.elapsed()
            eta = _format_eta(self._t, remaining) if remaining >= 1 else self._t["progress_almost"]
            text = self._t["progress_line"].format(
                label=self._t[f"progress_{self._phase}"],
                bar=_render_bar(f"{percent}%"),
                percent=f"{percent}%",
                eta=eta,
            )
            if self._note:
                text = f"{self._note}\n\n{text}"
            if text == self._last_text:
                return
            self._last_text = text
        try:
            self._bot.edit_message_text(text, self._chat_id, self._status_msg.message_id)
        except Exception:
            pass  # a failed edit must never affect the download

    def _run(self):
        while not self._stop.wait(PROGRESS_EDIT_INTERVAL):
            self._render()


def _files_size(paths) -> int:
    total = 0
    for path in paths or []:
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    return total


TEXTS = {
    'fa': {
        'welcome': (
            "🦆 **به داکلودر خوش اومدی!**\n\n"
            "کافیه لینکت رو بفرستی، "
            "می‌رم پیداش می‌کنم و برات برمی‌گردونمش. ⚡\n\n"
            "فعلا هم از اینستاگرام، یوتیوب، تیک تاک، اسپاتیفای و ساندکلاد "
            "پشتیبانی می‌کنم.\n\n"
            "زبان و کیفیت دلخواهت رو میتونی از /settings انتخاب کنی.\n\n"
            "📖 راهنمای کامل و قابلیت‌های جدید: /help\n"
            "💬 پیشنهاد یا مشکلی داشتی؟ از دکمه‌ی پایین صفحه یا /feedback برامون بفرست.\n\n"
            "🦆 لینکتو بفرست تا شروع کنیم!"
        ),        'init': "⏳ در حال برقراری ارتباط...",
        'downloading': "🔄 **در حال دانلود** {bar} {percent}\n\n📦 حجم: {size}\n⏱ زمان: {eta}",
        'progress_line': "{label} {bar} {percent}\n\n⏱ زمان تقریبی باقی‌مانده: {eta}",
        'feedback_choose': "💬 چه چیزی می‌خوای برامون بفرستی؟",
        'feedback_idea_btn': "💡 پیشنهاد قابلیت جدید",
        'feedback_bug_btn': "🐞 گزارش مشکل",
        'feedback_cancel_btn': "❌ انصراف",
        'feedback_prompt_idea': "💡 پیشنهادت رو بنویس. هر ایده‌ای که ربات رو بهتر می‌کنه مستقیم به دست مدیر می‌رسه. 🦆\n\n(برای لغو: /cancel)",
        'feedback_prompt_bug': "🐞 مشکل رو توضیح بده: چه لینکی فرستادی و چه اتفاقی افتاد؟ اگه اسکرین‌شات داری اونم می‌تونی بفرستی.\n\n(برای لغو: /cancel)",
        'feedback_thanks': "✅ پیامت به دست مدیر رسید. ممنون که کمک می‌کنی داکلودر بهتر بشه! 🦆",
        'feedback_cancelled': "❌ لغو شد.",
        'feedback_too_many': "⏳ توی یک ساعت اخیر چند پیام فرستادی؛ لطفاً کمی بعد دوباره امتحان کن.",
        'feedback_private_only': "💬 برای ارسال پیشنهاد یا گزارش مشکل، در چت خصوصی با ربات /feedback رو بزن.",
        'feedback_unsupported': "لطفاً متن، عکس، ویدیو، ویس یا فایل بفرست (برای لغو: /cancel).",
        'feedback_failed': "❌ ارسال پیام انجام نشد. لطفاً چند دقیقه‌ی دیگه دوباره امتحان کن.",
        'feedback_hint': "💬 هر پیشنهاد یا مشکلی داشتی، از دکمه‌ی پایین صفحه برامون بفرست.",
        'feedback_reply_prefix': "📩 پاسخ مدیر داکلودر به پیامت:",
        'fb_admin_idea': "💡 پیشنهاد جدید",
        'fb_admin_bug': "🐞 گزارش مشکل",
        'fb_admin_reply_btn': "💬 پاسخ به کاربر",
        'fb_admin_reply_prompt': "✍️ پاسخت به کاربر {id} رو بفرست (متن، عکس یا ویس). برای لغو: /cancel",
        'fb_admin_reply_sent': "✅ پاسخ برای کاربر ارسال شد.",
        'fb_admin_reply_failed': "❌ ارسال پاسخ ممکن نشد: {error}",
        'fb_admin_recent_errors': "🧾 آخرین خطاهای این کاربر:",
        'progress_downloading': "🔄 در حال دریافت",
        'progress_uploading': "📤 در حال ارسال",
        'progress_almost': "چند لحظه‌ی دیگه",
        'eta_seconds': "{n} ثانیه",
        'eta_minutes': "{m} دقیقه و {s} ثانیه",
        'uploading': "✅ دانلود تکمیل شد! در حال آپلود...",
        'failed': "❌ خطا: {error}",
        'download_failed': "❌ دانلود این لینک در حال حاضر انجام نشد. لطفاً مطمئن شو لینک قابل دسترسیه و دوباره امتحان کن.",
        'instagram_failed': "❌ دریافت محتوای اینستاگرام انجام نشد. لطفاً لینک رو بررسی کن و دوباره امتحان کن.",
        'instagram_unavailable': "❌ این استوری اینستاگرام در حال حاضر برای ربات قابل دسترسی نیست. ممکنه پرایوت باشه یا استوری دیگه در دسترس نباشه.",
        'youtube_failed': "❌ دریافت این ویدیوی یوتیوب در حال حاضر انجام نشد. لطفاً چند لحظه بعد دوباره امتحان کن.",
        'tiktok_failed': (
            "❌ دریافت این ویدیوی تیک تاک در حال حاضر انجام نشد. "
            "لطفاً لینک رو بررسی کن و دوباره امتحان کن."
        ),
        'soundcloud_failed': "❌ دریافت این ترک ساندکلاد در حال حاضر انجام نشد. لطفاً لینک رو بررسی کن و دوباره امتحان کن.",
        'spotify_failed': "❌ دریافت این ترک اسپاتیفای در حال حاضر انجام نشد. لطفاً چند لحظه بعد دوباره امتحان کن.",
        'ig_private': "🔒 این پست متعلق به یک پیج خصوصیه و ربات به اون دسترسی نداره.",
        'ig_restricted': "🔞 اینستاگرام این محتوا رو برای همه نمایش نمی‌ده (محدودیت سنی یا منطقه‌ای) و فعلاً قابل دریافت نیست.",
        'ig_expired': "⌛️ این استوری یا هایلایت دیگه در دسترس نیست؛ ممکنه منقضی یا حذف شده باشه، یا پیجش خصوصی باشه.",
        'ig_story_login': "❌ این استوری در حال حاضر برای ربات قابل دسترسی نیست. ممکنه پیج خصوصی باشه یا استوری منقضی شده باشه.",
        'ig_login': "⚠️ اینستاگرام موقتاً اجازه‌ی دسترسی به این محتوا رو نمی‌ده. لطفاً چند دقیقه‌ی دیگه دوباره امتحان کن.",
        'ig_rate_limited': "🚦 اینستاگرام موقتاً تعداد درخواست‌ها رو محدود کرده. لطفاً چند دقیقه‌ی دیگه دوباره امتحان کن.",
        'ig_profile': "ℹ️ این لینک یه پروفایل اینستاگرامه. لطفاً لینک یک پست، ریلز، استوری یا هایلایت رو بفرست.",
        'ig_audio': "ℹ️ صفحه‌ی «Audio» اینستاگرام قابل دانلود نیست. لینک خود ریلز رو بفرست و بعد از دانلود، دکمه‌ی «🎵 دریافت صدا» رو بزن.",
        'ig_unsupported': "ℹ️ این نوع لینک اینستاگرام پشتیبانی نمی‌شه. لطفاً لینک یک پست، ریلز، استوری یا هایلایت رو بفرست.",
        'not_found': "❌ این محتوا پیدا نشد؛ ممکنه حذف شده یا خصوصی باشه.",
        'ig_not_found': "❌ اینستاگرام این پست رو پیدا نکرد؛ احتمالاً حذف یا آرشیو شده، یا پیجش عمومی نیست.\n\nاگه تو اینستاگرام بدون مشکل باز میشه، از «💬 پیشنهاد و گزارش مشکل» خبرمون کن.",
        'platform_unavailable': "⏳ این سرویس الان جواب نمی‌ده. لطفاً چند دقیقه‌ی دیگه دوباره امتحان کن.",
        'already_downloading': "⏳ این لینک در حال دانلوده؛ چند لحظه صبر کن تا برسه.",
        'yt_not_yours': "🦆 فقط کسی که لینک رو فرستاده می‌تونه کیفیت رو انتخاب کنه.",
        'yt_pick_for': "👤 انتخاب کیفیت فقط با {name}",
        'group_intro': "🦆 سلام! من داکلودرم.\n\nهر لینک اینستاگرام، تیک‌تاک، یوتیوب، ساندکلاد یا اسپاتیفای که توی این گروه فرستاده بشه، فایلش رو همین‌جا میارم. لازم نیست کار دیگه‌ای بکنید، فقط لینک بفرستید!",
        'group_intro_needs_admin': "\n\n⚠️ برای اینکه لینک‌های گروه رو ببینم، باید منو ادمین گروه کنید (هیچ دسترسی خاصی لازم نیست).",
        'group_album_private': "🎵 آلبوم و پلی‌لیست اسپاتیفای رو توی چت خصوصی ربات بفرست تا گروه شلوغ نشه.",
        'open_bot_btn': "🦆 باز کردن داکلودر",
        'inline_bot_btn': "🦆 دانلود با داکلودر",
        'inline_help_btn': "🦆 لینک رو بعد از اسم ربات بذار",
        'inline_gate_btn': "🔒 برای استفاده، اول توی ربات عضو کانال‌ها شو",
        'inline_ready': "✅ آماده‌ست! حالا توی هر چتی بنویس @{bot} و یه فاصله، بعد لینک رو بذار تا فایل همون‌جا ارسال بشه.",
        'inline_download_title': "🦆 دانلود و ارسال همین‌جا",
        'inline_download_desc': "بزن تا فایل توی همین چت ارسال بشه",
        'inline_cached_title': "⚡️ ارسال فوری",
        'inline_placeholder': "⏳ اردک داره میاردش…",
        'inline_in_bot_btn': "🦆 دانلود داخل ربات",
        'inline_spotify_bot': "🎵 آلبوم و پلی‌لیست رو داخل ربات بفرست",
        'inline_yt_in_bot': "🎬 انتخاب کیفیت داخل ربات",
        'inline_more_btn': "➕ بقیه‌ی پست داخل ربات",
        'inline_next_btn': "🦆 دانلود یه لینک دیگه",
        'share_btn': "📤 فرستادن برای دوستات",
        'inline_try_btn': "📤 امتحانش کن",
        'inline_yt_title': "🎬 دانلود از یوتیوب",
        'inline_yt_desc': "کیفیت رو همین‌جا توی چت انتخاب کن",
        'inline_yt_loading': "🎬 {title}\n\n⏳ دارم کیفیت‌ها رو پیدا می‌کنم…",
        'inline_expired': "⌛️ این دکمه منقضی شده؛ لینک رو دوباره بفرست.",
        'inline_recent_desc': "دانلودهای اخیرت",
        'help': (
            "📖 راهنمای داکلودر 🦆\n\n"
            "📥 دانلود\n"
            "لینک رو همین‌جا بفرست تا فایلش رو برات بیارم:\n"
            "اینستاگرام (ریلز، پست، عکس، استوری و هایلایت)، یوتیوب، تیک‌تاک، ساندکلاد و اسپاتیفای.\n"
            "برای یوتیوب، کیفیت رو خودت انتخاب می‌کنی.\n\n"
            "💬 دانلود توی هر چتی (جدید)\n"
            "توی چت با دوستات یا هر گروهی بنویس:\n"
            "@{bot} لینک\n"
            "فایل همون‌جا ارسال میشه، بدون اینکه از چت بیرون بیای. کیفیت یوتیوب رو هم همون‌جا انتخاب می‌کنی.\n"
            "اگه فقط @{bot} رو بنویسی، دانلودهای اخیرت میاد تا دوباره بفرستیشون.\n"
            "زیر هر فایلی که اینجا دانلود می‌کنی هم دکمه‌ی «📤 فرستادن برای دوستات» هست.\n\n"
            "👥 داکلودر توی گروه (جدید)\n"
            "منو به گروهت اضافه کن؛ هر لینکی که توی گروه فرستاده بشه، فایلش رو همون‌جا میارم. "
            "کیفیت یوتیوب رو فقط کسی انتخاب می‌کنه که لینک رو فرستاده.\n\n"
            "⚙️ /settings — زبان و کیفیت اینستاگرام\n"
            "💬 /feedback — پیشنهاد یا گزارش مشکل"
        ),
        'help_try_inline_btn': "📤 امتحانش کن توی یه چت",
        'help_add_group_btn': "➕ اضافه کردن به گروه",
        'cache_ask_forward': "📦 یه پیام از کانال انبار (DuckLoader Cache) رو همین‌جا برام فوروارد کن.",
        'cache_set_done': "✅ کانال انبار وصل شد: {title}",
        'cache_set_failed': "❌ نتونستم توی اون کانال پیام بفرستم. مطمئن شو ربات ادمین کانال باشه و اجازه‌ی ارسال پیام داشته باشه.\n\n{error}",
        'stats_groups': "گروه‌های فعال",
        'stats_inline': "ارسال‌های اینلاین",
        'senddl_notice': "✅ مشکل لینک شما برطرف شد، فایلی که می‌خواستید در ادامه براتون ارسال می‌شه. 🦆",
        'tiktok_blocked': "🌍 تیک‌تاک دسترسی به این ویدیو رو از منطقه‌ی سرور ربات بسته و فعلاً قابل دریافت نیست.",
        'tiktok_login': "🔞 تیک‌تاک این ویدیو رو فقط برای کاربران واردشده نمایش می‌ده (محدودیت سنی) و فعلاً قابل دریافت نیست.",
        'tiktok_photo_audio': "ℹ️ این پست تیک‌تاک عکسیه و صدای جداگانه‌ای برای دانلود نداره.",
        'drm': "🔒 این ترک توسط ناشرش محافظت شده و نسخه‌ی جایگزینی هم ازش پیدا نشد.",
        'yt_private': "🔒 این ویدیوی یوتیوب خصوصی یا مخصوص اعضای کاناله.",
        'yt_age': "🔞 این ویدیوی یوتیوب محدودیت سنی داره و فعلاً قابل دریافت نیست.",
        'spotify_unsupported': "ℹ️ فقط لینک ترک، آلبوم یا پلی‌لیست اسپاتیفای پشتیبانی می‌شه.",
        'spotify_direct': "ℹ️ فقط لینک ترک، آلبوم یا پلی‌لیست اسپاتیفای پشتیبانی می‌شه.",
        'spotify_not_found': "❌ این لینک اسپاتیفای پیدا نشد یا خصوصیه (پلی‌لیست‌های شخصی‌سازی‌شده‌ی خود اسپاتیفای قابل دسترسی نیستن).",
        'spotify_partial': "⚠️ {failed} ترک از {total} ترک پیدا نشد:\n{names}",
        'quality_audio': "🎵 فقط صدا",
        'not_launched': "🚧 دانلود از {platform} هنوز لانچ نشده. به‌زودی فعال می‌شود!",
        'too_large': "⚠️ حجم این فایل حدود {size} است و از سقف مجاز بیشتره، پس امکان ارسالش نیست.\n\nمی‌تونی از /settings کیفیت پایین‌تر یا «فقط صدا» رو انتخاب کنی.",
        'quality_reduced': "ℹ️ به‌خاطر محدودیت حجم تلگرام، کیفیت به‌صورت خودکار به «{quality}» کاهش یافت.",
        'rate_limited': "⏳ توی یک دقیقه‌ی اخیر بیش از حد مجاز ({limit} تا) دانلود کردی. کمی صبر کن و دوباره امتحان کن.",
        'queued': "📋 توی صف دانلودی (نفر {position}) — به‌محض آزاد شدن ظرفیت شروع می‌شه...",
        'server_busy': "🚦 سرور الان خیلی شلوغه. چند دقیقه‌ی دیگه دوباره امتحان کن.",
        'spotify_searching': "🔎 در حال جست‌وجو ({i}/{total}): {name}",
        'view_link': "🔗 مشاهده در پلتفرم اصلی",
        'dl_cover': "🖼 دانلود کاور",
        'get_audio_btn': "🎵 دریافت صدا",
        'get_caption': '👁 دریافت کپشن',
        'hide_caption': '🙈 مخفی کردن کپشن',
        'audio_expired': "⚠️ این دکمه دیگه معتبر نیست (بات ری‌استارت شده). لینک رو دوباره بفرست.",
        'cover_loading': "⏳ در حال دریافت کاور...",
        'cover_error': "⚠️ کاور این پست یافت نشد.",
        'settings_msg': (
            "⚙️ **تنظیمات داکلودر**\n\n"
            "تنظیمات شخصی خودت را از بخش‌های زیر مدیریت کن.\n"
            "هر تغییر بلافاصله ذخیره می‌شود."
        ),

        'settings_quality': "🎬 کیفیت پیش‌فرض اینستاگرام",
        'settings_low_data': "📶 حالت مصرف اینترنت کم",
        'settings_language': "🌐 زبان",
        'settings_current': "📋 تنظیمات فعلی",
        'settings_reset': "♻️ بازنشانی تنظیمات",

        'settings_back': "↩️ بازگشت",
        'settings_close': "✖️ بستن",

        'instagram_quality_title': "🎬 **کیفیت پیش‌فرض اینستاگرام**",

        'instagram_quality_help': (
            "کیفیتی که اینجا انتخاب می‌کنی، برای دانلودهای اینستاگرام "
            "به‌صورت پیش‌فرض استفاده می‌شود.\n\n"
            "💎 بهترین: بالاترین کیفیت موجود\n"
            "📺 1080p: 1080p حداکثر\n"
            "📱 720p: 720p حداکثر\n"
            "🪶 480p: 480p حداکثر\n"
            "🪶 360p: 360p حداکثر"
        ),

        'instagram_quality_best': "💎 بهترین کیفیت",
        'instagram_quality_1080': "📺 1080p",
        'instagram_quality_720': "📱 720p",
        'instagram_quality_480': "🪶 480p",
        'instagram_quality_360': "🪶 360p",

        'low_data_title': "📶 **حالت مصرف اینترنت کم**",

        'low_data_help': (
            "وقتی این حالت فعال باشد، دانلودهای اینستاگرام "
            "به حداکثر 480p محدود می‌شوند تا حجم اینترنت کمتری مصرف شود.\n\n"
            "این گزینه فقط روی اینستاگرام تأثیر دارد و تنظیمات یوتیوب "
            "و سایر پلتفرم‌ها را تغییر نمی‌دهد."
        ),

        'low_data_on': "✅ فعال",
        'low_data_off': "⭕️ غیرفعال",

        'current_settings_title': "📋 **تنظیمات فعلی شما:**",

        'current_instagram_quality': "🎬 کیفیت اینستاگرام",
        'current_low_data': "📶 مصرف اینترنت کم",
        'current_language': "🌐 زبان",

        'language_title': "🌐 **زبان رابط کاربری**",
        'language_help': "زبان مورد استفاده در پیام‌ها و منوهای داکلودر را انتخاب کن.",

        'reset_title': "♻️ **بازنشانی تنظیمات**",

        'reset_warning': (
            "⚠️ همه تنظیمات شخصی شما به مقادیر پیش‌فرض برمی‌گردند.\n\n"
            "این کار قابل بازگشت نیست."
        ),

        'reset_confirm': "✅ بله، بازنشانی کن",
        'reset_cancel': "❌ انصراف",
        'reset_done': "✅ تنظیمات شما به حالت پیش‌فرض بازگردانده شد.",
        'not_owner': "⛔️ این دستور فقط برای مدیر بات است.",
        'lock_usage': "استفاده: /{cmd} <{options}>",
        'toggle_usage': "استفاده: /toggle <{options}>",
        'lock_done': "{icon} {name}: {status}",
        'status_locked': "قفل شد",
        'status_unlocked': "باز شد",
        'status_on': "روشن",
        'status_off': "خاموش",
        'stats_users': "کاربران",
        'stats_errors': "خطاها",
        'stats_daily_title': "📅 ۷ روز اخیر (کاربر جدید | کاربر فعال | دانلود):",
        'stats_sources_title': "🔗 کاربران جذب‌شده از لینک‌های start:",
        'broadcast_usage': "استفاده: /broadcast <پیام>",
        'broadcast_done': "✅ به {sent} کاربر ارسال شد ({failed} ناموفق).",
        'ban_usage': "استفاده: /{cmd} <user_id>",
        'ban_done': "🚫 کاربر {id} مسدود شد.",
        'unban_done': "✅ کاربر {id} از مسدودیت خارج شد.",
        'unban_not_found': "این کاربر مسدود نبود.",
        'setad_done': "✅ متن تبلیغ ذخیره شد. با /toggle sponsor_message نمایشش رو روشن/خاموش کن.",
        'setad_cleared': "متن تبلیغ خالی شد (چیزی نمایش داده نمی‌شه).",
        'addsponsor_usage': "استفاده: /addsponsor <@یوزرنیم> <نام نمایشی>",
        'addsponsor_done': "✅ {name} به لیست کانال‌های اسپانسر اضافه شد.",
        'addsponsor_reminder': "⚠️ یادت نره بات رو ادمین همون کانال کن، وگرنه نمی‌تونه عضویت رو چک کنه.",
        'removesponsor_usage': "استفاده: /removesponsor <@یوزرنیم>",
        'removesponsor_done': "✅ حذف شد.",
        'removesponsor_not_found': "همچین کانالی توی لیست نبود.",
        'sponsors_empty': "لیست کانال‌های اسپانسر خالیه — یعنی قفل عضویت برای هیچ‌کس فعال نیست.",
        'sponsor_gate_title': "🔸 برای استفاده‌ی رایگان از این بات، در چنل‌های اسپانسر عضو بشید:",
        'sponsor_gate_join': "{name}",
        'sponsor_gate_retry': (
            "بعد از عضویت، روی دکمه «✅ بررسی عضویت و ادامه» بزنید "
            "تا وضعیت عضویت شما بررسی بشه و دانلود ادامه پیدا کنه."
        ),
        'sponsor_gate_check': "✅ بررسی عضویت و ادامه",
        'sponsor_gate_not_joined': (
            "❌ هنوز عضو همه کانال‌های اسپانسر نشده‌اید.\n\n"
            "لطفاً عضو کانال‌های باقی‌مانده شوید و دوباره بررسی کنید."
        ),
        'sponsor_gate_confirmed': "✅ عضویت شما تأیید شد. دانلود شروع می‌شود.",
        'yt_choose_quality': "🎬 {title}\n\nکیفیت مورد نظر رو انتخاب کن:",
        'yt_no_quality': "⚠️ متأسفانه هیچ کیفیتی از این ویدیو زیر سقف مجاز نیست.",
        'ad_button': "📣 تبلیغات در ربات",
        'ad_channel_prompt': "📣 لطفاً آیدی، یوزرنیم یا لینک کانالی که می‌خواهید تبلیغ کنید رو ارسال کنید:",
        'ad_display_name_prompt': "🏷 نام نمایشی موردنظرتان برای تبلیغ رو وارد کنید:",
        'ad_type_prompt': "📌 نوع تبلیغ رو انتخاب کنید:",
        'ad_summary_title': "📋 خلاصه درخواست تبلیغات",
        'ad_summary_channel': "📣 کانال",
        'ad_summary_display_name': "🏷 نام نمایشی",
        'ad_summary_type': "📌 نوع تبلیغ",
        'ad_summary_duration': "⏱ مدت / تعداد نمایش",
        'ad_summary_notes': "📝 توضیحات",
        'ad_confirm_prompt': "آیا اطلاعات درخواست صحیح است؟",
        'ad_submit': "✅ ارسال درخواست",
        'ad_edit': "✏️ ویرایش",
        'ad_submitted': "✅ درخواست تبلیغات شما با موفقیت ثبت شد.\n\nمدیر درخواست شما را بررسی می‌کند و در صورت تأیید با شما هماهنگ خواهد شد.",
        'ad_cancelled': "❌ درخواست تبلیغات لغو شد.",
        'ad_unavailable': "⚠️ ثبت درخواست تبلیغات در حال حاضر غیرفعال است.",
        'ad_existing_pending': "⏳ شما یک درخواست تبلیغات در حال بررسی دارید.\n\nلطفاً تا بررسی درخواست قبلی منتظر بمانید.",
        'ad_admin_new': "📣 درخواست جدید تبلیغات",
        'ad_admin_user': "👤 کاربر",
        'ad_admin_username': "یوزرنیم تلگرام",
        'ad_admin_user_id': "Telegram ID",
        'ad_admin_name': "نام تلگرام",
        'ad_admin_channel': "📣 کانال / آیدی",
        'ad_admin_display_name': "🏷 نام نمایشی",
        'ad_admin_type': "📌 نوع تبلیغ",
        'ad_admin_duration': "⏱ مدت / تعداد نمایش",
        'ad_admin_notes': "📝 توضیحات",
        'ad_admin_request_id': "🆔 شماره درخواست",
        'ad_admin_created_at': "🕐 زمان ثبت",
        'ad_admin_approve': "✅ تأیید",
        'ad_admin_reject': "❌ رد",
        'ad_admin_contact': "💬 تماس با کاربر",
        'ad_admin_approved': "✅ درخواست تبلیغات شما تأیید شد.\n\nمدیر برای هماهنگی ادامه کار با شما در تماس خواهد بود.",
        'ad_admin_rejected': "❌ درخواست تبلیغات شما در حال حاضر تأیید نشد.",
        'ad_admin_requests_title': "📨 درخواست‌های تبلیغات",
        'ad_admin_requests_empty': "✅ هیچ درخواست تبلیغاتی در انتظار بررسی نیست.",
        'ad_admin_request_status_pending': "⏳ در انتظار بررسی",
        'ad_admin_request_status_approved': "✅ تأیید شده",
        'ad_admin_request_status_rejected': "❌ رد شده",
        'toggle_ad_requests_button': "📣 دکمه تبلیغات در ربات",
        'toggle_sponsor_channel_gate': "🔒 الزام عضویت در کانال‌های اسپانسر",
        'adm_title': "🛠 **پنل مدیریت**",
        'adm_platforms': "🔒 پلتفرم‌ها",
        'adm_toggles': "⚙️ تنظیمات کلی",
        'adm_ads': "📢 تبلیغات",
        'adm_users': "👥 کاربران",
        'adm_stats': "📊 آمار",
        'adm_errors': "🚨 خطاهای دانلود",
        'adm_errors_title': "🚨 آخرین خطاهای دانلود:",
        'adm_errors_empty': "✅ هیچ خطای ثبت‌شده‌ای وجود نداره.",
        'adm_mysettings': "🌐 تنظیمات شخصی من",
        'adm_commands': "📚 دستورات مدیر",
        'adm_commands_title': (
            "📚 **دستورات مدیر DuckLoader**\n\n"
            "/lock <platform> — قفل کردن پلتفرم\n"
            "/unlock <platform> — باز کردن پلتفرم\n"
            "/toggle <setting> — روشن/خاموش کردن تنظیم\n"
            "/stats — نمایش آمار\n"
            "/broadcast <message> — پیام همگانی\n"
            "/ban <user_id> — مسدود کردن کاربر\n"
            "/unban <user_id> — رفع مسدودی کاربر\n"
            "/setad <message> — تنظیم تبلیغ عمومی\n"
            "/checksponsor <channel> — بررسی دسترسی بات به کانال اسپانسر\n"
            "/addsponsor <channel> <name> — افزودن کانال اسپانسر\n"
            "/removesponsor <channel> — حذف کانال اسپانسر\n"
            "/sponsors — نمایش کانال‌های اسپانسر\n"
            "/senddl <user_id> <link> — ارسال دانلود یک لینک برای کاربر"
        ),
        'back': "⬅️ بازگشت",
        'adm_platforms_title': "کدوم پلتفرم رو می‌خوای قفل/باز کنی؟",
        'adm_toggles_title': "کدوم تنظیم رو می‌خوای روشن/خاموش کنی؟",
        'adm_ads_title': "بخش تبلیغات:",
        'adm_users_title': "مدیریت کاربران:",
        'adm_setad_btn': "✏️ تنظیم متن تبلیغ",
        'adm_addsponsor_btn': "➕ افزودن کانال اسپانسر",
        'adm_removesponsor_btn': "➖ حذف کانال اسپانسر",
        'adm_sponsorlist_btn': "📋 لیست کانال‌های اسپانسر",
        'adm_ban_btn': "🚫 مسدودکردن کاربر",
        'adm_unban_btn': "✅ رفع مسدودی کاربر",
        'adm_broadcast_btn': "📣 پیام همگانی",
        'adm_exempt_btn': "✅ معاف کردن از الزام عضویت",
        'adm_unexempt_btn': "❌ لغو معافیت عضویت",
        'adm_ask_setad': "متن تبلیغ جدید رو بفرست:",
        'adm_ask_addsponsor': "به این شکل بفرست: @یوزرنیم نام نمایشی",
        'adm_ask_removesponsor': "یوزرنیم کانالی که می‌خوای حذف کنی رو بفرست (با @):",
        'adm_ask_ban': "آی‌دی عددی کاربری که می‌خوای مسدود کنی رو بفرست:",
        'adm_ask_unban': "آی‌دی عددی کاربری که می‌خوای رفع مسدودیت کنی رو بفرست:",
        'adm_ask_broadcast': "متنی که می‌خوای برای همه‌ی کاربرها ارسال بشه رو بفرست:",
        'adm_ask_exempt': "آی‌دی عددی کاربری که می‌خوای از الزام عضویت در کانال‌های اسپانسر معاف کنی رو بفرست:",
        'adm_ask_unexempt': "آی‌دی عددی کاربری که می‌خوای معافیتش از الزام عضویت برداشته بشه رو بفرست:",
        'adm_cancelled': "لغو شد.",
        "adm_duck": "🦆 واکنش‌های اردک",
        "adm_duck_title": "🦆 تنظیم واکنش‌های DuckLoader:",
        "adm_duck_start": "👋 شروع",
        "adm_duck_downloading": "💨 دانلود",
        "adm_duck_failed": "😤 خطا",
        "adm_duck_complete": "👋 پایان",
        "adm_duck_clear_all": "🗑 پاک کردن همه",
        "adm_ask_duck_start": "🦆 استیکر یا GIF اردک برای شروع را ارسال کنید.",
        "adm_ask_duck_downloading": "🦆 استیکر یا GIF اردک در حال دانلود را ارسال کنید.",
        "adm_ask_duck_failed": "🦆 استیکر یا GIF اردک ناراحت را ارسال کنید.",
        "adm_ask_duck_complete": "🦆 استیکر یا GIF اردک «به‌زودی می‌بینمت» را ارسال کنید.",
        "adm_duck_saved": "✅ واکنش اردک برای «{event}» ذخیره شد.",
        "adm_duck_invalid_media": "❌ لطفاً یک استیکر یا GIF/Animation ارسال کنید.",
        "adm_duck_all_cleared": "✅ همه واکنش‌های اردک پاک شدند.",
        'adm_ad_requests': "📨 درخواست‌های تبلیغات",
        'adm_ad_requests_title': "📨 درخواست‌های تبلیغات",
        'adm_ad_requests_empty': "✅ هیچ درخواست تبلیغاتی در انتظار بررسی نیست.",
        'ad_lang_en': "🇺🇸 English",
        'ad_lang_fa': "🇮🇷 فارسی",
        'ad_type_sponsor_channel': "📢 کانال اسپانسر",
        'ad_type_post_download': "📣 تبلیغ بعد از هر دانلود",
        'ad_lang_en': "🇺🇸 English",
        'ad_lang_fa': "🇮🇷 فارسی",
    },
    'en': {
        'welcome': (
            "🦆 **Welcome to DuckLoader!**\n\n"
            "I’m your little download duck. Send me a link and "
            "I’ll waddle off, fetch it, and bring it back to you. ⚡\n\n"
            "I support Instagram, TikTok, SoundCloud, Spotify, "
            "and YouTube.\n\n"
            "Choose your preferred quality in /settings.\n\n"
            "📖 Full guide and new features: /help\n"
            "💬 Got an idea or a problem? Use the button below or /feedback.\n\n"
            "🦆 You bring the link. I’ll bring the media."
        ),        'init': "⏳ Initializing connection...",
        'downloading': "🔄 **Downloading** {bar} {percent}\n\n📦 Size: {size}\n⏱ ETA: {eta}",
        'progress_line': "{label} {bar} {percent}\n\n⏱ Estimated time left: {eta}",
        'feedback_choose': "💬 What would you like to send us?",
        'feedback_idea_btn': "💡 Suggest a feature",
        'feedback_bug_btn': "🐞 Report a problem",
        'feedback_cancel_btn': "❌ Cancel",
        'feedback_prompt_idea': "💡 Write your suggestion — every idea goes straight to the bot's admin. 🦆\n\n(To cancel: /cancel)",
        'feedback_prompt_bug': "🐞 Describe the problem: which link did you send and what happened? You can send a screenshot too.\n\n(To cancel: /cancel)",
        'feedback_thanks': "✅ Your message reached the admin. Thanks for helping make DuckLoader better! 🦆",
        'feedback_cancelled': "❌ Cancelled.",
        'feedback_too_many': "⏳ You've sent several messages in the last hour; please try again a bit later.",
        'feedback_private_only': "💬 To send a suggestion or report a problem, use /feedback in a private chat with the bot.",
        'feedback_unsupported': "Please send text, a photo, a video, a voice message or a file (to cancel: /cancel).",
        'feedback_failed': "❌ Your message couldn't be delivered. Please try again in a few minutes.",
        'feedback_hint': "💬 Got an idea or a problem? Send it to us with the button below.",
        'feedback_reply_prefix': "📩 The DuckLoader admin replied to your message:",
        'fb_admin_idea': "💡 New suggestion",
        'fb_admin_bug': "🐞 Problem report",
        'fb_admin_reply_btn': "💬 Reply to user",
        'fb_admin_reply_prompt': "✍️ Send your reply to user {id} (text, photo or voice). To cancel: /cancel",
        'fb_admin_reply_sent': "✅ Reply sent to the user.",
        'fb_admin_reply_failed': "❌ Couldn't send the reply: {error}",
        'fb_admin_recent_errors': "🧾 This user's latest errors:",
        'progress_downloading': "🔄 Fetching",
        'progress_uploading': "📤 Sending",
        'progress_almost': "a few more seconds",
        'eta_seconds': "{n}s",
        'eta_minutes': "{m}m {s}s",
        'uploading': "✅ Download complete! Preparing upload...",
        'failed': "❌ Failed: {error}",
        'download_failed': "❌ We couldn't download this link right now. Please check the link and try again.",
        'instagram_failed': "❌ We couldn't download this Instagram content right now. Please check the link and try again.",
        'instagram_unavailable': "❌ This Instagram story isn't currently accessible to the bot. It may require an Instagram login or may no longer be available.",
        'youtube_failed': "❌ We couldn't download this YouTube video right now. Please try again in a moment.",
        'tiktok_failed': (
            "❌ We couldn't download this TikTok video right now. "
            "Please check the link and try again."
        ),
        'soundcloud_failed': "❌ We couldn't download this SoundCloud track right now. Please check the link and try again.",
        'spotify_failed': "❌ We couldn't download this Spotify track right now. Please try again in a moment.",
        'ig_private': "🔒 This post belongs to a private account, so the bot can't access it.",
        'ig_restricted': "🔞 Instagram doesn't show this content to everyone (age or region restriction), so it can't be downloaded right now.",
        'ig_expired': "⌛️ This story or highlight is no longer available. It may have expired or been deleted, or the account may be private.",
        'ig_story_login': "❌ This Instagram story isn't currently accessible to the bot. The account may be private or the story may have expired.",
        'ig_login': "⚠️ Instagram is temporarily not allowing access to this content. Please try again in a few minutes.",
        'ig_rate_limited': "🚦 Instagram is temporarily limiting requests. Please try again in a few minutes.",
        'ig_profile': "ℹ️ That's an Instagram profile link. Please send the link of a post, Reel, story or highlight.",
        'ig_audio': "ℹ️ Instagram \"Audio\" pages can't be downloaded. Send the Reel's link instead, then tap \"🎵 Get Audio\" under the video.",
        'ig_unsupported': "ℹ️ This kind of Instagram link isn't supported. Please send the link of a post, Reel, story or highlight.",
        'not_found': "❌ This content couldn't be found — it may have been deleted or made private.",
        'ig_not_found': "❌ Instagram couldn't find this post — it was probably deleted or archived, or its account isn't public.\n\nIf it opens fine in Instagram, let us know via \"💬 Feedback\".",
        'platform_unavailable': "⏳ The service isn't responding right now. Please try again in a few minutes.",
        'already_downloading': "⏳ This link is already downloading — it'll arrive in a moment.",
        'yt_not_yours': "🦆 Only the person who sent the link can choose the quality.",
        'yt_pick_for': "👤 Only {name} can choose the quality",
        'group_intro': "🦆 Hi! I'm DuckLoader.\n\nSend any Instagram, TikTok, YouTube, SoundCloud or Spotify link in this group and I'll bring the file right here. Nothing else to do — just send links!",
        'group_intro_needs_admin': "\n\n⚠️ To see the group's links, please make me an admin (no special permissions needed).",
        'group_album_private': "🎵 Send Spotify albums and playlists in a private chat with the bot, so the group doesn't get flooded.",
        'open_bot_btn': "🦆 Open DuckLoader",
        'inline_bot_btn': "🦆 Download with DuckLoader",
        'inline_help_btn': "🦆 Put a link after the bot's name",
        'inline_gate_btn': "🔒 To use this, first join the channels in the bot",
        'inline_ready': "✅ All set! In any chat type @{bot}, a space, then the link — the file is sent right there.",
        'inline_download_title': "🦆 Download and send here",
        'inline_download_desc': "Tap to send the file in this chat",
        'inline_cached_title': "⚡️ Send instantly",
        'inline_placeholder': "⏳ The duck is fetching it…",
        'inline_in_bot_btn': "🦆 Download in the bot",
        'inline_spotify_bot': "🎵 Send albums and playlists in the bot",
        'inline_yt_in_bot': "🎬 Choose the quality in the bot",
        'inline_more_btn': "➕ Rest of the post in the bot",
        'inline_next_btn': "🦆 Download another link",
        'share_btn': "📤 Send to your friends",
        'inline_try_btn': "📤 Try it",
        'inline_yt_title': "🎬 Download from YouTube",
        'inline_yt_desc': "Choose the quality right here in the chat",
        'inline_yt_loading': "🎬 {title}\n\n⏳ Looking up the available qualities…",
        'inline_expired': "⌛️ This button has expired; please send the link again.",
        'inline_recent_desc': "Your recent downloads",
        'help': (
            "📖 DuckLoader guide 🦆\n\n"
            "📥 Downloading\n"
            "Send a link here and I'll bring you the file:\n"
            "Instagram (Reels, posts, photos, stories and highlights), YouTube, TikTok, SoundCloud and Spotify.\n"
            "For YouTube you choose the quality.\n\n"
            "💬 Download in any chat (new)\n"
            "In a chat with friends or in any group, type:\n"
            "@{bot} link\n"
            "The file is sent right there — you never leave the chat. YouTube quality is chosen right there too.\n"
            "Type just @{bot} to see your recent downloads and send them again.\n"
            "Files you download here also have a \"📤 Send to your friends\" button.\n\n"
            "👥 DuckLoader in groups (new)\n"
            "Add me to your group and I'll bring the file for every link anyone sends. "
            "Only the person who sent a YouTube link can choose its quality.\n\n"
            "⚙️ /settings — language and Instagram quality\n"
            "💬 /feedback — suggestions and problem reports"
        ),
        'help_try_inline_btn': "📤 Try it in a chat",
        'help_add_group_btn': "➕ Add to a group",
        'cache_ask_forward': "📦 Forward me any message from the cache channel (DuckLoader Cache).",
        'cache_set_done': "✅ Cache channel connected: {title}",
        'cache_set_failed': "❌ I couldn't post in that channel. Make sure the bot is an admin there and allowed to post.\n\n{error}",
        'stats_groups': "Active groups",
        'stats_inline': "Inline sends",
        'senddl_notice': "✅ The problem with your link has been fixed — the file you wanted is on its way. 🦆",
        'tiktok_blocked': "🌍 TikTok blocks this video in the bot server's region, so it can't be downloaded right now.",
        'tiktok_login': "🔞 TikTok only shows this video to logged-in users (age restriction), so it can't be downloaded right now.",
        'tiktok_photo_audio': "ℹ️ This TikTok post is a photo slideshow and has no separate audio to download.",
        'drm': "🔒 This track is protected by its publisher and no alternative copy could be found.",
        'yt_private': "🔒 This YouTube video is private or members-only.",
        'yt_age': "🔞 This YouTube video is age-restricted and can't be downloaded right now.",
        'spotify_unsupported': "ℹ️ Only Spotify track, album and playlist links are supported.",
        'spotify_direct': "ℹ️ Only Spotify track, album and playlist links are supported.",
        'spotify_not_found': "❌ This Spotify link wasn't found or is private (Spotify's own personalised playlists can't be accessed).",
        'spotify_partial': "⚠️ {failed} of {total} tracks couldn't be found:\n{names}",
        'quality_audio': "🎵 Audio only",
        'not_launched': "🚧 Downloading from {platform} hasn't launched yet. Stay tuned!",
        'too_large': "⚠️ This file is about {size}, over the allowed limit, so it can't be sent.\n\nYou can pick a lower quality or \"audio only\" in /settings.",
        'quality_reduced': "ℹ️ Quality was automatically reduced to \"{quality}\" to stay under Telegram's size limit.",
        'rate_limited': "⏳ You've hit the download limit ({limit}) for the last minute. Please wait a bit and try again.",
        'queued': "📋 You're in the download queue (position {position}) — it will start as soon as a slot frees up...",
        'server_busy': "🚦 The server is very busy right now. Please try again in a few minutes.",
        'spotify_searching': "🔎 Searching ({i}/{total}): {name}",
        'view_link': "🔗 View Original",
        'dl_cover': "🖼 Download Cover",
        'get_audio_btn': "🎵 Get Audio",
        'get_caption': '👁 Get Caption',
        'hide_caption': '🙈 Hide Caption',
        'audio_expired': "⚠️ This button is no longer valid (the bot restarted). Please resend the link.",
        'cover_loading': "⏳ Fetching cover...",
        'cover_error': "⚠️ Cover not found.",
        'settings_msg': (
            "⚙️ **DuckLoader Settings**\n\n"
            "Manage your personal preferences using the sections below.\n"
            "Changes are saved immediately."
        ),

        'settings_quality': "🎬 Instagram Default Quality",
        'settings_low_data': "📶 Low Data Mode",
        'settings_language': "🌐 Language",
        'settings_current': "📋 Current Settings",
        'settings_reset': "♻️ Reset Settings",

        'settings_back': "↩️ Back",
        'settings_close': "✖️ Close",

        'instagram_quality_title': "🎬 **Instagram Default Quality**",

        'instagram_quality_help': (
            "This setting controls the default quality used for Instagram downloads.\n\n"
            "💎 Best: highest available quality\n"
            "📺 1080p: up to 1080p\n"
            "📱 720p: up to 720p\n"
            "🪶 480p: up to 480p\n"
            "🪶 360p: up to 360p"
        ),

        'instagram_quality_best': "💎 Best Quality",
        'instagram_quality_1080': "📺 1080p",
        'instagram_quality_720': "📱 720p",
        'instagram_quality_480': "🪶 480p",
        'instagram_quality_360': "🪶 360p",

        'low_data_title': "📶 **Low Data Mode**",

        'low_data_help': (
            "When enabled, Instagram downloads are limited to a maximum of 480p "
            "to reduce data usage.\n\n"
            "This only affects Instagram. YouTube and other platforms are not changed."
        ),

        'low_data_on': "✅ Enabled",
        'low_data_off': "⭕️ Disabled",

        'current_settings_title': "📋 **Your Current Settings:**",

        'current_instagram_quality': "🎬 Instagram Quality",
        'current_low_data': "📶 Low Data Mode",
        'current_language': "🌐 Language",

        'language_title': "🌐 **Interface Language**",
        'language_help': "Choose the language used for DuckLoader messages and menus.",

        'reset_title': "♻️ **Reset Settings**",

        'reset_warning': (
            "⚠️ All of your personal settings will be returned to their defaults.\n\n"
            "This action cannot be undone."
        ),

        'reset_confirm': "✅ Yes, Reset",
        'reset_cancel': "❌ Cancel",
        'reset_done': "✅ Your settings have been reset to their defaults.",
        'not_owner': "⛔️ This command is for the bot admin only.",
        'lock_usage': "Usage: /{cmd} <{options}>",
        'toggle_usage': "Usage: /toggle <{options}>",
        'lock_done': "{icon} {name}: {status}",
        'status_locked': "locked",
        'status_unlocked': "unlocked",
        'status_on': "on",
        'status_off': "off",
        'stats_users': "Users",
        'stats_errors': "Errors",
        'stats_daily_title': "📅 Last 7 days (new users | active users | downloads):",
        'stats_sources_title': "🔗 Users who joined via start links:",
        'broadcast_usage': "Usage: /broadcast <message>",
        'broadcast_done': "✅ Sent to {sent} users ({failed} failed).",
        'ban_usage': "Usage: /{cmd} <user_id>",
        'ban_done': "🚫 User {id} banned.",
        'unban_done': "✅ User {id} unbanned.",
        'unban_not_found': "That user wasn't banned.",
        'setad_done': "✅ Ad text saved. Use /toggle sponsor_message to turn it on/off.",
        'setad_cleared': "Ad text cleared (nothing will be shown).",
        'addsponsor_usage': "Usage: /addsponsor <@username> <display name>",
        'addsponsor_done': "✅ {name} added to the sponsor channel list.",
        'addsponsor_reminder': "⚠️ Don't forget to make the bot an admin in that channel, or it can't check membership.",
        'removesponsor_usage': "Usage: /removesponsor <@username>",
        'removesponsor_done': "✅ Removed.",
        'removesponsor_not_found': "That channel wasn't in the list.",
        'sponsors_empty': "The sponsor channel list is empty — the join gate is off for everyone.",
        'sponsor_gate_title': "🔸 To use this bot for free, please join our sponsor channels.",
        'sponsor_gate_join': "{name}",
        'sponsor_gate_retry': (
            "After joining, tap "
            "\"✅ Check Membership & Continue\" below. "
            "Your download will then continue automatically."
        ),
        'sponsor_gate_check': "✅ Check Membership & Continue",
        'sponsor_gate_not_joined': (
            "❌ You're still not a member of all sponsor channels.\n\n"
            "Please join the remaining channel(s) and check again."
        ),
        'sponsor_gate_confirmed': "✅ Membership confirmed. Your download continues.",
        'yt_choose_quality': "🎬 {title}\n\nChoose a quality:",
        'yt_no_quality': "⚠️ Unfortunately no quality of this video is under the allowed size limit.",
        'ad_button': "📣 تبلیغات در ربات",
        'ad_channel_prompt': "📣 Send the channel ID, username, or link you want to advertise:",
        'ad_display_name_prompt': "🏷 Enter the display name you want for the advertisement:",
        'ad_type_prompt': "📌 Choose the advertisement type:",
        'ad_summary_title': "📋 Advertisement Request Summary",
        'ad_summary_channel': "📣 Channel",
        'ad_summary_display_name': "🏷 Display Name",
        'ad_summary_type': "📌 Ad Type",
        'ad_summary_duration': "⏱ Duration / Displays",
        'ad_summary_notes': "📝 Notes",
        'ad_confirm_prompt': "Is the information correct?",
        'ad_submit': "✅ Submit Request",
        'ad_edit': "✏️ Edit",
        'ad_submitted': "✅ Your advertising request has been submitted.\n\nThe admin will review it and contact you if it is approved.",
        'ad_cancelled': "❌ Advertising request cancelled.",
        'ad_unavailable': "⚠️ Advertising requests are currently disabled.",
        'ad_existing_pending': "⏳ You already have an advertising request awaiting review.\n\nPlease wait for the previous request to be reviewed.",
        'ad_admin_new': "📣 New Advertising Request",
        'ad_admin_user': "👤 User",
        'ad_admin_username': "Telegram Username",
        'ad_admin_user_id': "Telegram ID",
        'ad_admin_name': "Telegram Name",
        'ad_admin_channel': "📣 Channel / ID",
        'ad_admin_display_name': "🏷 Display Name",
        'ad_admin_type': "📌 Advertisement Type",
        'ad_admin_duration': "⏱ Duration / Displays",
        'ad_admin_notes': "📝 Notes",
        'ad_admin_request_id': "🆔 Request ID",
        'ad_admin_created_at': "🕐 Created",
        'ad_admin_approve': "✅ Approve",
        'ad_admin_reject': "❌ Reject",
        'ad_admin_contact': "💬 Contact User",
        'ad_admin_approved': "✅ Your advertising request has been approved.\n\nThe admin will contact you to coordinate the next steps.",
        'ad_admin_rejected': "❌ Your advertising request was not approved at this time.",
        'ad_admin_requests_title': "📨 Advertising Requests",
        'ad_admin_requests_empty': "✅ There are no advertising requests waiting for review.",
        'ad_admin_request_status_pending': "⏳ Pending",
        'ad_admin_request_status_approved': "✅ Approved",
        'ad_admin_request_status_rejected': "❌ Rejected",
        'toggle_ad_requests_button': "📣 Ad Requests Button",
        'toggle_sponsor_channel_gate': "🔒 Sponsor Channel Membership Requirement",
        'adm_title': "🛠 **Admin Panel**",
        'adm_platforms': "🔒 Platforms",
        'adm_toggles': "⚙️ General Settings",
        'adm_ads': "📢 Ads",
        'adm_users': "👥 Users",
        'adm_stats': "📊 Stats",
        'adm_errors': "🚨 Download Errors",
        'adm_errors_title': "🚨 Recent Download Errors:",
        'adm_errors_empty': "✅ No recorded download errors.",
        'adm_mysettings': "🌐 My Own Settings",
        'adm_commands': "📚 Admin Commands",
        'adm_commands_title': (
            "📚 **DuckLoader Admin Commands**\n\n"
            "/lock <platform> — Lock a platform\n"
            "/unlock <platform> — Unlock a platform\n"
            "/toggle <setting> — Toggle a bot setting\n"
            "/stats — Show bot statistics\n"
            "/broadcast <message> — Broadcast to users\n"
            "/ban <user_id> — Ban a user\n"
            "/unban <user_id> — Unban a user\n"
            "/setad <message> — Set the global ad\n"
            "/checksponsor <channel> — Check bot access to a sponsor channel\n"
            "/addsponsor <channel> <name> — Add a sponsor channel\n"
            "/removesponsor <channel> — Remove a sponsor channel\n"
            "/sponsors — List sponsor channels\n"
            "/senddl <user_id> <link> — Send a link's download to a user"
        ),
        'back': "⬅️ Back",
        'adm_platforms_title': "Which platform do you want to lock/unlock?",
        'adm_toggles_title': "Which setting do you want to turn on/off?",
        'adm_ads_title': "Ads section:",
        'adm_users_title': "User management:",
        'adm_setad_btn': "✏️ Set ad text",
        'adm_addsponsor_btn': "➕ Add sponsor channel",
        'adm_removesponsor_btn': "➖ Remove sponsor channel",
        'adm_sponsorlist_btn': "📋 List sponsor channels",
        'adm_ban_btn': "🚫 Ban a user",
        'adm_unban_btn': "✅ Unban a user",
        'adm_broadcast_btn': "📣 Broadcast message",
        'adm_exempt_btn': "✅ Exempt User from Membership Requirement",
        'adm_unexempt_btn': "❌ Remove Membership Exemption",
        'adm_ask_setad': "Send the new ad text:",
        'adm_ask_addsponsor': "Send it like this: @username Display Name",
        'adm_ask_removesponsor': "Send the channel's username to remove (with @):",
        'adm_ask_ban': "Send the numeric user ID to ban:",
        'adm_ask_unban': "Send the numeric user ID to unban:",
        'adm_ask_broadcast': "Send the message to broadcast to all users:",
        'adm_ask_exempt': "Send the numeric user ID to exempt from the sponsor-channel membership requirement:",
        'adm_ask_unexempt': "Send the numeric user ID whose sponsor-channel membership exemption should be removed:",
        'adm_cancelled': "Cancelled.",
        "adm_duck": "🦆 Duck Reactions",
        "adm_duck_title": "🦆 Configure DuckLoader reactions:",
        "adm_duck_start": "👋 Start",
        "adm_duck_downloading": "💨 Downloading",
        "adm_duck_failed": "😤 Failed",
        "adm_duck_complete": "👋 Complete",
        "adm_duck_clear_all": "🗑 Clear All",
        "adm_ask_duck_start": "🦆 Send the duck sticker or GIF/animation to use when a user starts.",
        "adm_ask_duck_downloading": "🦆 Send the jumping duck sticker or GIF/animation to show while downloading.",
        "adm_ask_duck_failed": "🦆 Send the frustrated duck sticker or GIF/animation to show when a download fails.",
        "adm_ask_duck_complete": "🦆 Send the goodbye/see-you-soon duck sticker or GIF/animation to show after a successful download.",
        "adm_duck_saved": "✅ Duck reaction for “{event}” saved.",
        "adm_duck_invalid_media": "❌ Please send a Sticker or GIF/Animation.",
        "adm_duck_all_cleared": "✅ All duck reactions cleared.",
        'adm_ad_requests': "📨 Ad Requests",
        'adm_ad_requests_title': "📨 Ad Requests",
        'adm_ad_requests_empty': "✅ There are no advertising requests waiting for review.",
        'ad_lang_en': "🇺🇸 English",
        'ad_lang_fa': "🇮🇷 فارسی",
        'ad_type_sponsor_channel': "📢 Sponsor Channel",
        'ad_type_post_download': "📣 Ad After Every Download",
        'ad_lang_en': "🇺🇸 English",
        'ad_lang_fa': "🇮🇷 فارسی",
    }
}

def _texts_for(chat_id) -> dict:
    # Groups have no /settings of their own; their audience is Persian.
    try:
        if int(chat_id) < 0 and str(chat_id) not in user_settings:
            return TEXTS["fa"]
    except (TypeError, ValueError):
        pass
    user = store.get_user(user_settings, chat_id)
    return TEXTS.get(user.get('lang'), TEXTS[store.DEFAULT_LANGUAGE])


def _quality_label(t: dict, quality: str) -> str:
    labels = {
        "best": t["instagram_quality_best"],
        "1080p": t["instagram_quality_1080"],
        "720p": t["instagram_quality_720"],
        "480p": t["instagram_quality_480"],
        "360p": t["instagram_quality_360"],
        "audio": t["quality_audio"],
    }
    return labels.get(quality, quality)

def _build_caption(
    entry: dict,
    source_url: str = "",
) -> str:
    """
    Build the caption for downloaded media.

    Instagram:
        - Use the Instagram public username, not the display name.
        - Prefer yt-dlp's "channel" field because it maps to the
          Instagram username.
        - For Story URLs, extract the username directly from the URL
          when possible.
        - Never use the numeric uploader_id as a hashtag.
        - Preserve the username as much as Telegram allows.
        - Replace unsupported hashtag characters with "_".
        - Prefix the Instagram hashtag with 🆔.

    Other platforms:
        - Keep the existing uploader-based hashtag behavior.
    """

    platform = (
        entry.get("extractor_key")
        or entry.get("extractor")
        or ""
    ).lower()

    is_instagram_story = bool(
        re.search(
            r"https?://(?:www\.)?instagram\.com/"
            r"stories/[^/?#]+/\d+",
            source_url,
            re.IGNORECASE,
        )
    )
    # ---------------------------------------------------------------
    # Instagram
    # ---------------------------------------------------------------

    if "instagram" in platform:

        instagram_username = ""

        # -----------------------------------------------------------
        # 1. yt-dlp's "channel" is the Instagram username.
        #
        # Current yt-dlp Instagram extractor:
        #
        #   channel      -> user.username
        #   uploader     -> user.full_name
        #   uploader_id  -> user.pk (numeric internal ID)
        #
        # Therefore channel MUST be preferred.
        # -----------------------------------------------------------

        channel = entry.get(
            "channel"
        )

        if channel:
            channel = str(
                channel
            ).strip()

            if (
                channel
                and not channel.isdigit()
                and "instagram.com" not in channel.lower()
            ):
                instagram_username = channel

        # -----------------------------------------------------------
        # 2. For Story URLs, the username is explicitly present:
        #
        #   /stories/USERNAME/STORY_ID/
        #
        # Use it when available.
        # -----------------------------------------------------------

        if (
            not instagram_username
            and source_url
        ):

            story_match = re.search(
                r"https?://(?:www\.)?instagram\.com/"
                r"stories/"
                r"([^/?#]+)/",
                source_url,
                re.IGNORECASE,
            )

            if story_match:

                possible_username = (
                    story_match.group(1)
                    .strip()
                    .lstrip("@")
                )

                if (
                    possible_username
                    and not possible_username.isdigit()
                ):
                    instagram_username = (
                        possible_username
                    )

        # -----------------------------------------------------------
        # 3. If channel is unavailable, try uploader only when it
        #    clearly looks like a username.
        #
        # Do NOT use uploader if it is a generic value such as
        # "Instagram" or a numeric ID.
        # -----------------------------------------------------------

        if not instagram_username:

            uploader = entry.get(
                "uploader"
            )

            if uploader:

                uploader = str(
                    uploader
                ).strip()

                generic_uploader_values = {
                    "instagram",
                    "instagram user",
                    "unknown",
                }

                if (
                    uploader
                    and not uploader.isdigit()
                    and uploader.lower()
                    not in generic_uploader_values
                    and "instagram.com"
                    not in uploader.lower()
                ):
                    instagram_username = (
                        uploader
                    )

        # -----------------------------------------------------------
        # 4. Do NOT fall back to uploader_id.
        #
        # uploader_id is often a numeric Instagram internal account
        # identifier such as:
        #
        #   123456789
        #
        # That is exactly what caused the numeric hashtags before.
        # -----------------------------------------------------------

        if not instagram_username:
            instagram_username = "Instagram"

        # Remove a leading @ if present.
        instagram_username = (
            instagram_username
            .lstrip("@")
            .strip()
        )

        # -----------------------------------------------------------
        # 5. Preserve the username.
        #
        # Only replace characters that aren't safe inside a hashtag.
        #
        # saadati.clothing -> saadati_clothing
        # trendspersian    -> trendspersian
        # my-shop          -> my_shop
        # my_shop          -> my_shop
        #
        # No translation.
        # No capitalization.
        # No shortening.
        # -----------------------------------------------------------

        safe_hashtag = re.sub(
            r"[^A-Za-z0-9_]",
            "_",
            instagram_username,
        )

        safe_hashtag = re.sub(
            r"_+",
            "_",
            safe_hashtag,
        ).strip("_")

        if not safe_hashtag:
            safe_hashtag = "Instagram"

        hashtag_line = (
            f"🆔 #{safe_hashtag}"
        )

    # ---------------------------------------------------------------
    # Other platforms
    # ---------------------------------------------------------------

    else:

        uploader = (
            entry.get("uploader")
            or entry.get("channel")
            or "Unknown"
        )

        safe_hashtag = re.sub(
            r"\W+",
            "_",
            uploader,
        ).strip("_")

        if not safe_hashtag:
            safe_hashtag = "Media"

        hashtag_line = (
            f"#{safe_hashtag}"
        )

    # ---------------------------------------------------------------
    # Date
    # ---------------------------------------------------------------

    timestamp = (
        entry.get("timestamp")
        or entry.get("release_timestamp")
    )

    date_str = None

    if timestamp:

        try:
            date_str = (
                datetime.fromtimestamp(
                    timestamp
                ).strftime(
                    "%Y/%m/%d, %H:%M"
                )
            )
        except (
            TypeError,
            ValueError,
            OSError,
        ):
            date_str = None

    # ---------------------------------------------------------------
    # Statistics
    # ---------------------------------------------------------------

    likes = (
        entry.get("like_count")
        or 0
    )

    comments = (
        entry.get("comment_count")
        or 0
    )

    views = None

    for view_key in (
        "view_count",
        "video_view_count",
        "video_play_count",
        "play_count",
    ):
        value = entry.get(view_key)

        if value is None or isinstance(value, bool):
            continue

        try:
            value = int(value)
        except (TypeError, ValueError):
            continue

        if value > 0:
            views = value
            break

    if views is None:
        views = 0

    def format_num(n):
        return (
            f"{n:,}"
            if isinstance(n, int)
            else n
        )

    stats_line = (
        f"❤️ {format_num(likes)} | "
        f"💬 {format_num(comments)} | "
        f"👁‍🗨 {format_num(views)}"
    )

    # ---------------------------------------------------------------
    # Description
    # ---------------------------------------------------------------

    description = (
        entry.get("description")
        or ""
    ).strip()

    # ---------------------------------------------------------------
    # Final caption
    # ---------------------------------------------------------------

    lines = [
        hashtag_line,
        f"👤 {entry.get('uploader') or entry.get('channel') or 'Unknown'}",
    ]

    if date_str:
        lines.append(
            f"📅 {date_str}"
        )

    if not is_instagram_story:
        lines.append(
            stats_line
        )

    header = "\n".join(lines)
    footer = f"\n\n{BOT_SIGNATURE}"

    if description:
        # Telegram counts caption length in UTF-16 units (emoji count
        # twice), so leave the description whatever room is actually left.
        def _utf16_len(text):
            return len(text.encode("utf-16-le")) // 2

        room = MAX_CAPTION_LENGTH - _utf16_len(header) - _utf16_len(footer) - 8
        if room > 20:
            if _utf16_len(description) > room:
                while description and _utf16_len(description) > room - 3:
                    description = description[:-10]
                description = description.rstrip() + "..."
            header += f"\n\n📝 {description}"

    return header + footer

def _friendly_download_error(
    platform: str,
    error: Exception,
    t: dict,
) -> str:
    """
    Convert internal downloader errors into a short,
    human-readable message.

    Never expose yt-dlp's raw exception text to the user.
    """

    key = platforms.classify_error(
        platform,
        error,
    )

    return (
        t.get(key)
        or t.get(f"{platform}_failed")
        or t["download_failed"]
    )

def _start_language_markup() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(
        row_width=2
    )

    markup.add(
        InlineKeyboardButton(
            text="🇺🇸 English",
            callback_data="startlang_en",
        ),
        InlineKeyboardButton(
            text="🇮🇷 فارسی",
            callback_data="startlang_fa",
        ),
    )

    return markup

def _settings_markup(
    user: dict,
    t: dict,
) -> InlineKeyboardMarkup:

    markup = InlineKeyboardMarkup(
        row_width=1
    )

    markup.add(
        InlineKeyboardButton(
            text=t["settings_quality"],
            callback_data="settings_quality",
        )
    )

    low_data_text = (
        f"{t['settings_low_data']} ✅"
        if user.get("low_data_mode", False)
        else t["settings_low_data"]
    )

    markup.add(
        InlineKeyboardButton(
            text=low_data_text,
            callback_data="settings_low_data",
        )
    )

    markup.add(
        InlineKeyboardButton(
            text=t["settings_language"],
            callback_data="settings_language",
        )
    )
    
    """markup.add(
        InlineKeyboardButton(
            text=t["settings_current"],
            callback_data="settings_current",
        )
    )"""
    
    markup.add(
        InlineKeyboardButton(
            text=t["settings_reset"],
            callback_data="settings_reset",
        )
    )

    return markup

def _instagram_quality_markup(
    user: dict,
    t: dict,
) -> InlineKeyboardMarkup:

    markup = InlineKeyboardMarkup(
        row_width=2
    )

    qualities = (
        (
            "best",
            t["instagram_quality_best"],
        ),
        (
            "1080p",
            t["instagram_quality_1080"],
        ),
        (
            "720p",
            t["instagram_quality_720"],
        ),
        (
            "480p",
            t["instagram_quality_480"],
        ),
        (
            "360p",
            t["instagram_quality_360"],
        ),
    )

    buttons = []

    for key, label in qualities:

        text = (
            f"{label} ✅"
            if user.get(
                "instagram_quality",
                "best",
            ) == key
            else label
        )

        buttons.append(
            InlineKeyboardButton(
                text=text,
                callback_data=f"igquality_{key}",
            )
        )

    for index in range(
        0,
        len(buttons),
        2,
    ):
        markup.row(
            *buttons[index:index + 2]
        )

    markup.add(
        InlineKeyboardButton(
            text=t["settings_back"],
            callback_data="settings_home",
        )
    )

    return markup


def _low_data_markup(
    user: dict,
    t: dict,
) -> InlineKeyboardMarkup:

    markup = InlineKeyboardMarkup(
        row_width=2
    )

    enabled = user.get(
        "low_data_mode",
        False,
    )

    markup.row(
        InlineKeyboardButton(
            text=(
                f"{t['low_data_on']} ✅"
                if enabled
                else t["low_data_on"]
            ),
            callback_data="lowdata_on",
        ),
        InlineKeyboardButton(
            text=(
                f"{t['low_data_off']} ✅"
                if not enabled
                else t["low_data_off"]
            ),
            callback_data="lowdata_off",
        ),
    )

    markup.add(
        InlineKeyboardButton(
            text=t["settings_back"],
            callback_data="settings_home",
        )
    )

    return markup


def _language_markup(
    user: dict,
    t: dict,
) -> InlineKeyboardMarkup:

    markup = InlineKeyboardMarkup(
        row_width=2
    )

    markup.row(
        InlineKeyboardButton(
            text=(
                "🇮🇷 فارسی ✅"
                if user.get("lang") == "fa"
                else "🇮🇷 فارسی"
            ),
            callback_data="setlang_fa",
        ),
        InlineKeyboardButton(
            text=(
                "🇺🇸 English ✅"
                if user.get("lang") == "en"
                else "🇺🇸 English"
            ),
            callback_data="setlang_en",
        ),
    )

    markup.add(
        InlineKeyboardButton(
            text=t["settings_back"],
            callback_data="settings_home",
        )
    )

    return markup


def _current_settings_text(
    user: dict,
    t: dict,
) -> str:

    language_name = (
        "فارسی"
        if user.get("lang") == "fa"
        else "English"
    )

    quality_labels = {
        "best": t["instagram_quality_best"],
        "1080p": t["instagram_quality_1080"],
        "720p": t["instagram_quality_720"],
        "480p": t["instagram_quality_480"],
        "360p": t["instagram_quality_360"],
    }

    quality_name = quality_labels.get(
        user.get("instagram_quality"),
        t["instagram_quality_best"],
    )

    low_data_name = (
        t["low_data_on"]
        if user.get("low_data_mode", False)
        else t["low_data_off"]
    )

    return (
        f"{t['current_settings_title']}\n\n"
        f"• {t['current_instagram_quality']}: "
        f"{quality_name}\n"
        f"• {t['current_low_data']}: "
        f"{low_data_name}\n"
        f"• {t['current_language']}: "
        f"{language_name}"
    )


def _reset_markup(
    t: dict,
) -> InlineKeyboardMarkup:

    markup = InlineKeyboardMarkup(
        row_width=2
    )

    markup.row(
        InlineKeyboardButton(
            text=t["reset_confirm"],
            callback_data="settings_reset_confirm",
        ),
        InlineKeyboardButton(
            text=t["reset_cancel"],
            callback_data="settings_home",
        ),
    )

    return markup


def register_features(bot):
    flags = store.load_flags()

    def _main_reply_markup():
        current_flags = store.load_flags()

        markup = ReplyKeyboardMarkup(
            row_width=1,
            resize_keyboard=True,
        )

        # Always visible: the easiest way for users to reach the admin.
        markup.add(
            KeyboardButton(
                FEEDBACK_BUTTON_TEXT
            )
        )

        if current_flags.get(
            "ad_requests_button",
            False,
        ):
            markup.add(
                KeyboardButton(
                    AD_BUTTON_TEXT
                )
            )

        return markup

    def _ad_texts(lang="fa"):
        if lang == "en":
            return {
                "channel_prompt": "📣 Send the ID, username, or link of the channel you want to advertise:",
                "display_name_prompt": "🏷 Enter the display name you want for the advertisement:",
                "type_prompt": (
                    "📌 Choose the type of advertisement you want:\n\n"
                    "📢 Sponsor Channel\n"
                    "Your channel will be added as a sponsor channel. "
                    "Users will need to join your channel before they can use the bot's download features.\n\n"
                    "📣 After Every Downloaded Post\n"
                    "Your channel advertisement will be shown to users after each successful download."
                ),
                "type_sponsor_channel": "📢 Sponsor Channel",
                "type_post_download": "📣 After Every Downloaded Post",
                "duration_prompt": "⏱ Enter the desired duration or number of displays:",
                "notes_prompt": "📝 Send any additional notes or requirements.\nIf you have none, send \"none\".",
                "cancel": "❌ Cancel",
                "switch_to_english": "🇺🇸 English",
                "switch_to_persian": "🇮🇷 فارسی",
                "summary_title": "📋 Advertisement Request Summary",
                "summary_channel": "📣 Channel",
                "summary_display_name": "🏷 Display Name",
                "summary_type": "📌 Advertisement Type",
                "summary_duration": "⏱ Duration / Displays",
                "summary_notes": "📝 Notes",
                "confirm_prompt": "Is the information correct?",
                "submit": "✅ Submit Request",
                "edit": "✏️ Edit",
                "submitted": "✅ Your advertising request has been submitted.\n\nThe admin will review it and contact you if it is approved.",
                "cancelled": "❌ Advertising request cancelled.",
                "existing_pending": "⏳ You already have an advertising request awaiting review.\n\nPlease wait for the previous request to be reviewed.",
                "unavailable": "⚠️ Advertising requests are currently disabled.",
                "invalid_type": "Invalid advertisement type.",
            }

        return {
            "channel_prompt": "📣 لطفاً آیدی، یوزرنیم یا لینک کانالی که می‌خواهید تبلیغ کنید را ارسال کنید:",
            "display_name_prompt": "🏷 نام نمایشی موردنظرتان برای تبلیغ را وارد کنید:",
            "type_prompt": (
                "📌 نوع تبلیغ موردنظر خود را انتخاب کنید:\n\n"
                "📢 کانال اسپانسر\n"
                "کانال شما به‌عنوان کانال اسپانسر ربات ثبت می‌شود "
                "و کاربران برای استفاده از قابلیت دانلود باید ابتدا عضو کانال شما شوند.\n\n"
                "📣 نمایش بعد از هر دانلود\n"
                "تبلیغ کانال شما پس از هر دانلود موفق به کاربران نمایش داده می‌شود."
            ),
            "type_sponsor_channel": "📢 کانال اسپانسر",
            "type_post_download": "📣 نمایش بعد از هر دانلود",
            "duration_prompt": "⏱ مدت تبلیغ یا تعداد نمایش موردنظر را وارد کنید:",
            "notes_prompt": "📝 اگر توضیح یا درخواست دیگری دارید بنویسید.\nاگر ندارید، «ندارم» را ارسال کنید.",
            "cancel": "❌ لغو",
            "switch_to_english": "🇺🇸 English",
            "switch_to_persian": "🇮🇷 فارسی",
            "summary_title": "📋 خلاصه درخواست تبلیغات",
            "summary_channel": "📣 کانال",
            "summary_display_name": "🏷 نام نمایشی",
            "summary_type": "📌 نوع تبلیغ",
            "summary_duration": "⏱ مدت / تعداد نمایش",
            "summary_notes": "📝 توضیحات",
            "confirm_prompt": "آیا اطلاعات درخواست صحیح است؟",
            "submit": "✅ ارسال درخواست",
            "edit": "✏️ ویرایش",
            "submitted": "✅ درخواست تبلیغات شما با موفقیت ثبت شد.\n\nمدیر درخواست شما را بررسی می‌کند و در صورت تأیید با شما هماهنگ خواهد شد.",
            "cancelled": "❌ درخواست تبلیغات لغو شد.",
            "existing_pending": "⏳ شما یک درخواست تبلیغات در حال بررسی دارید.\n\nلطفاً تا بررسی درخواست قبلی منتظر بمانید.",
            "unavailable": "⚠️ ثبت درخواست تبلیغات در حال حاضر غیرفعال است.",
            "invalid_type": "نوع تبلیغ نامعتبر است.",
        }

    def _ad_lang_for_request(request):
        return request.get(
            "ad_form_lang",
            "fa",
        )

    def _ad_cancel_markup(request):
        ad_t = _ad_texts(
            _ad_lang_for_request(request)
        )

        markup = InlineKeyboardMarkup(
            row_width=1
        )

        if _ad_lang_for_request(request) == "fa":
            markup.add(
                InlineKeyboardButton(
                    ad_t["switch_to_english"],
                    callback_data="ad_lang_en",
                )
            )
        else:
            markup.add(
                InlineKeyboardButton(
                    ad_t["switch_to_persian"],
                    callback_data="ad_lang_fa",
                )
            )

        markup.add(
            InlineKeyboardButton(
                ad_t["cancel"],
                callback_data="ad_cancel",
            )
        )

        return markup

    def _ad_type_markup(request):
        ad_t = _ad_texts(
            _ad_lang_for_request(request)
        )

        markup = InlineKeyboardMarkup(
            row_width=1
        )

        markup.add(
            InlineKeyboardButton(
                ad_t["type_sponsor_channel"],
                callback_data="ad_type_sponsor_channel",
            )
        )

        markup.add(
            InlineKeyboardButton(
                ad_t["type_post_download"],
                callback_data="ad_type_post_download",
            )
        )

        if _ad_lang_for_request(request) == "fa":
            markup.add(
                InlineKeyboardButton(
                    ad_t["switch_to_english"],
                    callback_data="ad_lang_en",
                )
            )
        else:
            markup.add(
                InlineKeyboardButton(
                    ad_t["switch_to_persian"],
                    callback_data="ad_lang_fa",
                )
            )

        markup.add(
            InlineKeyboardButton(
                ad_t["cancel"],
                callback_data="ad_cancel",
            )
        )

        return markup

    def _format_ad_summary(
        request,
    ):
        ad_t = _ad_texts(
            _ad_lang_for_request(request)
        )

        type_labels = {
            "sponsor_channel": ad_t["type_sponsor_channel"],
            "post_download": ad_t["type_post_download"],
        }

        return (
            f"{ad_t['summary_title']}\n\n"
            f"{ad_t['summary_channel']}: "
            f"{request.get('channel', '')}\n"
            f"{ad_t['summary_display_name']}: "
            f"{request.get('display_name', '')}\n"
            f"{ad_t['summary_type']}: "
            f"{type_labels.get(request.get('ad_type'), request.get('ad_type', ''))}\n"
            f"{ad_t['summary_duration']}: "
            f"{request.get('duration', '')}\n"
            f"{ad_t['summary_notes']}: "
            f"{request.get('notes', '')}\n\n"
            f"{ad_t['confirm_prompt']}"
        )

    def _ad_confirmation_markup(request):
        ad_t = _ad_texts(
            _ad_lang_for_request(request)
        )

        markup = InlineKeyboardMarkup(
            row_width=2
        )

        markup.add(
            InlineKeyboardButton(
                ad_t["submit"],
                callback_data="ad_submit",
            ),
            InlineKeyboardButton(
                ad_t["edit"],
                callback_data="ad_edit",
            ),
        )

        if _ad_lang_for_request(request) == "fa":
            markup.add(
                InlineKeyboardButton(
                    ad_t["switch_to_english"],
                    callback_data="ad_lang_en",
                )
            )
        else:
            markup.add(
                InlineKeyboardButton(
                    ad_t["switch_to_persian"],
                    callback_data="ad_lang_fa",
                )
            )

        markup.add(
            InlineKeyboardButton(
                ad_t["cancel"],
                callback_data="ad_cancel",
            )
        )

        return markup

    def _send_ad_prompt(
        chat_id_int,
        request,
    ):
        step = request.get(
            "step"
        )

        ad_t = _ad_texts(
            _ad_lang_for_request(request)
        )

        if step == "channel":
            bot.send_message(
                chat_id_int,
                ad_t["channel_prompt"],
                reply_markup=_ad_cancel_markup(request),
            )

        elif step == "display_name":
            bot.send_message(
                chat_id_int,
                ad_t["display_name_prompt"],
                reply_markup=_ad_cancel_markup(request),
            )

        elif step == "type":
            bot.send_message(
                chat_id_int,
                ad_t["type_prompt"],
                reply_markup=_ad_type_markup(request),
            )

        elif step == "duration":
            bot.send_message(
                chat_id_int,
                ad_t["duration_prompt"],
                reply_markup=_ad_cancel_markup(request),
            )

        elif step == "notes":
            bot.send_message(
                chat_id_int,
                ad_t["notes_prompt"],
                reply_markup=_ad_cancel_markup(request),
            )

    def _my_settings_view(chat_id):
        user = store.get_user(user_settings, chat_id)
        t = TEXTS[user['lang']]
        return t['settings_msg'], _settings_markup(user, t)

    admin.register_admin(bot, flags, _texts_for, _my_settings_view)

    def _alert_owner_auth_problem(platform_name, detail):
        owner_id = int(os.environ.get("OWNER_ID", "0") or "0")

        if not owner_id:
            return

        name = platforms.PLATFORM_NAMES.get(platform_name, platform_name)

        try:
            bot.send_message(
                owner_id,
                (
                    f"⚠️ کوکی‌های ورود {name} ربات منقضی یا نامعتبر به نظر می‌رسن. "
                    "تا وقتی تازه نشن، استوری‌ها و محتوای خصوصی/محدود دانلود نمی‌شن.\n\n"
                    f"⚠️ The bot's {name} login cookies look expired or invalid. "
                    "Stories and private/restricted content will keep failing until they are refreshed.\n\n"
                    f"{str(detail)[:500]}"
                ),
            )
        except Exception:
            logger.exception("Could not send the auth alert to the owner")

    platforms.set_auth_alert_handler(_alert_owner_auth_problem)

    def _alert_owner_sponsor_check_failed(channel_username, error):
        owner_id = int(os.environ.get("OWNER_ID", "0") or "0")

        if not owner_id:
            return

        try:
            bot.send_message(
                owner_id,
                (
                    f"⚠️ عضویت کاربران در کانال اسپانسر {channel_username} قابل بررسی نیست "
                    "و فعلاً کاربران بدون عضویت رد می‌شن. ربات رو ادمین کانال کن "
                    f"و با /checksponsor {channel_username} تستش کن.\n\n"
                    f"⚠️ Membership in sponsor channel {channel_username} can't be checked, "
                    "so users currently pass without joining. Make the bot an admin there "
                    f"and test with /checksponsor {channel_username}.\n\n"
                    f"{str(error)[:300]}"
                ),
            )
        except Exception:
            logger.exception("Could not send the sponsor-check alert to the owner")

    ads.set_check_failure_handler(_alert_owner_sponsor_check_failed)

    # ------------------------------------------------------------------
    # Small helpers shared by group and inline mode
    # ------------------------------------------------------------------

    def _bot_me():
        if "me" not in _bot_identity:
            _bot_identity["me"] = bot.get_me()
        return _bot_identity["me"]

    def _bot_username():
        return _bot_me().username

    def _cache_chat_id():
        value = os.environ.get("CACHE_CHAT_ID") or flags.get("cache_chat_id")
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    def _raw_api(method_name, params):
        """Bot API methods newer than pyTelegramBotAPI (ephemeral messages)."""
        return apihelper._make_request(bot.token, method_name, method="post", params=params)

    def _set_reaction(chat_id, message_id, emoji):
        """Quiet progress feedback in groups: 👀 while working, removed when
        the file arrives, 🤷 if it failed. Groups that disabled reactions
        simply get nothing."""
        if not message_id:
            return
        try:
            bot.set_message_reaction(
                chat_id,
                message_id,
                [ReactionTypeEmoji(emoji)] if emoji else [],
            )
        except Exception:
            pass

    def _bot_is_admin(chat_id) -> bool:
        cached = _bot_admin_cache.get(chat_id)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        try:
            status = bot.get_chat_member(chat_id, _bot_me().id).status
            is_admin = status in ("administrator", "creator")
        except Exception:
            is_admin = False
        _bot_admin_cache[chat_id] = (time.monotonic(), is_admin)
        return is_admin

    def _inline_try_markup(t):
        markup = InlineKeyboardMarkup()
        markup.add(_SwitchInlineButton(t["inline_try_btn"], switch_inline_query=""))
        return markup

    def _open_bot_markup(t, payload="inline", text_key="open_bot_btn"):
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(
                t[text_key],
                url=f"https://t.me/{_bot_username()}?start={payload}",
            )
        )
        return markup

    # ------------------------------------------------------------------
    # /setcache: connect the private "DuckLoader Cache" channel
    # ------------------------------------------------------------------
    # Inline messages can only be turned into media that's already on
    # Telegram, so inline downloads are uploaded to this private channel
    # first. The owner forwards a message from it — only the owner can do
    # this, so nobody else can point the bot's uploads at their channel.

    _awaiting_cache_forward = set()

    def _forwarded_channel(message):
        origin = getattr(message, "forward_origin", None)
        if origin is not None and getattr(origin, "type", "") == "channel":
            return getattr(origin, "chat", None)
        chat = getattr(message, "forward_from_chat", None)
        if chat is not None and getattr(chat, "type", "") == "channel":
            return chat
        return None

    @bot.message_handler(commands=["setcache"])
    def set_cache_channel(message):
        if not admin.is_owner(message.from_user.id) or not _is_private_chat(message.chat.type):
            return
        _awaiting_cache_forward.add(message.from_user.id)
        bot.reply_to(message, _texts_for(message.chat.id)["cache_ask_forward"])

    @bot.message_handler(
        func=lambda msg:
            msg.from_user is not None
            and msg.from_user.id in _awaiting_cache_forward
            and _forwarded_channel(msg) is not None,
        content_types=["text", "photo", "video", "audio", "document", "animation", "voice", "sticker"],
    )
    def receive_cache_channel(message):
        _awaiting_cache_forward.discard(message.from_user.id)
        t = _texts_for(message.chat.id)
        channel = _forwarded_channel(message)

        try:
            bot.send_message(channel.id, "🦆 DuckLoader cache channel connected.", disable_notification=True)
        except Exception as e:
            bot.reply_to(message, t["cache_set_failed"].format(error=str(e)[:300]))
            return

        flags["cache_chat_id"] = channel.id
        store.save_flags(flags)
        bot.reply_to(message, t["cache_set_done"].format(title=getattr(channel, "title", channel.id)))

    # ------------------------------------------------------------------
    # Feedback: suggestions and problem reports, forwarded to the owner
    # ------------------------------------------------------------------
    # Registered before handle_media_link and the ad-request form, so a
    # user who is writing feedback can paste the link that failed.

    def _owner_id():
        return int(os.environ.get("OWNER_ID", "0") or "0")

    def _feedback_kind_markup(t):
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton(t["feedback_idea_btn"], callback_data="fb_kind_idea"),
            InlineKeyboardButton(t["feedback_bug_btn"], callback_data="fb_kind_bug"),
            InlineKeyboardButton(t["feedback_cancel_btn"], callback_data="fb_cancel"),
        )
        return markup

    def _feedback_waiting_kind(user_id):
        with _feedback_lock:
            state = _feedback_waiting.get(user_id)
            if state and time.monotonic() - state["since"] > FEEDBACK_WAIT_SECONDS:
                _feedback_waiting.pop(user_id, None)
                return None
            return state["kind"] if state else None

    def _feedback_rate_limited(user_id) -> bool:
        now = time.time()
        with _feedback_lock:
            recent = [ts for ts in _feedback_recent[user_id] if now - ts < 3600]
            _feedback_recent[user_id] = recent
            return len(recent) >= FEEDBACK_LIMIT_PER_HOUR

    @bot.message_handler(commands=["feedback"])
    @bot.message_handler(
        func=lambda msg:
            bool(msg.text)
            and msg.text.strip() in FEEDBACK_BUTTON_TEXTS
    )
    def start_feedback(message):
        t = _texts_for(message.chat.id)

        if store.is_banned(message.from_user.id):
            return

        if not _is_private_chat(message.chat.type):
            bot.reply_to(message, t["feedback_private_only"])
            return

        with _feedback_lock:
            _feedback_waiting.pop(message.from_user.id, None)

        bot.reply_to(
            message,
            t["feedback_choose"],
            reply_markup=_feedback_kind_markup(t),
        )

    @bot.callback_query_handler(
        func=lambda call: call.data in {"fb_kind_idea", "fb_kind_bug", "fb_cancel"}
    )
    def handle_feedback_kind(call):
        t = _texts_for(call.message.chat.id)
        user_id = call.from_user.id

        bot.answer_callback_query(call.id)

        try:
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass

        if call.data == "fb_cancel":
            with _feedback_lock:
                _feedback_waiting.pop(user_id, None)
            bot.send_message(call.message.chat.id, t["feedback_cancelled"])
            return

        if _feedback_rate_limited(user_id):
            bot.send_message(call.message.chat.id, t["feedback_too_many"])
            return

        kind = "idea" if call.data == "fb_kind_idea" else "bug"

        with _feedback_lock:
            _feedback_waiting[user_id] = {
                "kind": kind,
                "since": time.monotonic(),
            }

        bot.send_message(
            call.message.chat.id,
            t["feedback_prompt_idea" if kind == "idea" else "feedback_prompt_bug"],
        )

    def _is_feedback_message(msg):
        if msg.from_user is None or not _is_private_chat(msg.chat.type):
            return False

        if not _feedback_waiting_kind(msg.from_user.id):
            return False

        if (msg.text or "").startswith("/"):
            # Any command ends feedback mode; /cancel is answered below,
            # everything else is handled by its own handler as usual.
            if (msg.text or "").split()[0].split("@")[0] != "/cancel":
                with _feedback_lock:
                    _feedback_waiting.pop(msg.from_user.id, None)
                return False

        return True

    @bot.message_handler(
        func=_is_feedback_message,
        content_types=[
            "text", "photo", "video", "voice", "audio",
            "document", "animation", "video_note", "sticker",
        ],
    )
    def receive_feedback(message):
        user = message.from_user
        t = _texts_for(message.chat.id)

        with _feedback_lock:
            state = _feedback_waiting.pop(user.id, None)

        if (message.text or "").startswith("/cancel"):
            bot.reply_to(message, t["feedback_cancelled"], reply_markup=_main_reply_markup())
            return

        if message.content_type == "sticker":
            with _feedback_lock:
                _feedback_waiting[user.id] = state
            bot.reply_to(message, t["feedback_unsupported"])
            return

        kind = (state or {}).get("kind", "idea")
        owner_id = _owner_id()

        if not owner_id:
            bot.reply_to(message, t["feedback_failed"])
            return

        user_record = store.get_user(user_settings, message.chat.id)
        content = (message.text or message.caption or "").strip()

        feedback_id = store.add_feedback(
            user_id=user.id,
            username=user.username or "",
            name=" ".join(p for p in (user.first_name, user.last_name) if p),
            kind=kind,
            text=content,
            content_type=message.content_type,
        )

        owner_t = _texts_for(owner_id)
        header_lines = [
            f"{owner_t['fb_admin_idea'] if kind == 'idea' else owner_t['fb_admin_bug']}  #FB{feedback_id}",
            "",
            f"👤 {' '.join(p for p in (user.first_name, user.last_name) if p) or '—'}"
            + (f" (@{user.username})" if user.username else ""),
            f"🆔 {user.id}",
            f"🌐 {user_record.get('lang', '—')}"
            + (f" | 🔗 {user_record['source']}" if user_record.get("source") else ""),
            f"🕐 {time.strftime('%Y-%m-%d %H:%M')}",
        ]

        if kind == "bug":
            # The most useful context for a bug report: what actually
            # failed for this user recently, straight from the error log.
            recent_errors = [
                event
                for event in store.load_error_log()
                if event.get("user_id") in (user.id, message.chat.id)
            ][-3:]

            if recent_errors:
                header_lines += ["", owner_t["fb_admin_recent_errors"]]

                for event in reversed(recent_errors):
                    header_lines.append(
                        f"• {event.get('time', '')} | {event.get('platform', '')}\n"
                        f"  {event.get('url', '')}\n"
                        f"  {str(event.get('error', ''))[:200]}"
                    )

        if content and message.content_type == "text":
            header_lines += ["", f"💬 {content}"]

        reply_markup = InlineKeyboardMarkup()
        reply_markup.add(
            InlineKeyboardButton(
                owner_t["fb_admin_reply_btn"],
                callback_data=f"fbreply_{user.id}_{feedback_id}",
            )
        )

        try:
            bot.send_message(
                owner_id,
                "\n".join(header_lines)[:4000],
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )

            if message.content_type != "text":
                # Photos, screenshots, voice notes... arrive as they were sent.
                bot.copy_message(owner_id, message.chat.id, message.message_id)
        except Exception:
            logger.exception("Could not forward feedback #%s to the owner", feedback_id)
            bot.reply_to(message, t["feedback_failed"])
            return

        with _feedback_lock:
            _feedback_recent[user.id].append(time.time())

        bot.reply_to(message, t["feedback_thanks"], reply_markup=_main_reply_markup())

    @bot.callback_query_handler(func=lambda call: call.data.startswith("fbreply_"))
    def handle_feedback_reply_button(call):
        if not admin.is_owner(call.from_user.id):
            bot.answer_callback_query(call.id)
            return

        try:
            _, target_id, feedback_id = call.data.split("_", 2)
            target_id = int(target_id)
        except ValueError:
            bot.answer_callback_query(call.id, "Invalid.", show_alert=True)
            return

        _owner_reply_target[call.from_user.id] = (target_id, feedback_id)
        bot.answer_callback_query(call.id)

        bot.send_message(
            call.message.chat.id,
            _texts_for(call.message.chat.id)["fb_admin_reply_prompt"].format(id=target_id),
            reply_markup=ForceReply(selective=True),
        )

    @bot.message_handler(
        func=lambda msg:
            msg.from_user is not None
            and msg.from_user.id in _owner_reply_target,
        content_types=["text", "photo", "video", "voice", "audio", "document", "animation"],
    )
    def send_owner_feedback_reply(message):
        target_id, feedback_id = _owner_reply_target.pop(message.from_user.id)
        owner_t = _texts_for(message.chat.id)

        if (message.text or "").startswith("/"):
            bot.reply_to(message, owner_t["adm_cancelled"])
            return

        target_t = _texts_for(target_id)

        try:
            bot.send_message(target_id, target_t["feedback_reply_prefix"])
            bot.copy_message(target_id, message.chat.id, message.message_id)
            store.mark_feedback_replied(feedback_id)
            bot.reply_to(message, owner_t["fb_admin_reply_sent"])
        except Exception as e:
            bot.reply_to(message, owner_t["fb_admin_reply_failed"].format(error=str(e)[:300]))

    # Registered before handle_media_link: the command text contains links,
    # and handlers are matched in registration order.
    @bot.message_handler(commands=["senddl"])
    def owner_send_download(message):
        """/senddl <user_id> <link> [<link> ...] — owner only.

        Downloads each link and delivers it to that user exactly as a normal
        download (caption, buttons, duck reactions, post-download ad), e.g.
        to make up for requests that failed because of a bot bug."""
        if not admin.is_owner(message.from_user.id):
            return

        parts = (message.text or "").split()

        if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
            bot.reply_to(
                message,
                "Usage: /senddl <user_id> <link> [<link> ...]",
            )
            return

        target_id = int(parts[1])
        urls = [
            url
            for url in (platforms.extract_url(part) for part in parts[2:])
            if url
        ]

        if not urls:
            bot.reply_to(message, "❌ No supported link found.")
            return

        target_user = store.get_user(user_settings, target_id)
        target_t = TEXTS.get(target_user.get("lang"), TEXTS[store.DEFAULT_LANGUAGE])
        results = []

        try:
            bot.send_message(target_id, target_t["senddl_notice"])
        except Exception as e:
            # Usually "bot was blocked by the user" — nothing will reach them.
            bot.reply_to(message, f"❌ Could not message {target_id}: {str(e)[:300]}")
            return

        for url in urls:
            platform = platforms.detect_platform(url)

            if platform == "spotify":
                results.append(f"⚠️ {url}\nSpotify links aren't supported by /senddl.")
                continue

            if platform == "instagram":
                quality = (
                    "480p"
                    if target_user.get("low_data_mode", False)
                    else target_user.get("instagram_quality", "best")
                )
            else:
                quality = target_user.get("quality", "best")

            ok, error = _run_direct_download(
                target_id,
                None,
                url,
                quality,
                target_t,
                None,
                show_ui=True,
            )

            results.append(
                f"✅ {url}"
                if ok
                else f"❌ {url}\n{str(error)[:300]}"
            )

        bot.reply_to(
            message,
            f"/senddl → {target_id}\n\n" + "\n\n".join(results),
            disable_web_page_preview=True,
        )

    def _delete_duck_message(
        message_map,
        chat_id_int,
    ):
        with _duck_message_lock:
            message_id = message_map.pop(
                chat_id_int,
                None,
            )

        if message_id is None:
            return

        try:
            bot.delete_message(
                chat_id_int,
                message_id,
            )
        except Exception:
            pass

    def _send_duck_reaction(
        chat_id_int,
        event,
        track_status=False,
        track_complete=False,
        show_ui=True,
    ):
        if not show_ui:
            return None

        reactions = (
            store.load_duck_reactions()
        )

        reaction_list = reactions.get(
            event,
            [],
        )

        if not reaction_list:
            return None

        reaction = random.choice(
            reaction_list
        )

        media_type = reaction.get(
            "type"
        )

        file_id = reaction.get(
            "file_id"
        )

        if not file_id:
            return None

        try:
            if media_type == "sticker":
                sent = bot.send_sticker(
                    chat_id_int,
                    file_id,
                )

            elif media_type == "animation":
                sent = bot.send_animation(
                    chat_id_int,
                    file_id,
                )

            else:
                logger.warning(
                    "Unknown duck reaction type: %s",
                    media_type,
                )
                return None

        except Exception:
            logger.exception(
                "Failed to send duck reaction | event=%s",
                event,
            )
            return None

        message_id = (
            sent.message_id
        )

        with _duck_message_lock:
            if track_status:
                _duck_status_messages[
                    chat_id_int
                ] = message_id

            if track_complete:
                _duck_complete_messages[
                    chat_id_int
                ] = message_id

        return message_id

    def _delete_previous_download_ducks(
        chat_id_int,
    ):
        _delete_duck_message(
            _duck_status_messages,
            chat_id_int,
        )

        _delete_duck_message(
            _duck_complete_messages,
            chat_id_int,
        )


    def _finish_duck_download(
        chat_id_int,
    ):
        _delete_duck_message(
            _duck_status_messages,
            chat_id_int,
        )


    def _send_duck_download_failed(
        chat_id_int,
        show_ui=True,
    ):
        if not show_ui:
            return

        _finish_duck_download(
            chat_id_int,
        )

        _send_duck_reaction(
            chat_id_int,
            "failed",
            show_ui=True,
        )

    def _send_duck_download_complete(
        chat_id_int,
        show_ui=True,
    ):
        if not show_ui:
            return

        _finish_duck_download(
            chat_id_int,
        )

        _delete_duck_message(
            _duck_complete_messages,
            chat_id_int,
        )

        _send_duck_reaction(
            chat_id_int,
            "complete",
            track_complete=True,
            show_ui=True,
        )

    def _maybe_send_ad(chat_id_int):
        if flags.get(
            "sponsor_message",
            False,
        ):
            ad_text = (
                ads.load_ad_message()
            )

            if ad_text:
                try:
                    bot.send_message(
                        chat_id_int,
                        ad_text,
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

        _send_campaign_ad(chat_id_int)

    AD_CLICK_PREFIX = "adk_"

    def _send_campaign_ad(chat_id_int):
        """At most one paid post-download ad, chosen by campaigns.py's
        frequency rules. The button is a callback first so the click can
        be counted; it then turns into the real channel link."""
        request, just_completed = campaigns.choose_and_record(
            chat_id_int,
            store.list_ad_requests(campaigns.ACTIVE),
        )
        if request is None:
            return

        name = campaigns.display_name(request)
        try:
            if campaigns.channel_url(request):
                markup = InlineKeyboardMarkup()
                markup.add(
                    InlineKeyboardButton(
                        f"📢 {name}",
                        callback_data=AD_CLICK_PREFIX + request["request_id"],
                    )
                )
                bot.send_message(chat_id_int, "📣 تبلیغ", reply_markup=markup)
            else:
                bot.send_message(chat_id_int, f"📣 {name}\n{request.get('channel', '')}")
        except Exception:
            campaigns.undo_impression(request["request_id"], chat_id_int)
            return

        if just_completed:
            _finish_campaign(request)

    def _finish_campaign(request):
        updated = store.update_ad_request(
            request["request_id"],
            status=campaigns.COMPLETED,
            completed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        ) or request
        owner_id = _owner_id()
        if not owner_id:
            return
        try:
            bot.send_message(
                owner_id,
                "✅ تبلیغ به سقف نمایشش رسید و متوقف شد.\n\n"
                + campaigns.report_text(updated),
                reply_markup=admin.campaign_report_markup(updated, TEXTS["fa"]),
            )
        except Exception:
            logger.exception("Could not send the campaign report to the owner")

    @bot.callback_query_handler(func=lambda call: call.data.startswith(AD_CLICK_PREFIX))
    def handle_ad_click(call):
        request_id = call.data[len(AD_CLICK_PREFIX):]
        request = store.get_ad_request(request_id)
        url = campaigns.channel_url(request) if request else ""
        if not url:
            bot.answer_callback_query(call.id)
            return

        campaigns.record_click(request_id, call.from_user.id)

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(f"↗️ ورود به {campaigns.display_name(request)}", url=url))
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "👇 روی دکمه بزن تا وارد کانال بشی")

    def _check_sponsor_channel_gate(
        chat_id_int,
        user_id,
        t,
    ):
        current_flags = store.load_flags()

        if not current_flags.get(
            "sponsor_channel_gate",
            True,
        ):
            return True

        if store.is_exempt(
            user_id
        ):
            return True

        unjoined = (
            ads.get_unjoined_channels(
                bot,
                user_id,
            )
        )

        if not unjoined:
            _inline_gate_cache[user_id] = (time.monotonic(), True)
            return True

        markup = InlineKeyboardMarkup(
            row_width=1
        )

        for channel in unjoined:
            username = (
                channel.get(
                    "username",
                    "",
                )
                or ""
            ).strip()

            if not username:
                continue

            if username.startswith(
                "@"
            ):
                channel_url = (
                    "https://t.me/"
                    + username[1:]
                )
            else:
                channel_url = (
                    "https://t.me/"
                    + username
                )

            channel_name = (
                channel.get(
                    "name",
                    "",
                )
                or username
            )

            markup.add(
                InlineKeyboardButton(
                    text=t[
                        "sponsor_gate_join"
                    ].format(
                        name=channel_name
                    ),
                    url=channel_url,
                )
            )

        if not markup.keyboard:
            return True

        markup.add(
            InlineKeyboardButton(
                t[
                    "sponsor_gate_check"
                ],
                callback_data=(
                    "sponsor_check_"
                    + str(
                        user_id
                    )
                ),
            )
        )

        bot.send_message(
            chat_id_int,
            (
                t[
                    "sponsor_gate_title"
                ]
                + "\n\n"
                + t[
                    "sponsor_gate_retry"
                ]
            ),
            reply_markup=markup,
        )

        return False


    def _send_download_result(
        chat_id_int,
        reply_to_id,
        url,
        platform,
        quality_requested,
        quality_used,
        info,
        entries,
        files,
        t,
        show_ui=True,
        cache_key=None,
    ):
        """Builds the caption/buttons and sends the downloaded file(s) —
        one file directly, or a chunked media group for multi-item posts
        (Instagram carousels). Adds the "get audio" button when the result
        includes a video and the user didn't already request audio-only.
        Shared by the direct-download flow and the 'get audio' button."""

        try:
            _deliver_download_result(
                chat_id_int,
                reply_to_id,
                url,
                platform,
                quality_requested,
                quality_used,
                info,
                entries,
                files,
                t,
                show_ui=show_ui,
                cache_key=cache_key,
            )
        finally:
            platforms.remove_download_files(files)

    def _result_markup(t, url, post_id, has_thumb, offer_audio_button, show_ui, single_video):
        if show_ui:
            markup = InlineKeyboardMarkup()

            markup.add(
                InlineKeyboardButton(
                    text=t['view_link'],
                    url=url,
                )
            )

            if has_thumb:
                markup.add(
                    InlineKeyboardButton(
                        text=t['dl_cover'],
                        callback_data=f"thumb_{post_id}",
                    )
                )

            if offer_audio_button:
                markup.add(
                    InlineKeyboardButton(
                        text=t['get_audio_btn'],
                        callback_data=f"audio_{post_id}",
                    )
                )

            # Opens the chat picker with "@DuckDownloader_Bot <link>" already
            # typed: the file goes to a friend without leaving Telegram, and
            # nobody has to remember the bot's username.
            if url and len(url) <= 240:
                markup.add(
                    InlineKeyboardButton(
                        text=t['share_btn'],
                        switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
                            query=url,
                            allow_user_chats=True,
                            allow_bot_chats=False,
                            allow_group_chats=True,
                            allow_channel_chats=False,
                        ),
                    )
                )

            return markup

        if single_video:
            markup = InlineKeyboardMarkup()

            markup.add(
                InlineKeyboardButton(
                    text=t["get_caption"],
                    callback_data=f"caption_{post_id}",
                )
            )

            return markup

        return None

    def _send_cached_result(
        chat_id_int,
        reply_to_id,
        url,
        platform,
        cached,
        t,
        show_ui=True,
    ):
        """Re-sends a previous result by Telegram file_id — no download.
        Raises if Telegram refuses a file_id, so the caller can fall back
        to a fresh download."""
        post_id = cached["post_id"]

        if cached.get("thumb_url"):
            thumb_cache[post_id] = cached["thumb_url"]

        if cached.get("offer_audio"):
            audio_source_cache[post_id] = url

        items = cached["items"]
        caption = cached["caption"] if show_ui else BOT_SIGNATURE
        markup = _result_markup(
            t,
            url,
            post_id,
            bool(cached.get("thumb_url")),
            cached.get("offer_audio"),
            show_ui,
            len(items) == 1 and items[0][0] == "video",
        )

        if len(items) == 1:
            kind, file_id = items[0]
            common = {
                "caption": caption,
                "reply_markup": markup,
                "reply_to_message_id": reply_to_id,
            }

            if kind == "video":
                sent_message = bot.send_video(chat_id_int, file_id, supports_streaming=True, **common)

                if not show_ui:
                    caption_cache[post_id] = {
                        "chat_id": chat_id_int,
                        "message_id": sent_message.message_id,
                        "caption": cached["caption"],
                        "visible": False,
                    }
            elif kind == "audio":
                bot.send_audio(chat_id_int, file_id, **common)
            elif kind == "photo":
                bot.send_photo(chat_id_int, file_id, **common)
            elif kind == "animation":
                bot.send_animation(chat_id_int, file_id, **common)
            else:
                bot.send_document(chat_id_int, file_id, **common)
        else:
            input_types = {
                "video": InputMediaVideo,
                "photo": InputMediaPhoto,
                "audio": InputMediaAudio,
                "document": InputMediaDocument,
            }

            for chunk_idx in range(0, len(items), 10):
                media_group = [
                    input_types[kind](
                        file_id,
                        caption=caption if chunk_idx == 0 and item_idx == 0 else "",
                    )
                    for item_idx, (kind, file_id) in enumerate(items[chunk_idx:chunk_idx + 10])
                ]

                bot.send_media_group(
                    chat_id_int,
                    media_group,
                    reply_to_message_id=reply_to_id if chunk_idx == 0 else None,
                )

        if chat_id_int > 0:
            store.add_recent(chat_id_int, url, cached.get("title") or platform.title(), items)

        store.record_download(platform, chat_id_int)

    def _deliver_download_result(
        chat_id_int,
        reply_to_id,
        url,
        platform,
        quality_requested,
        quality_used,
        info,
        entries,
        files,
        t,
        show_ui=True,
        cache_key=None,
    ):
        # Only a real step down the quality ladder (auto_quality_fallback)
        # is worth a notice. This used to look up t['quality_best'], a key
        # that never existed — it crashed every Instagram "Get Audio" tap
        # after the file had already been downloaded.
        # SoundCloud is audio-only by nature (quality_used is always "audio"),
        # so it must never trigger the notice — and without the fallback
        # toggle there is no automatic reduction at all.
        if (
            show_ui
            and platform != "soundcloud"
            and flags.get("auto_quality_fallback", False)
            and quality_used != quality_requested
            and quality_requested in platforms.QUALITY_LADDER
            and quality_used in platforms.QUALITY_LADDER
            and platforms.QUALITY_LADDER.index(quality_used)
            > platforms.QUALITY_LADDER.index(quality_requested)
        ):
            bot.send_message(
                chat_id_int,
                t['quality_reduced'].format(
                    quality=_quality_label(t, quality_used)
                )
            )

        # Merge the parent result with the individual media entry.
        #
        # The parent result can contain the Instagram username ("channel")
        # even when the individual Reel/Story media entry doesn't.
        metadata_source = dict(
            info or {}
        )

        if entries:
            for key, value in (
                entries[0] or {}
            ).items():
                if value not in (
                    None,
                    "",
                    [],
                    {},
                ):
                    metadata_source[key] = value

        full_caption = _build_caption(
            metadata_source,
            url,
        )

        if show_ui:
            caption = full_caption
        else:
            caption = BOT_SIGNATURE

        thumb_url = (
            metadata_source.get("thumbnail")
        )
        post_id = str(
            metadata_source.get('id')
            or int(time.time() * 1000)
        )[:48]  # callback_data is limited to 64 bytes
        if thumb_url:
            thumb_cache[post_id] = thumb_url

        # entries[i] describes files[i] (e.g. each track of a SoundCloud set).
        file_metadata = {
            filepath: (entries[index] if index < len(entries or []) else None)
            for index, filepath in enumerate(files)
        }
        valid_files = [f for f in files if os.path.exists(f)]
        any_video = any(platforms.media_kind(f) == 'video' for f in valid_files)
        offer_audio_button = any_video and quality_requested != 'audio'
        if offer_audio_button:
            audio_source_cache[post_id] = url

        markup = _result_markup(
            t,
            url,
            post_id,
            bool(thumb_url),
            offer_audio_button,
            show_ui,
            len(valid_files) == 1 and platforms.media_kind(valid_files[0]) == "video",
        )

        sent_refs = []

        if len(valid_files) == 1:
            filepath = valid_files[0]
            kind = platforms.media_kind(filepath)

            if kind == "audio":
                platforms.tag_audio_file(
                        filepath,
                        title=(
                            metadata_source.get("track")
                            or metadata_source.get("title")
                            or ""
                        ),
                        artist=(
                            metadata_source.get("artist")
                            or metadata_source.get("uploader")
                            or metadata_source.get("channel")
                            or ""
                        ),
                        album=(
                            metadata_source.get("album")
                            or ""
                        ),
                        album_artist=(
                            metadata_source.get("album_artist")
                            or metadata_source.get("artist")
                            or metadata_source.get("uploader")
                            or metadata_source.get("channel")
                            or ""
                        ),
                        release_date=(
                            metadata_source.get("release_date")
                            or metadata_source.get("upload_date")
                            or ""
                        ),
                        track_number=metadata_source.get("track_number"),
                        total_tracks=metadata_source.get("track_count"),
                        disc_number=metadata_source.get("disc_number"),
                        total_discs=metadata_source.get("disc_count"),
                        cover_url=(
                            None
                            if platform == "youtube"
                            else thumb_url
                        ),
                     )
                
            if show_ui:
                bot.send_chat_action(
                    chat_id_int,
                    'upload_video'
                    if kind == 'video'
                    else 'upload_audio'
                    if kind == 'audio'
                    else 'upload_photo',
                )

            with open(filepath, "rb") as media_file:
                if kind == "video":
                    video_info = metadata_source or {}

                    video_width = video_info.get("width")
                    video_height = video_info.get("height")
                    video_duration = video_info.get("duration")

                    # yt-dlp can sometimes return floats for duration.
                    if video_duration is not None:
                        try:
                            video_duration = int(round(float(video_duration)))
                        except (TypeError, ValueError):
                            video_duration = None

                    if video_width is not None:
                        try:
                            video_width = int(video_width)
                        except (TypeError, ValueError):
                            video_width = None

                    if video_height is not None:
                        try:
                            video_height = int(video_height)
                        except (TypeError, ValueError):
                            video_height = None

                    sent_message = bot.send_video(
                        chat_id_int,
                        media_file,
                        caption=caption,
                        reply_markup=markup,
                        reply_to_message_id=reply_to_id,

                        # Tell Telegram that this is a normal streamable MPEG-4 video.
                        supports_streaming=True,

                        # Explicit video metadata.
                        width=video_width,
                        height=video_height,
                        duration=video_duration,

                        timeout=600,
                    )
                    sent_refs.append(_file_ref(sent_message))

                    if not show_ui:
                        caption_cache[post_id] = {
                            "chat_id": chat_id_int,
                            "message_id": sent_message.message_id,
                            "caption": full_caption,
                            "visible": False,
                        }
                elif kind == "audio":
                    audio_duration = metadata_source.get("duration") or 0

                    try:
                        audio_duration = int(float(audio_duration))
                    except (TypeError, ValueError):
                        audio_duration = 0

                    audio_title = (
                        metadata_source.get("track")
                        or metadata_source.get("title")
                        or ""
                    )

                    audio_performer = (
                        metadata_source.get("artist")
                        or metadata_source.get("uploader")
                        or metadata_source.get("channel")
                        or ""
                    )

                    # Telegram ignores a thumbnail given as a URL; it
                    # has to be uploaded as a small JPEG file.
                    thumb_path = platforms.make_thumbnail(thumb_url)

                    try:
                        thumb_file = (
                            open(thumb_path, "rb")
                            if thumb_path
                            else None
                        )

                        try:
                            sent_message = bot.send_audio(
                                chat_id_int,
                                media_file,

                                # Telegram audio metadata
                                duration=audio_duration,
                                title=audio_title,
                                performer=audio_performer,
                                thumbnail=thumb_file,

                                caption=caption,
                                reply_markup=markup,
                                reply_to_message_id=reply_to_id,
                                timeout=600,
                            )
                            sent_refs.append(_file_ref(sent_message))
                        finally:
                            if thumb_file:
                                thumb_file.close()
                    finally:
                        platforms.remove_download_files([thumb_path])
                else:
                    try:
                        sent_message = bot.send_photo(chat_id_int, media_file, caption=caption, reply_markup=markup, reply_to_message_id=reply_to_id)
                    except Exception:
                        # Very large or unusually shaped images are
                        # refused as photos but still work as files.
                        logger.warning("send_photo failed; sending as document | %s", filepath, exc_info=True)
                        media_file.seek(0)
                        sent_message = bot.send_document(chat_id_int, media_file, caption=caption, reply_markup=markup, reply_to_message_id=reply_to_id, timeout=600)

                    sent_refs.append(_file_ref(sent_message))

        elif len(valid_files) > 1:
            chunks = [valid_files[idx:idx + 10] for idx in range(0, len(valid_files), 10)]

            for chunk_idx, chunk in enumerate(chunks):
                media_group = []
                open_files = []

                try:
                    for item_idx, filepath in enumerate(chunk):
                        kind = platforms.media_kind(filepath)
                        entry_meta = file_metadata.get(filepath) or metadata_source

                        if kind == "audio":
                            platforms.tag_audio_file(
                                filepath,
                                title=entry_meta.get('track') or entry_meta.get('title') or '',
                                artist=entry_meta.get('artist') or entry_meta.get('uploader') or entry_meta.get('channel') or '',
                                cover_url=entry_meta.get('thumbnail'),
                            )

                        f = open(filepath, "rb")
                        open_files.append(f)

                        item_caption = caption if chunk_idx == 0 and item_idx == 0 else ""

                        if kind == "video":
                            media_group.append(InputMediaVideo(f, caption=item_caption, supports_streaming=True))
                        elif kind == "audio":
                            media_group.append(InputMediaAudio(f, caption=item_caption))
                        else:
                            media_group.append(InputMediaPhoto(f, caption=item_caption))

                    bot.send_chat_action(chat_id_int, 'upload_document')
                    sent_group = bot.send_media_group(chat_id_int, media_group, reply_to_message_id=reply_to_id if chunk_idx == 0 else None, timeout=600)
                    sent_refs.extend(_file_ref(sent) for sent in (sent_group or []))
                finally:
                    for f in open_files:
                        f.close()

        if (
            cache_key
            and valid_files
            and len(sent_refs) == len(valid_files)
            and all(sent_refs)
            and not (len(sent_refs) > 1 and any(kind == "animation" for kind, _ in sent_refs))
        ):
            media_cache[cache_key] = {
                "time": time.time(),
                "items": sent_refs,
                "caption": full_caption,
                "post_id": post_id,
                "thumb_url": thumb_url,
                "offer_audio": offer_audio_button,
                "title": _recent_title(metadata_source, platform),
            }

        if chat_id_int > 0 and sent_refs and all(sent_refs):
            store.add_recent(chat_id_int, cache_key or url, _recent_title(metadata_source, platform), sent_refs)

        store.record_download(platform, chat_id_int)

    def _run_direct_download(
        chat_id_int,
        reply_to_id,
        url,
        quality,
        t,
        status_msg,
        show_ui=True,
    ):
        """
        Shared direct-download worker.

        Technical errors are stored for the administrator and
        are never shown directly to the user.

        Returns (True, None) on success or (False, error).
        """

        platform = (
            platforms.detect_platform(url)
            or "unknown"
        )
        cache_key = platforms.media_cache_key(url, quality)

        # The same chat is already downloading this link (double tap, resend).
        in_flight_key = (chat_id_int, cache_key or url)

        if not _claim_in_flight(in_flight_key):
            if show_ui and status_msg is not None:
                try:
                    bot.edit_message_text(t["already_downloading"], chat_id_int, status_msg.message_id)
                except Exception:
                    pass
            return False, Exception("This link is already being downloaded for this chat.")

        group_lock = _group_lock(chat_id_int) if chat_id_int < 0 else contextlib.nullcontext()

        try:
            with group_lock:
                if not cache_key:
                    return _download_and_send(
                        chat_id_int, reply_to_id, url, quality, t, status_msg, show_ui, platform, None,
                    )

                with _cache_key_lock(cache_key):
                    # Already sent this exact media before? Re-send it by file_id.
                    cached = media_cache.get(cache_key)

                    if cached and time.time() - cached["time"] < MEDIA_CACHE_TTL:
                        try:
                            _send_cached_result(
                                chat_id_int,
                                reply_to_id,
                                url,
                                platform,
                                cached,
                                t,
                                show_ui=show_ui,
                            )

                            if show_ui and status_msg is not None:
                                try:
                                    bot.delete_message(chat_id_int, status_msg.message_id)
                                except Exception:
                                    pass

                            if show_ui:
                                _maybe_send_ad(chat_id_int)

                            logger.info("Served from media cache | %s", cache_key)
                            return True, None
                        except Exception:
                            logger.warning("Cached re-send failed; downloading again | %s", cache_key, exc_info=True)
                            media_cache.pop(cache_key, None)

                    return _download_and_send(
                        chat_id_int, reply_to_id, url, quality, t, status_msg, show_ui, platform, cache_key,
                    )
        finally:
            _release_in_flight(in_flight_key)

    def _download_and_send(
        chat_id_int,
        reply_to_id,
        url,
        quality,
        t,
        status_msg,
        show_ui,
        platform,
        cache_key,
    ):
        timing_key = f"{platform}:{'audio' if quality == 'audio' else 'media'}"

        slot = _acquire_download_slot(
            bot,
            chat_id_int,
            status_msg,
            t,
            show_ui=show_ui,
            platform=platform,
        )

        if slot is None:
            return False, Exception("The download queue is full.")

        # Groups get no status messages; a reaction on the link says "on it".
        react_target = reply_to_id if (chat_id_int < 0 and reply_to_id) else None
        _set_reaction(chat_id_int, react_target, "👀")

        # The clock starts once the job really starts (queue time excluded).
        ticker = _ProgressTicker(
            bot, chat_id_int, status_msg, t, show_ui,
            duration_model.expected(timing_key),
        ).start()

        _send_duck_reaction(
            chat_id_int,
            "downloading",
            track_status=True,
            show_ui=show_ui,
        )

        files = []

        try:

            allow_fallback = flags.get(
                "auto_quality_fallback",
                False,
            )

            (
                info,
                entries,
                files,
                quality_used,
            ) = platforms.download_direct(
                url,
                quality=quality,
                allow_fallback=allow_fallback,
                progress_hook=ticker.hook,
            )
            ticker.set_phase("uploading")
            delivered_bytes = _files_size(files)

            _send_download_result(
                chat_id_int,
                reply_to_id,
                url,
                platform,
                quality,
                quality_used,
                info,
                entries,
                files,
                t,
                show_ui=show_ui,
                cache_key=cache_key,
            )

            ticker.stop()
            duration_model.record(timing_key, ticker.elapsed(), delivered_bytes)

            _send_duck_download_complete(
                chat_id_int,
                show_ui=show_ui,
            )

            if show_ui and status_msg is not None:
                try:
                    bot.delete_message(
                        chat_id_int,
                        status_msg.message_id,
                    )
                except Exception:
                    pass
            _set_reaction(chat_id_int, react_target, None)

            if show_ui:
                _maybe_send_ad(chat_id_int)

            return True, None

        except platforms.FileTooLargeError as e:
            ticker.stop()

            if show_ui and status_msg is not None:
                _send_duck_download_failed(
                    chat_id_int,
                    show_ui=show_ui,
                )

            store.record_error(
                platform=platform,
                url=url,
                user_id=chat_id_int,
                error=str(e),
            )

            try:
                bot.edit_message_text(
                    t["too_large"].format(
                        size=str(e)
                    ),
                    chat_id_int,
                    status_msg.message_id,
                )
            except Exception:
                pass

            _set_reaction(chat_id_int, react_target, "🤷")

            return False, e

        except Exception as e:
            ticker.stop()

            if show_ui and status_msg is not None:
                _send_duck_download_failed(
                    chat_id_int,
                    show_ui=show_ui,
                )

            if isinstance(e, platforms.UnsupportedLinkError):
                # The user sent a profile/audio page etc. — not a bot error.
                logger.info(
                    "Unsupported link | platform=%s | kind=%s | url=%s",
                    platform,
                    e.kind,
                    url,
                )
            else:
                store.record_error(
                    platform=platform,
                    url=url,
                    user_id=chat_id_int,
                    error=str(e),
                )

                logger.exception(
                    "Download failed | platform=%s | url=%s",
                    platform,
                    url,
                )

            try:
                bot.edit_message_text(
                    _friendly_download_error(
                        platform,
                        e,
                        t,
                    ),
                    chat_id_int,
                    status_msg.message_id,
                )
            except Exception:
                pass

            _set_reaction(chat_id_int, react_target, "🤷")

            return False, e

        finally:
            ticker.stop()
            platforms.remove_download_files(files)

            slot.release()

    def _quality_option_label(opt, lang):
        digits = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

        if opt.get("size_bytes", 0) <= 0:
            size_label = "Unknown Size" if lang == "en" else "حجم نامشخص"
        else:
            size_label = f"{round(opt['size_bytes'] / 1024 / 1024)} MB"
            if lang == "fa":
                size_label = size_label.replace("MB", "مگابایت").translate(digits)

        if opt["kind"] == "audio":
            return f"🎵 Audio — {size_label}" if lang == "en" else f"🎵 صوت — {size_label}"

        height_str = str(opt.get("height"))
        if lang == "fa":
            height_str = height_str.translate(digits)
        return f"🎬 {height_str}p — {size_label}"

    def _youtube_quality_markup(probe, lang, requester_id):
        markup = InlineKeyboardMarkup(row_width=2)
        buttons = []

        for opt in probe["options"]:
            choice = "audio" if opt["kind"] == "audio" else str(opt.get("height"))
            buttons.append(
                InlineKeyboardButton(
                    text=_quality_option_label(opt, lang),
                    callback_data=f"{YT_PICK_PREFIX}{requester_id}_{probe['id']}_{choice}",
                )
            )

        markup.add(*buttons)
        return markup

    def _send_ephemeral_picker(chat_id, receiver_id, probe, caption, markup) -> bool:
        """Shows the picker only to the person who sent the link (Bot API
        10.2 ephemeral messages; possible at any time when the bot is a
        group admin). Returns False if it couldn't be sent ephemerally."""
        params = {
            "chat_id": chat_id,
            "caption" if probe.get("thumbnail") else "text": caption,
            "reply_markup": markup.to_json(),
            "ephemeral_message_parameters": json.dumps({"receiver_user_id": receiver_id}),
        }
        try:
            if probe.get("thumbnail"):
                params["photo"] = probe["thumbnail"]
                result = _raw_api("sendPhoto", params)
            else:
                result = _raw_api("sendMessage", params)
        except Exception as e:
            logger.info("Ephemeral picker not available: %s", str(e)[:200])
            return False

        if isinstance(result, dict) and result.get("ephemeral_message_id"):
            return True

        # An older Bot API server ignored the parameter and posted it
        # publicly: remove it and fall back to the normal picker.
        try:
            if isinstance(result, dict) and result.get("message_id"):
                bot.delete_message(chat_id, result["message_id"])
        except Exception:
            pass
        return False

    def _send_youtube_quality_picker(
        message,
        t,
        lang,
        show_ui=True,
        url=None,
    ):
        chat_id_int = message.chat.id
        requester_id = message.from_user.id

        url = url or platforms.extract_url(message.text or "")

        if not url:
            return

        status_msg = None

        if show_ui:
            status_msg = bot.reply_to(message, t["init"])
        else:
            _set_reaction(chat_id_int, message.message_id, "👀")

        try:
            probe = platforms.probe_youtube_qualities(url)
        except Exception as e:
            _send_duck_download_failed(chat_id_int, show_ui=show_ui)

            store.record_error(
                platform="youtube",
                url=url,
                user_id=requester_id,
                error=str(e),
            )
            logger.exception("YouTube quality probe failed | url=%s", url)

            if show_ui and status_msg is not None:
                try:
                    bot.edit_message_text(
                        _friendly_download_error("youtube", e, t),
                        chat_id_int,
                        status_msg.message_id,
                    )
                except Exception:
                    pass
            else:
                _set_reaction(chat_id_int, message.message_id, "🤷")
            return

        if not show_ui:
            _set_reaction(chat_id_int, message.message_id, None)

        if not probe.get("options"):
            if show_ui and status_msg is not None:
                bot.edit_message_text(t["yt_no_quality"], chat_id_int, status_msg.message_id)
            else:
                _set_reaction(chat_id_int, message.message_id, "🤷")
            return

        markup = _youtube_quality_markup(probe, lang, requester_id)

        if show_ui and status_msg is not None:
            try:
                bot.delete_message(chat_id_int, status_msg.message_id)
            except Exception:
                pass

        caption = t["yt_choose_quality"].format(title=(probe.get("title") or "")[:200])

        if not show_ui:
            _yt_pick_context[(chat_id_int, requester_id, probe["id"])] = {
                "link_message_id": message.message_id,
            }

            # Only the sender sees the picker when Telegram allows it...
            if _bot_is_admin(chat_id_int) and _send_ephemeral_picker(
                chat_id_int, requester_id, probe, caption, markup,
            ):
                return

            # ...otherwise everyone sees it, but only the sender can use it.
            name = message.from_user.first_name or (
                f"@{message.from_user.username}" if message.from_user.username else str(requester_id)
            )
            caption = f"{caption}\n\n{t['yt_pick_for'].format(name=name)}"

        sent_with_thumbnail = False

        if probe.get("thumbnail"):
            try:
                bot.send_photo(
                    chat_id_int,
                    probe["thumbnail"],
                    caption=caption,
                    reply_markup=markup,
                    reply_to_message_id=message.message_id,
                )
                sent_with_thumbnail = True
            except Exception:
                logger.warning(
                    "Could not send YouTube thumbnail; sending the picker as text | %s",
                    probe.get("thumbnail"),
                )

        if not sent_with_thumbnail:
            bot.send_message(
                chat_id_int,
                caption,
                reply_markup=markup,
                reply_to_message_id=message.message_id,
            )

    def _handle_start_payload(message, payload, t) -> bool:
        """Deep links: t.me/<bot>?start=gate | inline | yt_<id> | dl_<token>.
        Returns True if the payload was an action (not a campaign source)."""
        user_id = message.from_user.id

        if payload == "gate":
            if _check_sponsor_channel_gate(message.chat.id, user_id, t):
                bot.reply_to(message, t["inline_ready"].format(bot=_bot_username()), reply_markup=_inline_try_markup(t))
            return True

        if payload == "inline":
            bot.reply_to(message, t["inline_ready"].format(bot=_bot_username()), reply_markup=_inline_try_markup(t))
            return True

        if payload.startswith("yt_") and platforms.youtube_video_id(f"https://youtu.be/{payload[3:]}"):
            fake = copy.copy(message)
            fake.text = f"https://www.youtube.com/watch?v={payload[3:]}"
            handle_media_link(fake)
            return True

        if payload.startswith("dl_"):
            data = _inline_tokens.get(payload[3:])
            if not data:
                bot.reply_to(message, t["audio_expired"])
                return True
            fake = copy.copy(message)
            fake.text = data["url"]
            handle_media_link(fake)
            return True

        return False

    @bot.message_handler(
        commands=["start"]
    )
    def send_welcome(message):
        store.track_user(
            message.chat.id
        )

        chat_id = str(
            message.chat.id
        )

        is_new_user = (
            chat_id not in user_settings
        )

        user = store.get_user(
            user_settings,
            chat_id,
        )

        language = user.get(
            "lang",
            "en",
        )

        if language not in {
            "en",
            "fa",
        }:
            language = "en"

        user["lang"] = language

        parts = (message.text or "").split(maxsplit=1)
        payload = re.sub(r"[^A-Za-z0-9_-]", "", parts[1])[:64] if len(parts) > 1 else ""
        is_action_payload = payload in ("gate", "inline") or payload.startswith(("yt_", "dl_"))

        if is_new_user:
            user["joined"] = time.strftime("%Y-%m-%d")

            # t.me/<bot>?start=<source> arrives as "/start <source>" —
            # one link per ad campaign shows exactly where users came from.
            source = "" if is_action_payload else payload[:32]

            if source and not user.get("source"):
                user["source"] = source
                store.record_source(source)

        user_settings[chat_id] = user

        store.save_user_settings(
            user_settings
        )

        t = TEXTS[language]

        if is_action_payload and _is_private_chat(message.chat.type) and _handle_start_payload(message, payload, t):
            return

        _delete_previous_download_ducks(
            message.chat.id
        )

        _send_duck_reaction(
            message.chat.id,
            "start",
        )

        if is_new_user:
            bot.reply_to(
                message,
                t["welcome"],
                reply_markup=_start_language_markup(),
                parse_mode="Markdown",
            )
        else:
            bot.reply_to(
                message,
                t["welcome"],
                reply_markup=_main_reply_markup() if _is_private_chat(message.chat.type) else None,
                parse_mode="Markdown",
            )

    @bot.message_handler(
        commands=["help"]
    )
    def send_help(message):
        chat_id = str(
            message.chat.id
        )

        t = _texts_for(
            chat_id
        )

        username = _bot_username()
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            _SwitchInlineButton(t["help_try_inline_btn"], switch_inline_query=""),
            InlineKeyboardButton(t["help_add_group_btn"], url=f"https://t.me/{username}?startgroup=true"),
        )

        bot.reply_to(
            message,
            t["help"].format(bot=username),
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    @bot.message_handler(commands=["whoami"])
    def whoami(message):
        bot.reply_to(message, f"🆔 `{message.from_user.id}`", parse_mode="Markdown")

    @bot.message_handler(
        commands=["settings"]
    )
    def open_settings(message):

        if admin.is_owner(
            message.from_user.id
        ):
            text, markup = admin.build_panel(
                _texts_for(
                    message.chat.id
                )
            )

            bot.reply_to(
                message,
                text,
                reply_markup=markup,
                parse_mode="Markdown",
            )

            return

        user = store.get_user(
            user_settings,
            message.chat.id,
        )

        t = TEXTS[
            user.get(
                "lang",
                "en",
            )
        ]

        settings_text = (
            f"{t['settings_msg']}\n\n"
            f"{_current_settings_text(user, t)}"
        )

        bot.reply_to(
            message,
            settings_text,
            reply_markup=_settings_markup(
                user,
                t,
            ),
            parse_mode="Markdown",
        )


    @bot.callback_query_handler(
        func=lambda call:
            call.data.startswith("settings_")
            or call.data.startswith("igquality_")
            or call.data.startswith("lowdata_")
            or call.data.startswith("setlang_")
    )
    def handle_settings_callback(call):

        chat_id = str(
            call.message.chat.id
        )

        user = store.get_user(
            user_settings,
            chat_id,
        )

        data = call.data

        if data == "settings_home":

            t = TEXTS[
                user.get(
                    "lang",
                    "en",
                )
            ]

            settings_text = (
                f"{t['settings_msg']}\n\n"
                f"{_current_settings_text(user, t)}"
            )

            bot.edit_message_text(
                settings_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_settings_markup(
                    user,
                    t,
                ),
                parse_mode="Markdown",
            )

            bot.answer_callback_query(
                call.id
            )

            return

        if data == "settings_quality":

            t = TEXTS[
                user.get(
                    "lang",
                    "en",
                )
            ]

            text = (
                f"{t['instagram_quality_title']}\n\n"
                f"{t['instagram_quality_help']}"
            )

            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_instagram_quality_markup(
                    user,
                    t,
                ),
                parse_mode="Markdown",
            )

            bot.answer_callback_query(
                call.id
            )

            return

        if data.startswith("igquality_"):

            selected = data.split(
                "_",
                1,
            )[1]

            if selected not in {
                "best",
                "1080p",
                "720p",
                "480p",
                "360p",
            }:
                bot.answer_callback_query(
                    call.id,
                    "Invalid selection.",
                    show_alert=True,
                )
                return

            user["instagram_quality"] = selected
            user_settings[chat_id] = user

            store.save_user_settings(
                user_settings
            )

            t = TEXTS[
                user.get(
                    "lang",
                    "en",
                )
            ]

            text = (
                f"{t['instagram_quality_title']}\n\n"
                f"{t['instagram_quality_help']}"
            )

            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_instagram_quality_markup(
                    user,
                    t,
                ),
                parse_mode="Markdown",
            )

            bot.answer_callback_query(
                call.id
            )

            return

        if data == "settings_low_data":

            t = TEXTS[
                user.get(
                    "lang",
                    "en",
                )
            ]

            text = (
                f"{t['low_data_title']}\n\n"
                f"{t['low_data_help']}"
            )

            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_low_data_markup(
                    user,
                    t,
                ),
                parse_mode="Markdown",
            )

            bot.answer_callback_query(
                call.id
            )

            return

        if data.startswith("lowdata_"):

            selected = data.split(
                "_",
                1,
            )[1]

            if selected not in {
                "on",
                "off",
            }:
                bot.answer_callback_query(
                    call.id,
                    "Invalid selection.",
                    show_alert=True,
                )
                return

            user["low_data_mode"] = (
                selected == "on"
            )

            user_settings[chat_id] = user

            store.save_user_settings(
                user_settings
            )

            t = TEXTS[
                user.get(
                    "lang",
                    "en",
                )
            ]

            text = (
                f"{t['low_data_title']}\n\n"
                f"{t['low_data_help']}"
            )

            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_low_data_markup(
                    user,
                    t,
                ),
                parse_mode="Markdown",
            )

            bot.answer_callback_query(
                call.id
            )

            return

        if data == "settings_language":

            t = TEXTS[
                user.get(
                    "lang",
                    "en",
                )
            ]

            text = (
                f"{t['language_title']}\n\n"
                f"{t['language_help']}"
            )

            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_language_markup(
                    user,
                    t,
                ),
                parse_mode="Markdown",
            )

            bot.answer_callback_query(
                call.id
            )

            return

        if data.startswith("setlang_"):

            selected = data.split(
                "_",
                1,
            )[1]

            if selected not in {
                "fa",
                "en",
            }:
                bot.answer_callback_query(
                    call.id,
                    "Invalid language.",
                    show_alert=True,
                )
                return

            user["lang"] = selected

            user_settings[chat_id] = user

            store.save_user_settings(
                user_settings
            )

            t = TEXTS[
                selected
            ]

            settings_text = (
                f"{t['settings_msg']}\n\n"
                f"{_current_settings_text(user, t)}"
            )

            bot.edit_message_text(
                settings_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_settings_markup(
                    user,
                    t,
                ),
                parse_mode="Markdown",
            )

            bot.answer_callback_query(
                call.id
            )

            return

        if data == "settings_current":

            t = TEXTS[
                user.get(
                    "lang",
                    "en",
                )
            ]

            bot.edit_message_text(
                _current_settings_text(
                    user,
                    t,
                ),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton(
                        text=t["settings_back"],
                        callback_data="settings_home",
                    )
                ),
                parse_mode="Markdown",
            )

            bot.answer_callback_query(
                call.id
            )

            return

        if data == "settings_reset":

            t = TEXTS[
                user.get(
                    "lang",
                    "en",
                )
            ]

            bot.edit_message_text(
                f"{t['reset_title']}\n\n"
                f"{t['reset_warning']}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_reset_markup(
                    t
                ),
                parse_mode="Markdown",
            )

            bot.answer_callback_query(
                call.id
            )

            return

        if data == "settings_reset_confirm":

            user = {
                "lang": store.DEFAULT_LANGUAGE,
                "quality": store.DEFAULT_QUALITY,
                "instagram_quality": store.DEFAULT_INSTAGRAM_QUALITY,
                "low_data_mode": store.DEFAULT_LOW_DATA_MODE,
                # Where the user came from isn't a preference; keep it.
                **{
                    key: user[key]
                    for key in ("source", "joined")
                    if key in user
                },
            }

            user_settings[chat_id] = user

            store.save_user_settings(
                user_settings
            )

            t = TEXTS[
                user["lang"]
            ]

            bot.edit_message_text(
                t["reset_done"],
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_settings_markup(
                    user,
                    t,
                ),
                parse_mode="Markdown",
            )

            bot.answer_callback_query(
                call.id
            )

            return

    @bot.message_handler(commands=["adrequest"])
    @bot.message_handler(
        func=lambda msg:
            bool(msg.text)
            and msg.text.strip()
            == AD_BUTTON_TEXT
    )
    def start_ad_request(message):
        user_id = message.from_user.id
        chat_id_int = message.chat.id

        if store.is_banned(
            user_id
        ):
            return

        existing_draft = (
            store.get_user_ad_request(
                user_id,
                "draft",
            )
        )

        if existing_draft:
            _send_ad_prompt(
                chat_id_int,
                existing_draft,
            )
            return

        existing_pending = (
            store.get_user_ad_request(
                user_id,
                "pending",
            )
        )

        if existing_pending:
            ad_t = _ad_texts(
                "fa"
            )

            bot.send_message(
                chat_id_int,
                ad_t["existing_pending"],
                reply_markup=_main_reply_markup(),
            )
            return

        telegram_username = (
            message.from_user.username
            or ""
        )

        if telegram_username:
            telegram_username = (
                "@"
                + telegram_username
            )

        telegram_name = " ".join(
            part
            for part in (
                message.from_user.first_name,
                message.from_user.last_name,
            )
            if part
        ).strip()

        if not telegram_name:
            telegram_name = "—"

        request = (
            store.create_ad_request(
                user_id=user_id,
                telegram_username=telegram_username,
                telegram_name=telegram_name,
            )
        )

        request = (
            store.update_ad_request(
                request["request_id"],
                ad_form_lang="fa",
                step="channel",
            )
        )

        _send_ad_prompt(
            chat_id_int,
            request,
        )
    

    @bot.message_handler(
        func=lambda msg:
            bool(msg.text)
            and store.get_user_ad_request(
                msg.from_user.id,
                "draft",
            ) is not None
    )
    def handle_ad_request_input(message):
        user_id = (
            message.from_user.id
        )

        chat_id_int = (
            message.chat.id
        )

        request = (
            store.get_user_ad_request(
                user_id,
                "draft",
            )
        )

        if not request:
            return

        ad_t = _ad_texts(
            _ad_lang_for_request(request)
        )

        text = (
            message.text
            or ""
        ).strip()

        if text.lower() in {
            "لغو",
            "cancel",
        }:
            store.update_ad_request(
                request["request_id"],
                status="cancelled",
            )

            bot.send_message(
                chat_id_int,
                ad_t["cancelled"],
                reply_markup=_main_reply_markup(),
            )
            return

        step = request.get(
            "step"
        )

        if step == "channel":
            channel_value = (
                ads.normalize_sponsor_channel(
                    text
                )
            )

            if not channel_value:
                bot.send_message(
                    chat_id_int,
                    (
                        "⚠️ لطفاً یک یوزرنیم عمومی کانال مانند "
                        "@MyChannel یا https://t.me/MyChannel ارسال کنید."
                    ),
                    reply_markup=_ad_cancel_markup(
                        request
                    ),
                )
                return

            updated = (
                store.update_ad_request(
                    request["request_id"],
                    channel=channel_value,
                    step="display_name",
                )
            )

        elif step == "display_name":
            updated = (
                store.update_ad_request(
                    request["request_id"],
                    display_name=text,
                    step="type",
                )
            )

        elif step == "duration":
            updated = (
                store.update_ad_request(
                    request["request_id"],
                    duration=text,
                    step="notes",
                )
            )

        elif step == "notes":
            updated = (
                store.update_ad_request(
                    request["request_id"],
                    notes=text,
                )
            )

            if not updated:
                return

            bot.send_message(
                chat_id_int,
                _format_ad_summary(
                    updated,
                ),
                reply_markup=_ad_confirmation_markup(
                    updated
                ),
            )
            return

        else:
            return

        if updated:
            _send_ad_prompt(
                chat_id_int,
                updated,
            )

    @bot.callback_query_handler(
        func=lambda call:
            call.data.startswith(
                "ad_type_"
            )
    )
    def handle_ad_type_callback(
        call
    ):
        chat_id_int = (
            call.message.chat.id
        )

        user_id = (
            call.from_user.id
        )

        request = (
            store.get_user_ad_request(
                user_id,
                "draft",
            )
        )

        if not request:
            bot.answer_callback_query(
                call.id,
                _ad_texts("fa")["cancelled"],
                show_alert=True,
            )
            return

        type_key = (
            call.data.split(
                "ad_type_",
                1,
            )[1]
        )

        if type_key not in {
            "sponsor_channel",
            "post_download",
        }:
            bot.answer_callback_query(
                call.id,
                _ad_texts(
                    _ad_lang_for_request(request)
                )["invalid_type"],
                show_alert=True,
            )
            return

        updated = (
            store.update_ad_request(
                request["request_id"],
                ad_type=type_key,
                step="duration",
            )
        )

        bot.answer_callback_query(
            call.id
        )

        try:
            bot.edit_message_reply_markup(
                chat_id_int,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass

        if updated:
            _send_ad_prompt(
                chat_id_int,
                updated,
            )

    @bot.callback_query_handler(
        func=lambda call:
            call.data in {
                "ad_lang_en",
                "ad_lang_fa",
            }
    )
    def handle_ad_language_callback(
        call
    ):
        chat_id_int = (
            call.message.chat.id
        )

        user_id = (
            call.from_user.id
        )

        request = (
            store.get_user_ad_request(
                user_id,
                "draft",
            )
        )

        if not request:
            bot.answer_callback_query(
                call.id
            )
            return

        selected_language = (
            "en"
            if call.data == "ad_lang_en"
            else "fa"
        )

        updated = (
            store.update_ad_request(
                request["request_id"],
                ad_form_lang=selected_language,
            )
        )

        bot.answer_callback_query(
            call.id
        )

        try:
            bot.edit_message_reply_markup(
                chat_id_int,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass

        if updated:
            _send_ad_prompt(
                chat_id_int,
                updated,
            )

    @bot.callback_query_handler(
        func=lambda call:
            call.data == "ad_cancel"
    )
    def handle_ad_cancel(
        call
    ):
        chat_id_int = (
            call.message.chat.id
        )

        user_id = (
            call.from_user.id
        )

        request = (
            store.get_user_ad_request(
                user_id,
                "draft",
            )
        )

        if request:
            ad_t = _ad_texts(
                _ad_lang_for_request(request)
            )

            store.update_ad_request(
                request["request_id"],
                status="cancelled",
            )
        else:
            ad_t = _ad_texts(
                "fa"
            )

        try:
            bot.edit_message_reply_markup(
                chat_id_int,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass

        bot.answer_callback_query(
            call.id
        )

        bot.send_message(
            chat_id_int,
            ad_t["cancelled"],
            reply_markup=_main_reply_markup(),
        )

    @bot.callback_query_handler(
        func=lambda call:
            call.data == "ad_edit"
    )
    def handle_ad_edit(
        call
    ):
        chat_id_int = (
            call.message.chat.id
        )

        user_id = (
            call.from_user.id
        )

        request = (
            store.get_user_ad_request(
                user_id,
                "draft",
            )
        )

        if not request:
            bot.answer_callback_query(
                call.id,
                _ad_texts("fa")["cancelled"],
                show_alert=True,
            )
            return

        updated = (
            store.update_ad_request(
                request["request_id"],
                channel="",
                display_name="",
                ad_type="",
                duration="",
                notes="",
                step="channel",
            )
        )

        bot.answer_callback_query(
            call.id
        )

        try:
            bot.edit_message_reply_markup(
                chat_id_int,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass

        if updated:
            _send_ad_prompt(
                chat_id_int,
                updated,
            )

    @bot.callback_query_handler(
        func=lambda call:
            call.data == "ad_submit"
    )
    def handle_ad_submit(
        call
    ):
        chat_id_int = (
            call.message.chat.id
        )

        user_id = (
            call.from_user.id
        )

        request = (
            store.get_user_ad_request(
                user_id,
                "draft",
            )
        )

        if not request:
            bot.answer_callback_query(
                call.id,
                _ad_texts("fa")["cancelled"],
                show_alert=True,
            )
            return

        ad_t = _ad_texts(
            _ad_lang_for_request(request)
        )

        required_fields = (
            "channel",
            "display_name",
            "ad_type",
            "duration",
            "notes",
        )

        if any(
            not str(
                request.get(
                    field,
                    "",
                )
            ).strip()
            for field in required_fields
        ):
            bot.answer_callback_query(
                call.id,
                "اطلاعات درخواست کامل نیست."
                if _ad_lang_for_request(request) == "fa"
                else "The request is incomplete.",
                show_alert=True,
            )
            return

        owner_id = int(
            os.environ.get(
                "OWNER_ID",
                "0",
            ) or "0"
        )

        if owner_id == 0:
            bot.answer_callback_query(
                call.id,
                "Admin is not configured.",
                show_alert=True,
            )
            return

        pending = (
            store.update_ad_request(
                request["request_id"],
                status="pending",
            )
        )

        if not pending:
            bot.answer_callback_query(
                call.id,
                "Could not save request.",
                show_alert=True,
            )
            return

        username_text = (
            pending.get(
                "telegram_username"
            )
            or "ندارد"
        )

        admin_type_labels = {
            "sponsor_channel": "Sponsor Channel",
            "post_download": "Post-Download Ad",
        }

        admin_type = admin_type_labels.get(
            pending.get("ad_type"),
            pending.get("ad_type", "—"),
        )

        admin_texts = _texts_for(
            owner_id
        )

        admin_message = (
            f"{admin_texts['ad_admin_new']}\n\n"
            f"{admin_texts['ad_admin_request_id']}: "
            f"{pending['request_id']}\n\n"
            f"{admin_texts['ad_admin_user']}\n"
            f"{admin_texts['ad_admin_username']}: "
            f"{username_text}\n"
            f"{admin_texts['ad_admin_user_id']}: "
            f"{pending['user_id']}\n"
            f"{admin_texts['ad_admin_name']}: "
            f"{pending.get('telegram_name', '—')}\n\n"
            f"{admin_texts['ad_admin_channel']}: "
            f"{pending.get('channel', '—')}\n"
            f"{admin_texts['ad_admin_display_name']}: "
            f"{pending.get('display_name', '—')}\n"
            f"{admin_texts['ad_admin_type']}: "
            f"{admin_type}\n"
            f"{admin_texts['ad_admin_duration']}: "
            f"{pending.get('duration', '—')}\n"
            f"{admin_texts['ad_admin_notes']}: "
            f"{pending.get('notes', '—')}\n"
            f"{admin_texts['ad_admin_created_at']}: "
            f"{pending.get('created_at', '—')}"
        )

        markup = InlineKeyboardMarkup(
            row_width=2
        )

        markup.add(
            InlineKeyboardButton(
                admin_texts[
                    "ad_admin_approve"
                ],
                callback_data=(
                    "adm_adreq_approve_"
                    + pending["request_id"]
                ),
            ),
            InlineKeyboardButton(
                admin_texts[
                    "ad_admin_reject"
                ],
                callback_data=(
                    "adm_adreq_reject_"
                    + pending["request_id"]
                ),
            ),
        )

        markup.add(
            InlineKeyboardButton(
                admin_texts[
                    "ad_admin_contact"
                ],
                url=(
                    "tg://user?id="
                    + str(
                        pending["user_id"]
                    )
                ),
            )
        )

        try:
            admin_message_result = (
                bot.send_message(
                    owner_id,
                    admin_message,
                    reply_markup=markup,
                )
            )

        except Exception:
            store.update_ad_request(
                pending["request_id"],
                status="draft",
            )

            bot.answer_callback_query(
                call.id,
                "ارسال درخواست به مدیر انجام نشد. دوباره تلاش کنید.",
                show_alert=True,
            )
            return

        store.update_ad_request(
            pending["request_id"],
            admin_message_id=(
                admin_message_result.message_id
            ),
        )

        try:
            bot.edit_message_reply_markup(
                chat_id_int,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass

        bot.answer_callback_query(
            call.id
        )

        bot.send_message(
            chat_id_int,
            ad_t["submitted"],
            reply_markup=_main_reply_markup(),
        )

    @bot.message_handler(func=lambda msg: bool(msg.text) and platforms.detect_platform(msg.text) is not None)
    def handle_media_link(message):
        chat_id_str = str(message.chat.id)
        chat_id_int = message.chat.id
        user_id = message.from_user.id

        show_ui = _is_private_chat(
            message.chat.type
        )

        if store.is_banned(user_id):
            return  # silently ignore banned users

        store.track_user(chat_id_int)
        user = store.get_user(user_settings, chat_id_str)
        t = TEXTS[user['lang']]

        last_link_messages[
            (
                chat_id_int,
                user_id,
            )
        ] = message

        if show_ui and not _check_sponsor_channel_gate(
            chat_id_int,
            user_id,
            t,
        ):
            return

        url = platforms.extract_url(
            message.text
        )

        if not url:
            return

        if "tiktok.com" in url.lower():
            url = platforms.resolve_tiktok_url(
                url
            )

        if show_ui:
            _delete_previous_download_ducks(
                chat_id_int
            )

        platform = platforms.detect_platform(
            url
        )

        if not flags.get(platform, True):
            bot.reply_to(message, t['not_launched'].format(platform=platforms.PLATFORM_NAMES[platform]))
            return

        # YouTube always gets the thumbnail+buttons quality picker — in
        # groups too, where only the person who sent the link can use it.
        if platform == "youtube":
            _send_youtube_quality_picker(message, t, user['lang'], show_ui=show_ui,)
            return

        if _is_rate_limited(user_id):
            bot.reply_to(message, t['rate_limited'].format(limit=RATE_LIMIT_COUNT))
            return

        status_msg = None
        if show_ui:
            status_msg = bot.reply_to(
                message,
                t['init'],
            )
            bot.send_chat_action(
                chat_id_int,
                'typing',
            )

        if platform == "spotify":
            slot = _acquire_download_slot(
                bot,
                chat_id_int,
                status_msg,
                t,
                show_ui=show_ui,
                platform="spotify",
            )

            if slot is None:
                return

            ticker = _ProgressTicker(
                bot, chat_id_int, status_msg, t, show_ui,
                duration_model.expected("spotify:track"),
            ).start()

            _send_duck_reaction(
                chat_id_int,
                "downloading",
                track_status=True,
                show_ui=show_ui,
            )

            try:
                tracks = platforms.resolve_spotify_tracks(url)
                if not tracks:
                    raise Exception("Spotify returned no playable tracks for this link.")

                if not show_ui and len(tracks) > 1:
                    # An album would be 20 files in a row in the group.
                    ticker.stop()
                    bot.reply_to(message, t["group_album_private"], reply_markup=_open_bot_markup(t))
                    return

                # An album takes about N tracks' worth of time.
                ticker.set_expected(
                    ticker.elapsed()
                    + duration_model.expected("spotify:track") * len(tracks)
                )

                failed_tracks = []
                last_track_error = None
                sent_count = 0
                cover_thumb = None

                try:
                    for i, track in enumerate(tracks):
                        track_label = f"{track['artists']} - {track['name']}"
                        track_started = time.monotonic()

                        ticker.set_phase("downloading")
                        if len(tracks) > 1:
                            ticker.set_note(
                                t['spotify_searching'].format(
                                    i=i + 1,
                                    total=len(tracks),
                                    name=track_label,
                                )
                            )

                        # One track that can't be found must not abort the
                        # rest of an album or playlist.
                        try:
                            filepath = platforms.download_spotify_track(track, None)
                        except platforms.FileTooLargeError:
                            raise
                        except Exception as track_error:
                            last_track_error = track_error
                            failed_tracks.append(track_label)
                            logger.warning(
                                "Spotify track failed | %s | %s",
                                track_label,
                                str(track_error)[:300],
                            )
                            continue

                        try:
                            ticker.set_phase("uploading")

                            if show_ui:
                                bot.send_chat_action(chat_id_int, 'upload_audio')

                            if cover_thumb is None:
                                cover_thumb = platforms.make_thumbnail(track.get('cover_url')) or ""

                            caption = (
                                f"💿 {track['album']}\n\n{BOT_SIGNATURE}"
                                if sent_count == 0 and track.get('album')
                                else ""
                            )
                            audio_duration = int((track.get('duration_ms') or 0) / 1000) or None

                            with open(filepath, "rb") as audio_file:
                                thumb_file = open(cover_thumb, "rb") if cover_thumb else None
                                try:
                                    sent_audio = bot.send_audio(
                                        chat_id_int, audio_file,
                                        title=track['name'], performer=track['artists'],
                                        duration=audio_duration,
                                        thumbnail=thumb_file,
                                        caption=caption, reply_to_message_id=message.message_id, timeout=600,
                                    )
                                finally:
                                    if thumb_file:
                                        thumb_file.close()
                        finally:
                            platforms.remove_download_files([filepath])

                        sent_count += 1
                        store.record_download("spotify", chat_id_int)

                        audio_ref = _file_ref(sent_audio)
                        if audio_ref and chat_id_int > 0:
                            store.add_recent(chat_id_int, f"spotify:{track.get('name')}:{track.get('artists')}",
                                             f"{track['artists']} - {track['name']}", [audio_ref])
                        duration_model.record("spotify:track", time.monotonic() - track_started)
                finally:
                    if cover_thumb:
                        platforms.remove_download_files([cover_thumb])

                if sent_count == 0:
                    raise last_track_error or Exception("No Spotify track could be matched.")

                if failed_tracks:
                    store.record_error(
                        platform="spotify",
                        url=url,
                        user_id=message.from_user.id,
                        error=(
                            f"{len(failed_tracks)}/{len(tracks)} tracks not found: "
                            + "; ".join(failed_tracks)[:1500]
                            + f" | last error: {last_track_error}"
                        ),
                    )
                    if show_ui:
                        names = "\n".join(f"• {name}" for name in failed_tracks[:20])
                        try:
                            bot.send_message(
                                chat_id_int,
                                t['spotify_partial'].format(
                                    failed=len(failed_tracks),
                                    total=len(tracks),
                                    names=names,
                                ),
                            )
                        except Exception:
                            pass

                ticker.stop()

                if show_ui and status_msg is not None:
                    try:
                        bot.delete_message(
                            chat_id_int,
                            status_msg.message_id,
                        )
                    except Exception:
                        pass

                _send_duck_download_complete(
                    chat_id_int,
                    show_ui=show_ui,
                )

                if show_ui:
                    _maybe_send_ad(
                        chat_id_int
                    )
            except Exception as e:
                ticker.stop()

                _send_duck_download_failed(
                    chat_id_int,
                    show_ui=show_ui,
                )

                if not isinstance(e, platforms.UnsupportedLinkError):
                    store.record_error(
                        platform="spotify",
                        url=url,
                        user_id=message.from_user.id,
                        error=str(e),
                    )

                    logger.exception(
                        "Spotify download failed | url=%s",
                        url,
                    )

                try:
                    if isinstance(e, platforms.FileTooLargeError):
                        error_text = t["too_large"].format(size=str(e))
                    else:
                        error_text = _friendly_download_error("spotify", e, t)

                    bot.edit_message_text(
                        error_text,
                        chat_id_int,
                        status_msg.message_id,
                    )
                except Exception:
                    pass
            finally:
                ticker.stop()
                slot.release()
            return

        download_quality = (
            user.get(
                "instagram_quality",
                "best",
            )
            if platform == "instagram"
            else user.get(
                "quality",
                "best",
            )
        )

        if (
            platform == "instagram"
            and user.get(
                "low_data_mode",
                False,
            )
        ):
            download_quality = "480p"

        _run_direct_download(
            chat_id_int,
            message.message_id,
            url,
            download_quality,
            t,
            status_msg,
            show_ui=show_ui,
        )

    @bot.callback_query_handler(
        func=lambda call:
            call.data.startswith(
                "sponsor_check_"
            )
    )
    def handle_sponsor_check_callback(
        call
    ):
        chat_id_int = (
            call.message.chat.id
        )

        chat_id_str = str(
            chat_id_int
        )

        user_id = (
            call.from_user.id
        )

        t = _texts_for(
            chat_id_str
        )

        if store.is_banned(
            user_id
        ):
            bot.answer_callback_query(
                call.id
            )
            return

        try:
            requested_user_id = int(
                call.data.split(
                    "sponsor_check_",
                    1,
                )[1]
            )
        except (
            ValueError,
            IndexError,
        ):
            bot.answer_callback_query(
                call.id,
                "Invalid membership check.",
                show_alert=True,
            )
            return

        if requested_user_id != user_id:
            bot.answer_callback_query(
                call.id,
                "This button belongs to another user.",
                show_alert=True,
            )
            return

        current_flags = store.load_flags()

        if not current_flags.get(
            "sponsor_channel_gate",
            True,
        ):
            bot.answer_callback_query(
                call.id
            )

            try:
                bot.edit_message_reply_markup(
                    chat_id_int,
                    call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass

            last_message = (
                last_link_messages.pop(
                    (
                        chat_id_int,
                        user_id,
                    ),
                    None,
                )
            )

            if last_message:
                handle_media_link(
                    last_message
                )

            return

        if store.is_exempt(
            user_id
        ):
            bot.answer_callback_query(
                call.id
            )

            try:
                bot.edit_message_reply_markup(
                    chat_id_int,
                    call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass

            last_message = (
                last_link_messages.pop(
                    (
                        chat_id_int,
                        user_id,
                    ),
                    None,
                )
            )

            if last_message:
                handle_media_link(
                    last_message
                )

            return

        unjoined = (
            ads.get_unjoined_channels(
                bot,
                user_id,
            )
        )

        if unjoined:
            markup = InlineKeyboardMarkup(
                row_width=1
            )

            for channel in unjoined:
                username = (
                    channel.get(
                        "username",
                        "",
                    )
                    or ""
                ).strip()

                if not username:
                    continue

                if username.startswith(
                    "@"
                ):
                    channel_url = (
                        "https://t.me/"
                        + username[1:]
                    )
                else:
                    channel_url = (
                        "https://t.me/"
                        + username
                    )

                channel_name = (
                    channel.get(
                        "name",
                        "",
                    )
                    or username
                )

                markup.add(
                    InlineKeyboardButton(
                        text=t[
                            "sponsor_gate_join"
                        ].format(
                            name=channel_name
                        ),
                        url=channel_url,
                    )
                )

            if markup.keyboard:
                markup.add(
                    InlineKeyboardButton(
                        t[
                            "sponsor_gate_check"
                        ],
                        callback_data=(
                            "sponsor_check_"
                            + str(
                                user_id
                            )
                        ),
                    )
                )

                try:
                    bot.edit_message_reply_markup(
                        chat_id_int,
                        call.message.message_id,
                        reply_markup=markup,
                    )
                except Exception:
                    pass

            bot.answer_callback_query(
                call.id,
                t[
                    "sponsor_gate_not_joined"
                ],
                show_alert=True,
            )

            return

        bot.answer_callback_query(
            call.id
        )

        try:
            bot.edit_message_reply_markup(
                chat_id_int,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass

        last_message = (
            last_link_messages.pop(
                (
                    chat_id_int,
                    user_id,
                ),
                None,
            )
        )

        if not last_message:
            # Came from the inline-mode gate: nothing to resume, just confirm.
            try:
                bot.send_message(chat_id_int, t["inline_ready"].format(bot=_bot_username()), reply_markup=_inline_try_markup(t))
            except Exception:
                pass
            return

        handle_media_link(
            last_message
        )
    
    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(
            "caption_"
        )
    )
    def handle_caption_toggle(call):
        if call.message.chat.type == "private":
            bot.answer_callback_query(
                call.id
            )
            return

        chat_id_str = str(
            call.message.chat.id
        )

        t = _texts_for(
            chat_id_str
        )

        post_id = call.data.split(
            "caption_",
            1,
        )[1]

        data = caption_cache.get(
            post_id
        )

        if not data:
            bot.answer_callback_query(
                call.id,
                "This caption is no longer available.",
                show_alert=True,
            )
            return

        visible = not data.get(
            "visible",
            False,
        )

        data["visible"] = visible

        if visible:
            caption = data["caption"]
            button_text = t["hide_caption"]
        else:
            caption = BOT_SIGNATURE
            button_text = t["get_caption"]

        markup = InlineKeyboardMarkup()

        markup.add(
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"caption_{post_id}",
            )
        )

        try:
            bot.edit_message_caption(
                chat_id=data["chat_id"],
                message_id=data["message_id"],
                caption=caption,
                reply_markup=markup,
            )

            bot.answer_callback_query(
                call.id
            )

        except Exception:
            bot.answer_callback_query(
                call.id,
                "Could not update the caption.",
                show_alert=True,
            )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('thumb_'))
    def handle_thumbnail_callback(call):
        chat_id = str(call.message.chat.id)
        t = _texts_for(chat_id)

        post_id = call.data.split('thumb_')[1]
        thumb_url = thumb_cache.get(post_id)

        if not thumb_url:
            bot.answer_callback_query(call.id, t['cover_error'], show_alert=True)
            return

        bot.answer_callback_query(call.id, t['cover_loading'])

        try:
            bot.send_photo(int(chat_id), thumb_url, reply_to_message_id=call.message.message_id)
        except Exception:
            # Telegram can't always fetch CDN URLs itself (signed/expiring
            # Instagram links, WebP YouTube covers) — upload the bytes instead.
            try:
                response = requests.get(thumb_url, timeout=20, headers={"User-Agent": platforms.USER_AGENT})
                response.raise_for_status()
                bot.send_photo(int(chat_id), response.content, reply_to_message_id=call.message.message_id)
            except Exception:
                logger.warning("Cover download failed | %s", thumb_url, exc_info=True)
                try:
                    bot.send_message(int(chat_id), t['cover_error'], reply_to_message_id=call.message.message_id)
                except Exception:
                    pass

    @bot.callback_query_handler(func=lambda call: call.data.startswith('audio_'))
    def handle_get_audio_callback(call):
        chat_id_str = str(call.message.chat.id)
        chat_id_int = call.message.chat.id
        user_id = call.from_user.id
        t = _texts_for(chat_id_str)

        if store.is_banned(user_id):
            bot.answer_callback_query(call.id)
            return

        if not _check_sponsor_channel_gate(
            chat_id_int,
            user_id,
            t,
        ):
            bot.answer_callback_query(
                call.id
            )
            return

        post_id = call.data.split('audio_', 1)[1]
        url = audio_source_cache.get(post_id)
        if not url:
            bot.answer_callback_query(call.id, t['audio_expired'], show_alert=True)
            return

        if _is_rate_limited(user_id):
            bot.answer_callback_query(call.id, t['rate_limited'].format(limit=RATE_LIMIT_COUNT), show_alert=True)
            return

        bot.answer_callback_query(call.id)

        show_ui = _is_private_chat(call.message.chat.type)
        status_msg = None
        if show_ui:
            status_msg = bot.send_message(chat_id_int, t['init'])

        _run_direct_download(
            chat_id_int,
            call.message.message_id,
            url,
            'audio',
            t,
            status_msg,
            show_ui=show_ui,
        )

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(
            "startlang_"
        )
    )
    def handle_start_language_callback(call):
        chat_id = str(
            call.message.chat.id
        )

        selected_language = (
            call.data.split(
                "_",
                1,
            )[1]
        )

        if selected_language not in {
            "en",
            "fa",
        }:
            bot.answer_callback_query(
                call.id,
                "Invalid language.",
                show_alert=True,
            )
            return

        user = store.get_user(
            user_settings,
            chat_id,
        )

        user["lang"] = (
            selected_language
        )

        user_settings[chat_id] = user

        store.save_user_settings(
            user_settings
        )

        t = TEXTS[
            selected_language
        ]

        bot.edit_message_text(
            t["welcome"],
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None,
            parse_mode="Markdown",
        )

        bot.answer_callback_query(
            call.id
        )

        # An edited message can't carry a reply keyboard, so new users
        # would never see the bottom buttons (feedback, ads) otherwise.
        try:
            bot.send_message(
                call.message.chat.id,
                t["feedback_hint"],
                reply_markup=_main_reply_markup(),
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # YouTube quality picker buttons
    # ------------------------------------------------------------------
    # "yq_<requester>_<video id>_<choice>": in groups only the person who
    # sent the link may pick. "ytq_<video id>_<choice>" is the old format,
    # still accepted so buttons sent before this update keep working.

    def _parse_quality_callback(data):
        if data.startswith(YT_PICK_PREFIX):
            requester, _, rest = data[len(YT_PICK_PREFIX):].partition("_")
            if not requester.isdigit() or len(rest) < 13 or rest[11] != "_":
                raise ValueError(data)
            return int(requester), rest[:11], rest[12:]
        video_id, choice = data[len("ytq_"):].rsplit("_", 1)
        return None, video_id, choice

    def _delete_picker(call):
        ephemeral_id = (getattr(call.message, "json", None) or {}).get("ephemeral_message_id")
        try:
            if ephemeral_id:
                _raw_api("deleteEphemeralMessage", {
                    "chat_id": call.message.chat.id,
                    "receiver_user_id": call.from_user.id,
                    "ephemeral_message_id": ephemeral_id,
                })
            else:
                bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(("ytq_", YT_PICK_PREFIX))
    )
    def handle_youtube_quality_pick(call):
        if call.message is None:
            bot.answer_callback_query(call.id)
            return

        chat_id_int = call.message.chat.id
        user_id = call.from_user.id
        show_ui = call.message.chat.type == "private"
        t = _texts_for(chat_id_int)

        if store.is_banned(user_id):
            bot.answer_callback_query(call.id)
            return

        try:
            requester_id, video_id, choice = _parse_quality_callback(call.data)
        except ValueError:
            bot.answer_callback_query(call.id, "Invalid selection.", show_alert=True)
            return

        if not show_ui and requester_id is not None and requester_id != user_id:
            # Someone else tapped the sender's picker: ignore it.
            bot.answer_callback_query(call.id, _texts_for(user_id)["yt_not_yours"], show_alert=True)
            return

        # The sponsor gate is only ever shown in private chats.
        if show_ui and not _check_sponsor_channel_gate(chat_id_int, user_id, t):
            bot.answer_callback_query(call.id)
            return

        if _is_rate_limited(user_id):
            bot.answer_callback_query(
                call.id,
                t["rate_limited"].format(limit=RATE_LIMIT_COUNT),
                show_alert=True,
            )
            return

        bot.answer_callback_query(call.id)

        link_message_id = None

        if not show_ui:
            context = _yt_pick_context.pop((chat_id_int, user_id, video_id), None) or {}
            link_message_id = context.get("link_message_id") or getattr(
                getattr(call.message, "reply_to_message", None), "message_id", None,
            )
            # The picker has done its job; don't leave it cluttering the group.
            _delete_picker(call)

        group_lock = _group_lock(chat_id_int) if not show_ui else contextlib.nullcontext()

        with group_lock:
            _download_youtube_choice(
                chat_id_int, user_id, link_message_id, video_id, choice, t, show_ui,
            )

    def _download_youtube_choice(chat_id_int, user_id, link_message_id, video_id, choice, t, show_ui):
        source_url = f"https://www.youtube.com/watch?v={video_id}"
        cache_key = platforms.media_cache_key(source_url, choice)

        # The same video at the same quality was already sent somewhere:
        # re-send it by file_id (common when a video goes around groups).
        cached = media_cache.get(cache_key) if cache_key else None

        if cached and time.time() - cached["time"] < MEDIA_CACHE_TTL:
            try:
                _send_cached_result(chat_id_int, link_message_id, source_url, "youtube", cached, t, show_ui=show_ui)
                if show_ui:
                    _maybe_send_ad(chat_id_int)
                return
            except Exception:
                logger.warning("Cached YouTube re-send failed; downloading again | %s", cache_key, exc_info=True)
                media_cache.pop(cache_key, None)

        status_msg = bot.send_message(chat_id_int, t["init"]) if show_ui else None

        slot = _acquire_download_slot(
            bot,
            chat_id_int,
            status_msg,
            t,
            show_ui=show_ui,
            platform="youtube",
        )

        if slot is None:
            return

        _set_reaction(chat_id_int, link_message_id, "👀")

        # The picker already knows this option's size: a far better guess
        # than an average over every YouTube video.
        timing_key = f"youtube:{'audio' if choice == 'audio' else 'video'}"
        ticker = _ProgressTicker(
            bot, chat_id_int, status_msg, t, show_ui,
            duration_model.expected(
                timing_key,
                platforms.youtube_option_size(video_id, choice),
            ),
        ).start()

        _send_duck_reaction(
            chat_id_int,
            "downloading",
            track_status=True,
            show_ui=show_ui,
        )

        files = []

        try:
            info, entries, files = platforms.download_youtube_quality(
                video_id,
                choice,
                ticker.hook,
            )

            if show_ui:
                _finish_duck_download(chat_id_int)

            ticker.set_phase("uploading")
            delivered_bytes = _files_size(files)

            _send_download_result(
                chat_id_int,
                link_message_id,
                source_url,
                "youtube",
                choice,
                choice,
                info,
                entries,
                files,
                t,
                show_ui=show_ui,
                cache_key=cache_key,
            )

            ticker.stop()
            duration_model.record(timing_key, ticker.elapsed(), delivered_bytes)
            _set_reaction(chat_id_int, link_message_id, None)

            _send_duck_download_complete(
                chat_id_int,
                show_ui=show_ui,
            )

            if show_ui and status_msg is not None:
                try:
                    bot.delete_message(chat_id_int, status_msg.message_id)
                except Exception:
                    pass

            if show_ui:
                _maybe_send_ad(chat_id_int)

        except platforms.FileTooLargeError as e:
            ticker.stop()
            _set_reaction(chat_id_int, link_message_id, "🤷")

            store.record_error(
                platform="youtube",
                url=source_url,
                user_id=user_id,
                error=str(e),
            )

            if show_ui and status_msg is not None:
                try:
                    bot.edit_message_text(
                        t["too_large"].format(size=str(e)),
                        chat_id_int,
                        status_msg.message_id,
                    )
                except Exception:
                    pass

        except Exception as e:
            ticker.stop()
            _set_reaction(chat_id_int, link_message_id, "🤷")

            store.record_error(
                platform="youtube",
                url=source_url,
                user_id=user_id,
                error=str(e),
            )

            logger.exception(
                "YouTube quality download failed | url=%s | choice=%s",
                source_url,
                choice,
            )

            if show_ui and status_msg is not None:
                try:
                    bot.edit_message_text(
                        _friendly_download_error("youtube", e, t),
                        chat_id_int,
                        status_msg.message_id,
                    )
                except Exception:
                    pass

        finally:
            ticker.stop()
            platforms.remove_download_files(files)

            slot.release()

    # ------------------------------------------------------------------
    # Groups: notice when the bot is added or removed
    # ------------------------------------------------------------------

    @bot.my_chat_member_handler()
    def handle_bot_membership(update):
        chat = update.chat

        if chat.type not in ("group", "supergroup"):
            return

        old_status = update.old_chat_member.status
        new_status = update.new_chat_member.status
        _bot_admin_cache.pop(chat.id, None)

        if new_status in ("left", "kicked"):
            store.record_group(chat.id, chat.title, False)
            return

        store.record_group(chat.id, chat.title, True)

        if old_status in ("left", "kicked") and new_status in ("member", "administrator"):
            t = _texts_for(chat.id)
            text = t["group_intro"]

            # With privacy mode on, a non-admin bot never sees plain links.
            if new_status != "administrator" and not getattr(_bot_me(), "can_read_all_group_messages", False):
                text += t["group_intro_needs_admin"]

            try:
                bot.send_message(chat.id, text)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Inline mode: "@DuckDownloader_Bot <link>" in any chat
    # ------------------------------------------------------------------
    # Already-downloaded media is offered straight from the cache. Anything
    # else is offered as a "⏳" placeholder: when the user picks it, the bot
    # downloads the file, uploads it to the private cache channel (an inline
    # message can only become media that's already on Telegram) and turns
    # the placeholder into the file. Telegram shows "via @DuckDownloader_Bot"
    # on every such message.

    def _inline_gate_ok(user_id) -> bool:
        current_flags = store.load_flags()

        if not current_flags.get("sponsor_channel_gate", True) or store.is_exempt(user_id):
            return True

        cached = _inline_gate_cache.get(user_id)

        if cached and time.monotonic() - cached[0] < INLINE_GATE_CACHE_SECONDS:
            return cached[1]

        passed = not ads.get_unjoined_channels(bot, user_id)
        _inline_gate_cache[user_id] = (time.monotonic(), passed)
        return passed

    def _inline_markup(t, more_token=None):
        markup = InlineKeyboardMarkup()

        if more_token:
            markup.add(
                InlineKeyboardButton(
                    t["inline_more_btn"],
                    url=f"https://t.me/{_bot_username()}?start=dl_{more_token}",
                )
            )

        # Types "@DuckDownloader_Bot " into the input field of whoever taps
        # it — in this same chat — so the next link is one tap away.
        markup.add(
            _SwitchInlineButton(
                t["inline_next_btn"],
                switch_inline_query_current_chat="",
            )
        )
        return markup

    def _inline_quality(user_id, platform):
        user = store.get_user(user_settings, user_id)

        if platform == "instagram":
            return "480p" if user.get("low_data_mode", False) else user.get("instagram_quality", "best")

        return "best"

    def _cached_inline_results(cached, t, title):
        results = []

        for index, (kind, file_id) in enumerate(cached["items"][:10]):
            result_id = f"c{index}"
            markup = _inline_markup(t)

            if kind == "video":
                results.append(InlineQueryResultCachedVideo(
                    result_id, file_id, title, caption=BOT_SIGNATURE, reply_markup=markup,
                ))
            elif kind == "photo":
                results.append(InlineQueryResultCachedPhoto(
                    result_id, file_id, title=title, caption=BOT_SIGNATURE, reply_markup=markup,
                ))
            elif kind == "audio":
                results.append(InlineQueryResultCachedAudio(
                    result_id, file_id, caption=BOT_SIGNATURE, reply_markup=markup,
                ))
            elif kind == "document":
                results.append(InlineQueryResultCachedDocument(
                    result_id, file_id, title, caption=BOT_SIGNATURE, reply_markup=markup,
                ))

        return results

    def _recent_inline_results(user_id, t):
        results = []

        for index, entry in enumerate(store.get_recent(user_id)[:10]):
            items = entry.get("items") or []
            if not items:
                continue

            kind, file_id = items[0]
            result_id = f"r{index}"
            title = (entry.get("title") or "🦆")[:60]
            markup = _inline_markup(t)

            if kind == "video":
                results.append(InlineQueryResultCachedVideo(
                    result_id, file_id, title, description=t["inline_recent_desc"],
                    caption=BOT_SIGNATURE, reply_markup=markup,
                ))
            elif kind == "photo":
                results.append(InlineQueryResultCachedPhoto(
                    result_id, file_id, title=title, caption=BOT_SIGNATURE, reply_markup=markup,
                ))
            elif kind == "audio":
                results.append(InlineQueryResultCachedAudio(
                    result_id, file_id, caption=BOT_SIGNATURE, reply_markup=markup,
                ))
            elif kind == "document":
                results.append(InlineQueryResultCachedDocument(
                    result_id, file_id, title, description=t["inline_recent_desc"],
                    caption=BOT_SIGNATURE, reply_markup=markup,
                ))

        return results

    def _fresh_cache(cache_key):
        cached = media_cache.get(cache_key) if cache_key else None
        if cached and time.time() - cached["time"] < MEDIA_CACHE_TTL:
            return cached
        return None

    def _placeholder_result(t, token, title, description="", thumbnail_url=None):
        return InlineQueryResultArticle(
            f"d:{token}",
            title,
            InputTextMessageContent(t["inline_placeholder"]),
            reply_markup=_inline_markup(t),
            description=description,
            thumbnail_url=thumbnail_url,
        )

    def _answer_inline(query, results, button=None, cache_time=0):
        try:
            bot.answer_inline_query(
                query.id,
                results,
                cache_time=cache_time,
                is_personal=True,
                button=button,
            )
        except Exception:
            logger.warning("answer_inline_query failed", exc_info=True)

    @bot.inline_handler(func=lambda query: True)
    def handle_inline_query(query):
        user_id = query.from_user.id
        t = _texts_for(user_id)

        if store.is_banned(user_id):
            _answer_inline(query, [])
            return

        url = platforms.extract_url(query.query or "")

        if not url:
            # Just "@DuckDownloader_Bot": offer the user's own recent
            # downloads, ready to send again.
            help_button = InlineQueryResultsButton(t["inline_help_btn"], start_parameter="inline")

            if not _inline_gate_ok(user_id):
                _answer_inline(query, [], InlineQueryResultsButton(t["inline_gate_btn"], start_parameter="gate"))
                return

            _answer_inline(query, _recent_inline_results(user_id, t), help_button)
            return

        if not _inline_gate_ok(user_id):
            _answer_inline(query, [], InlineQueryResultsButton(t["inline_gate_btn"], start_parameter="gate"))
            return

        platform = platforms.detect_platform(url)

        if not platform or not flags.get(platform, True):
            _answer_inline(query, [])
            return

        has_cache_channel = _cache_chat_id() is not None

        # ---------------- YouTube: quality picked inside the chat -------
        # One result; once sent, the message itself shows the quality
        # buttons (only the sender's taps count) and then becomes the video.
        if platform == "youtube":
            video_id = platforms.youtube_video_id(url)

            if not video_id:
                _answer_inline(query, [])
                return

            watch_url = f"https://www.youtube.com/watch?v={video_id}"

            if not has_cache_channel:
                _answer_inline(query, [], InlineQueryResultsButton(t["inline_in_bot_btn"], start_parameter=f"yt_{video_id}"))
                return

            known_title = ((platforms.cached_youtube_probe(watch_url) or {}).get("title") or "")[:100]
            title = known_title or t["inline_yt_title"]
            token = _new_inline_token({
                "mode": "youtube", "url": watch_url, "video_id": video_id, "user_id": user_id,
            })

            _answer_inline(query, [
                InlineQueryResultArticle(
                    f"y:{token}",
                    title,
                    InputTextMessageContent(t["inline_yt_loading"].format(title=known_title or "YouTube")),
                    reply_markup=_inline_markup(t),
                    description=t["inline_yt_desc"],
                    thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                )
            ])
            return

        # ---------------- Spotify: single tracks only ------------------
        if platform == "spotify":
            cache_key = platforms.media_cache_key(url, "audio")

            if not cache_key:
                _answer_inline(query, [], InlineQueryResultsButton(t["inline_spotify_bot"], start_parameter="inline"))
                return

            cached = _fresh_cache(cache_key)

            if cached:
                _answer_inline(query, _cached_inline_results(cached, t, t["inline_cached_title"]))
                return

            token = _new_inline_token({"mode": "spotify", "url": url, "user_id": user_id, "quality": "audio"})

            if not has_cache_channel:
                _answer_inline(query, [], InlineQueryResultsButton(t["inline_in_bot_btn"], start_parameter=f"dl_{token}"))
                return

            _answer_inline(query, [_placeholder_result(t, token, t["inline_download_title"], t["inline_download_desc"])])
            return

        # -------------- Instagram / TikTok / SoundCloud ----------------
        if platform == "tiktok":
            url = platforms.resolve_tiktok_url(url)

        quality = _inline_quality(user_id, platform)
        cached = _fresh_cache(platforms.media_cache_key(url, quality))

        if cached:
            _answer_inline(query, _cached_inline_results(cached, t, t["inline_cached_title"]))
            return

        token = _new_inline_token({"mode": "direct", "url": url, "user_id": user_id, "quality": quality})

        if not has_cache_channel:
            _answer_inline(query, [], InlineQueryResultsButton(t["inline_in_bot_btn"], start_parameter=f"dl_{token}"))
            return

        _answer_inline(query, [_placeholder_result(t, token, t["inline_download_title"], t["inline_download_desc"])])

    def _upload_to_cache_channel(cache_chat, files, metadata_source, platform):
        """Uploads files to the private cache channel and returns their
        (kind, file_id) pairs."""
        refs = []
        thumb_url = metadata_source.get("thumbnail")

        for filepath in files:
            kind = platforms.media_kind(filepath)

            if kind == "audio" and platform != "spotify":
                platforms.tag_audio_file(
                    filepath,
                    title=metadata_source.get("track") or metadata_source.get("title") or "",
                    artist=metadata_source.get("artist") or metadata_source.get("uploader") or metadata_source.get("channel") or "",
                    cover_url=None if platform == "youtube" else thumb_url,
                )

            with open(filepath, "rb") as media_file:
                if kind == "video":
                    sent = bot.send_video(
                        cache_chat, media_file,
                        supports_streaming=True,
                        width=_as_int(metadata_source.get("width")),
                        height=_as_int(metadata_source.get("height")),
                        duration=_as_int(metadata_source.get("duration")),
                        disable_notification=True,
                        timeout=600,
                    )
                elif kind == "audio":
                    thumb_path = platforms.make_thumbnail(thumb_url)
                    try:
                        thumb_file = open(thumb_path, "rb") if thumb_path else None
                        try:
                            sent = bot.send_audio(
                                cache_chat, media_file,
                                title=metadata_source.get("track") or metadata_source.get("title") or None,
                                performer=metadata_source.get("artist") or metadata_source.get("uploader") or None,
                                duration=_as_int(metadata_source.get("duration")),
                                thumbnail=thumb_file,
                                disable_notification=True,
                                timeout=600,
                            )
                        finally:
                            if thumb_file:
                                thumb_file.close()
                    finally:
                        platforms.remove_download_files([thumb_path])
                else:
                    try:
                        sent = bot.send_photo(cache_chat, media_file, disable_notification=True)
                    except Exception:
                        media_file.seek(0)
                        sent = bot.send_document(cache_chat, media_file, disable_notification=True, timeout=600)

            ref = _file_ref(sent)
            if ref:
                refs.append(ref)

        return refs

    def _inline_media(kind, file_id):
        if kind == "video":
            return InputMediaVideo(file_id, caption=BOT_SIGNATURE, supports_streaming=True)
        if kind == "photo":
            return InputMediaPhoto(file_id, caption=BOT_SIGNATURE)
        if kind == "audio":
            return InputMediaAudio(file_id, caption=BOT_SIGNATURE)
        return InputMediaDocument(file_id, caption=BOT_SIGNATURE)

    def _inline_fail(inline_message_id, t, text):
        try:
            bot.edit_message_text(
                text,
                inline_message_id=inline_message_id,
                reply_markup=_open_bot_markup(t, "inline"),
            )
        except Exception:
            pass

    @bot.chosen_inline_handler(func=lambda result: True)
    def handle_chosen_inline(result):
        if not result.result_id.startswith(("d:", "y:")):
            # A cached file was sent: nothing to download.
            store.record_inline_send()
            return

        data = _inline_tokens.get(result.result_id[2:])
        inline_message_id = result.inline_message_id
        user_id = result.from_user.id
        t = _texts_for(user_id)

        if not data or not inline_message_id:
            return

        if result.result_id.startswith("y:"):
            _show_inline_youtube_picker(result.result_id[2:], data, inline_message_id, result.from_user, t)
            return

        _deliver_inline(data, inline_message_id, user_id, t)

    def _show_inline_youtube_picker(token, data, inline_message_id, user, t):
        """Turns the sent "⏳" message into the quality picker, in the chat
        where it was sent. Only the sender's taps are accepted."""
        lang = store.get_user(user_settings, user.id).get("lang", store.DEFAULT_LANGUAGE)

        try:
            probe = platforms.probe_youtube_qualities(data["url"])
        except Exception as e:
            store.record_error(platform="youtube", url=data["url"], user_id=user.id, error=f"[inline] {e}")
            _inline_fail(inline_message_id, t, _friendly_download_error("youtube", e, t))
            return

        if not probe.get("options"):
            _inline_fail(inline_message_id, t, t["yt_no_quality"])
            return

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(*[
            InlineKeyboardButton(
                _quality_option_label(opt, lang),
                callback_data=f"iq_{token}_{'audio' if opt['kind'] == 'audio' else opt.get('height')}",
            )
            for opt in probe["options"]
        ])

        name = getattr(user, "first_name", None) or (
            f"@{user.username}" if getattr(user, "username", None) else str(user.id)
        )
        text = (
            t["yt_choose_quality"].format(title=(probe.get("title") or "")[:200])
            + "\n\n"
            + t["yt_pick_for"].format(name=name)
        )

        data["inline_message_id"] = inline_message_id

        try:
            bot.edit_message_text(text, inline_message_id=inline_message_id, reply_markup=markup)
        except Exception:
            logger.warning("Could not show the inline YouTube picker", exc_info=True)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("iq_"))
    def handle_inline_quality_pick(call):
        token, _, choice = call.data[3:].rpartition("_")
        data = _inline_tokens.get(token)
        clicker_t = _texts_for(call.from_user.id)

        if not data or not call.inline_message_id:
            bot.answer_callback_query(call.id, clicker_t["inline_expired"], show_alert=True)
            return

        if call.from_user.id != data["user_id"]:
            # Everyone in the chat sees the buttons; only the sender counts.
            bot.answer_callback_query(call.id, clicker_t["yt_not_yours"], show_alert=True)
            return

        if data.get("picked"):
            bot.answer_callback_query(call.id)
            return

        data["picked"] = True
        bot.answer_callback_query(call.id)

        t = _texts_for(data["user_id"])

        try:
            bot.edit_message_text(
                t["inline_placeholder"],
                inline_message_id=call.inline_message_id,
                reply_markup=_inline_markup(t),
            )
        except Exception:
            pass

        _deliver_inline(
            dict(data, choice=choice),
            call.inline_message_id,
            data["user_id"],
            t,
        )

    def _deliver_inline(data, inline_message_id, user_id, t):
        """Downloads the media for an inline message, uploads it to the cache
        channel and turns the inline message into the file."""
        cache_chat = _cache_chat_id()

        if cache_chat is None:
            _inline_fail(inline_message_id, t, t["download_failed"])
            return

        if _is_rate_limited(user_id):
            _inline_fail(inline_message_id, t, t["rate_limited"].format(limit=RATE_LIMIT_COUNT))
            return

        mode = data["mode"]
        url = data["url"]
        platform = platforms.detect_platform(url) or "unknown"
        quality = data.get("choice") if mode == "youtube" else data.get("quality", "best")
        cache_key = platforms.media_cache_key(url, quality)

        slot = _acquire_download_slot(bot, user_id, None, t, show_ui=False, platform=platform)

        if slot is None:
            _inline_fail(inline_message_id, t, t["server_busy"])
            return

        files = []

        try:
            with _cache_key_lock(cache_key) if cache_key else contextlib.nullcontext():
                cached = _fresh_cache(cache_key)

                if cached:
                    refs = cached["items"]
                    recent_title = cached.get("title") or platform.title()
                else:
                    if mode == "youtube":
                        info, entries, files = platforms.download_youtube_quality(data["video_id"], data["choice"])
                    elif mode == "spotify":
                        track = platforms.resolve_spotify_tracks(url)[0]
                        files = [platforms.download_spotify_track(track)]
                        info = {
                            "id": cache_key or url,
                            "title": track["name"],
                            "track": track["name"],
                            "artist": track["artists"],
                            "uploader": track["artists"],
                            "album": track.get("album"),
                            "thumbnail": track.get("cover_url"),
                            "duration": (track.get("duration_ms") or 0) / 1000,
                            "extractor_key": "Spotify",
                        }
                        entries = [info]
                    else:
                        info, entries, files, quality = platforms.download_direct(url, quality=quality)

                    metadata_source = dict(info or {})
                    for key, value in (entries[0] if entries else {}).items():
                        if value not in (None, "", [], {}):
                            metadata_source[key] = value

                    refs = _upload_to_cache_channel(cache_chat, files, metadata_source, platform)

                    if not refs:
                        raise Exception("Nothing could be uploaded to the cache channel.")

                    if cache_key:
                        media_cache[cache_key] = {
                            "time": time.time(),
                            "items": refs,
                            "caption": _build_caption(metadata_source, url),
                            "post_id": str(metadata_source.get("id") or int(time.time() * 1000))[:48],
                            "thumb_url": metadata_source.get("thumbnail"),
                            "offer_audio": any(kind == "video" for kind, _ in refs) and quality != "audio",
                            "title": _recent_title(metadata_source, platform),
                        }

                    recent_title = _recent_title(metadata_source, platform)

            more_token = None
            if len(refs) > 1:
                more_token = _new_inline_token({"mode": mode, "url": url, "user_id": user_id, "quality": quality})

            kind, file_id = refs[0]
            bot.edit_message_media(
                _inline_media(kind, file_id),
                inline_message_id=inline_message_id,
                reply_markup=_inline_markup(t, more_token),
            )

            store.record_download(platform, user_id)
            store.record_inline_send()
            store.add_recent(user_id, cache_key or url, recent_title, refs)

        except Exception as e:
            if not isinstance(e, platforms.UnsupportedLinkError):
                store.record_error(platform=platform, url=url, user_id=user_id, error=f"[inline] {e}")
                logger.exception("Inline download failed | url=%s", url)

            if isinstance(e, platforms.FileTooLargeError):
                _inline_fail(inline_message_id, t, t["too_large"].format(size=str(e)))
            else:
                _inline_fail(inline_message_id, t, _friendly_download_error(platform, e, t))

        finally:
            platforms.remove_download_files(files)
            slot.release()
