import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo, InputMediaAudio
import admin
import ads
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

thumb_cache = {}
audio_source_cache = {}  # post_id -> original url, for the "get audio" button under video posts
user_settings = store.load_user_settings()

# --- rate limiting + concurrency cap ---
RATE_LIMIT_COUNT = 5          # max downloads...
RATE_LIMIT_WINDOW = 60        # ...per this many seconds, per user
MAX_CONCURRENT_DOWNLOADS = 1  # how many downloads run at once, bot-wide

_recent_downloads = defaultdict(list)  # user_id -> [timestamps]
_download_semaphore = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# If this many requests are already waiting for a download slot, new ones
# get turned away immediately instead of growing an unbounded queue — keeps
# a burst of traffic (organic growth or abuse) from piling up memory and
# giving everyone a worse wait.
MAX_QUEUE_WAITING = 15
_queue_waiting_count = 0
_queue_lock = threading.Lock()


def _acquire_download_slot(bot, chat_id_int, status_msg, t) -> bool:
    """Returns True once a download slot is acquired. Returns False (and
    already told the user) if the wait queue is already too deep — caller
    should stop immediately without touching the semaphore."""
    global _queue_waiting_count

    if _download_semaphore.acquire(blocking=False):
        return True

    with _queue_lock:
        if _queue_waiting_count >= MAX_QUEUE_WAITING:
            bot.edit_message_text(t['server_busy'], chat_id_int, status_msg.message_id)
            return False
        _queue_waiting_count += 1

    bot.edit_message_text(t['queued'], chat_id_int, status_msg.message_id)
    _download_semaphore.acquire()

    with _queue_lock:
        _queue_waiting_count -= 1
    return True


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


TEXTS = {
    'fa': {
        'welcome': "🦆 **به DuckLoader خوش اومدی!**\n\nمن اردک دانلودچیِ توام — لینک رو بفرست، می‌رم پیداش می‌کنم و برات برمی‌گردونمش. 🎒\n\nاز Instagram، SoundCloud، Spotify و YouTube پشتیبانی می‌کنم.\n\nکیفیت دلخواهت رو از /settings انتخاب کن.\n\n🦆 اگه لینک داشته باشی، منم یه راه برای آوردنش پیدا می‌کنم!",
        'init': "⏳ در حال برقراری ارتباط...",
        'downloading': "🔄 **در حال دانلود** {bar} {percent}\n\n📦 حجم: {size}\n⏱ زمان: {eta}",
        'uploading': "✅ دانلود تکمیل شد! در حال آپلود...",
        'failed': "❌ خطا: {error}",
        'download_failed': "❌ دانلود این لینک در حال حاضر انجام نشد. لطفاً مطمئن شو لینک قابل دسترسیه و دوباره امتحان کن.",
        'instagram_failed': "❌ دریافت محتوای اینستاگرام انجام نشد. لطفاً لینک رو بررسی کن و دوباره امتحان کن.",
        'instagram_unavailable': "❌ این استوری اینستاگرام در حال حاضر برای ربات قابل دسترسی نیست. ممکنه نیاز به ورود به اینستاگرام داشته باشه یا استوری دیگه در دسترس نباشه.",
        'youtube_failed': "❌ دریافت این ویدیوی یوتیوب در حال حاضر انجام نشد. لطفاً چند لحظه بعد دوباره امتحان کن.",
        'soundcloud_failed': "❌ دریافت این ترک ساندکلاد در حال حاضر انجام نشد. لطفاً لینک رو بررسی کن و دوباره امتحان کن.",
        'spotify_failed': "❌ دریافت این ترک اسپاتیفای در حال حاضر انجام نشد. لطفاً چند لحظه بعد دوباره امتحان کن.",
        'not_launched': "🚧 دانلود از {platform} هنوز لانچ نشده. به‌زودی فعال می‌شود!",
        'too_large': "⚠️ حجم این فایل حدود {size} است و از سقف مجاز بیشتره، پس امکان ارسالش نیست.\n\nمی‌تونی از /settings کیفیت پایین‌تر یا «فقط صدا» رو انتخاب کنی.",
        'quality_reduced': "ℹ️ به‌خاطر محدودیت حجم تلگرام، کیفیت به‌صورت خودکار به «{quality}» کاهش یافت.",
        'rate_limited': "⏳ توی یک دقیقه‌ی اخیر بیش از حد مجاز ({limit} تا) دانلود کرده‌ای. کمی صبر کن و دوباره امتحان کن.",
        'queued': "📋 صف دانلود پر است — به‌محض آزاد شدن ظرفیت شروع می‌شود...",
        'server_busy': "🚦 سرور الان خیلی شلوغه. چند دقیقه‌ی دیگه دوباره امتحان کن.",
        'spotify_searching': "🔎 در حال جست‌وجو ({i}/{total}): {name}",
        'view_link': "🔗 مشاهده در پلتفرم اصلی",
        'dl_cover': "🖼 دانلود کاور",
        'get_audio_btn': "🎵 دریافت صدا",
        'audio_expired': "⚠️ این دکمه دیگه معتبر نیست (بات ری‌استارت شده). لینک رو دوباره بفرست.",
        'cover_loading': "⏳ در حال دریافت کاور...",
        'cover_error': "⚠️ کاور این پست یافت نشد.",
        'settings_msg': "⚙️ **تنظیمات ربات**\n\nزبان و کیفیت دانلود دلخواهت رو انتخاب کن:",
        'quality_best': "🎬 بهترین",
        'quality_720p': "📱 ۷۲۰p",
        'quality_audio': "🎵 فقط صدا",
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
        'broadcast_usage': "استفاده: /broadcast <پیام>",
        'broadcast_done': "✅ به {sent} کاربر ارسال شد ({failed} ناموفق).",
        'ban_usage': "استفاده: /{cmd} <user_id>",
        'ban_done': "🚫 کاربر {id} مسدود شد.",
        'unban_done': "✅ کاربر {id} از مسدودیت خارج شد.",
        'unban_not_found': "این کاربر مسدود نبود.",
        'setad_done': "✅ متن تبلیغ ذخیره شد. با /toggle sponsor_message نمایشش رو روشن/خاموش کن.",
        'setad_cleared': "متن تبلیغ خالی شد (چیزی نمایش داده نمی‌شه).",
        'addsponsor_usage': "استفاده: /addsponsor <@یوزرنیم> <نام نمایشی>",
        'addsponsor_done': "✅ {name} به لیست کانال‌های حامی اضافه شد.",
        'addsponsor_reminder': "⚠️ یادت نره بات رو ادمین همون کانال کن، وگرنه نمی‌تونه عضویت رو چک کنه.",
        'removesponsor_usage': "استفاده: /removesponsor <@یوزرنیم>",
        'removesponsor_done': "✅ حذف شد.",
        'removesponsor_not_found': "همچین کانالی توی لیست نبود.",
        'sponsors_empty': "لیست کانال‌های حامی خالیه — یعنی قفل عضویت برای هیچ‌کس فعال نیست.",
        'sponsor_gate_title': "🔸 برای استفاده‌ی رایگان از این بات، لطفاً عضو کانال(های) حامی زیر شو (هزینه‌ی نگهداری بات رو تأمین می‌کنن):",
        'sponsor_gate_join': "➕ عضویت در {name}",
        'sponsor_gate_retry': "بعد از عضویت، همون لینک رو دوباره بفرست.",
        'yt_choose_quality': "🎬 {title}\n\nکیفیت مورد نظر رو انتخاب کن:",
        'yt_no_quality': "⚠️ متأسفانه هیچ کیفیتی از این ویدیو زیر سقف مجاز نیست.",
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
        'back': "⬅️ بازگشت",
        'adm_platforms_title': "کدوم پلتفرم رو می‌خوای قفل/باز کنی؟",
        'adm_toggles_title': "کدوم تنظیم رو می‌خوای روشن/خاموش کنی؟",
        'adm_ads_title': "بخش تبلیغات:",
        'adm_users_title': "مدیریت کاربران:",
        'adm_setad_btn': "✏️ تنظیم متن تبلیغ",
        'adm_addsponsor_btn': "➕ افزودن کانال حامی",
        'adm_removesponsor_btn': "➖ حذف کانال حامی",
        'adm_sponsorlist_btn': "📋 لیست کانال‌های حامی",
        'adm_ban_btn': "🚫 مسدودکردن کاربر",
        'adm_unban_btn': "✅ رفع مسدودی کاربر",
        'adm_broadcast_btn': "📣 پیام همگانی",
        'adm_ask_setad': "متن تبلیغ جدید رو بفرست:",
        'adm_ask_addsponsor': "به این شکل بفرست: @یوزرنیم نام نمایشی",
        'adm_ask_removesponsor': "یوزرنیم کانالی که می‌خوای حذف کنی رو بفرست (با @):",
        'adm_ask_ban': "آی‌دی عددی کاربری که می‌خوای مسدود کنی رو بفرست:",
        'adm_ask_unban': "آی‌دی عددی کاربری که می‌خوای رفع مسدودیت کنی رو بفرست:",
        'adm_ask_broadcast': "متنی که می‌خوای برای همه‌ی کاربرها ارسال بشه رو بفرست:",
        'adm_cancelled': "لغو شد.",
    },
    'en': {
        'welcome': "🦆 **Welcome to DuckLoader!**\n\nI’m your little download duck. Send me a link and I’ll waddle off, fetch it, and bring it back to you. 🎒\n\nI support Instagram, SoundCloud, Spotify, and YouTube.\n\nChoose your preferred quality in /settings.\n\n🦆 You bring the link. I’ll bring the media.",
        'init': "⏳ Initializing connection...",
        'downloading': "🔄 **Downloading** {bar} {percent}\n\n📦 Size: {size}\n⏱ ETA: {eta}",
        'uploading': "✅ Download complete! Preparing upload...",
        'failed': "❌ Failed: {error}",
        'download_failed': "❌ We couldn't download this link right now. Please check the link and try again.",
        'instagram_failed': "❌ We couldn't download this Instagram content right now. Please check the link and try again.",
        'instagram_unavailable': "❌ This Instagram story isn't currently accessible to the bot. It may require an Instagram login or may no longer be available.",
        'youtube_failed': "❌ We couldn't download this YouTube video right now. Please try again in a moment.",
        'soundcloud_failed': "❌ We couldn't download this SoundCloud track right now. Please check the link and try again.",
        'spotify_failed': "❌ We couldn't download this Spotify track right now. Please try again in a moment.",
        'not_launched': "🚧 Downloading from {platform} hasn't launched yet. Stay tuned!",
        'too_large': "⚠️ This file is about {size}, over the allowed limit, so it can't be sent.\n\nYou can pick a lower quality or \"audio only\" in /settings.",
        'quality_reduced': "ℹ️ Quality was automatically reduced to \"{quality}\" to stay under Telegram's size limit.",
        'rate_limited': "⏳ You've hit the download limit ({limit}) for the last minute. Please wait a bit and try again.",
        'queued': "📋 The download queue is full — this will start as soon as a slot frees up...",
        'server_busy': "🚦 The server is very busy right now. Please try again in a few minutes.",
        'spotify_searching': "🔎 Searching ({i}/{total}): {name}",
        'view_link': "🔗 View Original",
        'dl_cover': "🖼 Download Cover",
        'get_audio_btn': "🎵 Get Audio",
        'audio_expired': "⚠️ This button is no longer valid (the bot restarted). Please resend the link.",
        'cover_loading': "⏳ Fetching cover...",
        'cover_error': "⚠️ Cover not found.",
        'settings_msg': "⚙️ **Bot Settings**\n\nChoose your language and preferred download quality:",
        'quality_best': "🎬 Best",
        'quality_720p': "📱 720p",
        'quality_audio': "🎵 Audio only",
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
        'sponsor_gate_title': "🔸 To use this bot for free, please join our sponsor channel(s) below (they help cover the cost of running it):",
        'sponsor_gate_join': "➕ Join {name}",
        'sponsor_gate_retry': "After joining, just resend your link.",
        'yt_choose_quality': "🎬 {title}\n\nChoose a quality:",
        'yt_no_quality': "⚠️ Unfortunately no quality of this video is under the allowed size limit.",
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
        'adm_ask_setad': "Send the new ad text:",
        'adm_ask_addsponsor': "Send it like this: @username Display Name",
        'adm_ask_removesponsor': "Send the channel's username to remove (with @):",
        'adm_ask_ban': "Send the numeric user ID to ban:",
        'adm_ask_unban': "Send the numeric user ID to unban:",
        'adm_ask_broadcast': "Send the message to broadcast to all users:",
        'adm_cancelled': "Cancelled.",
    }
}


def _texts_for(chat_id) -> dict:
    user = store.get_user(user_settings, chat_id)
    return TEXTS[user['lang']]

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

    views = (
        entry.get("view_count")
        or 0
    )

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
    )

    max_length = 750

    if len(description) > max_length:
        description = (
            description[:max_length]
            + "..."
        )

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

    lines.append(
        stats_line
    )

    if description:
        lines.append(
            f"\n📝 {description}"
        )

    # KEEP YOUR CHOSEN BOT USERNAME.
    lines.append(
        "\n🦆 Downloaded with @DuckDownloader_Bot"
    )

    return "\n".join(lines)

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

    error_text = str(error).lower()

    if platform == "instagram":

        if (
            "login" in error_text
            or "cookies" in error_text
            or "authentication" in error_text
            or "unreachable" in error_text
        ):
            return t[
                "instagram_unavailable"
            ]

        return t[
            "instagram_failed"
        ]

    if platform == "youtube":

        return t[
            "youtube_failed"
        ]

    if platform == "soundcloud":

        return t[
            "soundcloud_failed"
        ]

    if platform == "spotify":

        return t[
            "spotify_failed"
        ]

    return t[
        "download_failed"
    ]

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

def _settings_markup(user: dict, t: dict) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    fa_text = "🇮🇷 فارسی ✅" if user['lang'] == 'fa' else "🇮🇷 فارسی"
    en_text = "🇺🇸 English ✅" if user['lang'] == 'en' else "🇺🇸 English"
    markup.add(
        InlineKeyboardButton(text=fa_text, callback_data="lang_fa"),
        InlineKeyboardButton(text=en_text, callback_data="lang_en"),
    )

    quality_buttons = []
    for key in ("best", "720p", "audio"):
        label = t[f'quality_{key}']
        text = f"{label} ✅" if user['quality'] == key else label
        quality_buttons.append(InlineKeyboardButton(text=text, callback_data=f"quality_{key}"))
    markup.row(*quality_buttons)
    return markup


def register_features(bot):
    flags = store.load_flags()

    def _my_settings_view(chat_id):
        user = store.get_user(user_settings, chat_id)
        t = TEXTS[user['lang']]
        return t['settings_msg'], _settings_markup(user, t)

    admin.register_admin(bot, flags, _texts_for, _my_settings_view)

    def _maybe_send_ad(chat_id_int):
        if not flags.get('sponsor_message', False):
            return
        ad_text = ads.load_ad_message()
        if ad_text:
            try:
                bot.send_message(chat_id_int, ad_text, parse_mode="Markdown")
            except Exception:
                pass

    def _make_progress_hook(chat_id_int, status_msg, t):
        """Shared by every download path (direct, spotify, YouTube quality
        picker) so there's one place that decides how often to update the
        status message and what it looks like."""
        state = {"last_edit": 0}

        def progress_hook(d):
            if d.get('status') == 'downloading':
                now = time.time()
                if now - state["last_edit"] > 2.0:
                    percent = (d.get('_percent_str') or 'N/A').strip()
                    eta = (d.get('_eta_str') or 'N/A').strip()
                    size = d.get('_total_bytes_str') or d.get('_estimated_total_bytes_str', 'N/A')
                    if isinstance(size, str):
                        size = size.strip()
                    log_text = t['downloading'].format(bar=_render_bar(percent), percent=percent, size=size, eta=eta)
                    try:
                        bot.edit_message_text(log_text, chat_id_int, status_msg.message_id, parse_mode="Markdown")
                    except Exception:
                        pass
                    state["last_edit"] = now

        return progress_hook

    def _send_download_result(chat_id_int, reply_to_id, url, platform, quality_requested, quality_used, info, entries, files, t):
        """Builds the caption/buttons and sends the downloaded file(s) —
        one file directly, or a chunked media group for multi-item posts
        (Instagram carousels). Adds the "get audio" button when the result
        includes a video and the user didn't already request audio-only.
        Shared by the direct-download flow and the 'get audio' button."""
        if quality_used != quality_requested:
            bot.send_message(chat_id_int, t['quality_reduced'].format(quality=t[f'quality_{quality_used}']))

        # Merge the parent result with the individual media entry.
        #
        # The parent result can contain the Instagram username ("channel")
        # even when the individual Reel/Story media entry doesn't.
        metadata_source = dict(
            info or {}
        )

        if entries:
            metadata_source.update(
                entries[0] or {}
            )

        caption = _build_caption(
            metadata_source,
            url,
        )

        thumb_url = (
            metadata_source.get("thumbnail")
        )
        post_id = metadata_source.get('id', str(time.time()))
        if thumb_url:
            thumb_cache[post_id] = thumb_url

        valid_files = [f for f in files if os.path.exists(f)]
        any_video = any(platforms.media_kind(f) == 'video' for f in valid_files)
        offer_audio_button = any_video and quality_requested != 'audio'
        if offer_audio_button:
            audio_source_cache[post_id] = url

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(text=t['view_link'], url=url))
        if thumb_url:
            markup.add(InlineKeyboardButton(text=t['dl_cover'], callback_data=f"thumb_{post_id}"))
        if offer_audio_button:
            markup.add(InlineKeyboardButton(text=t['get_audio_btn'], callback_data=f"audio_{post_id}"))

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
                        cover_url=thumb_url,
                    )
                
            bot.send_chat_action(chat_id_int, 'upload_video' if kind == 'video' else 'upload_audio' if kind == 'audio' else 'upload_photo')

            try:
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

                        bot.send_video(
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

                        bot.send_audio(
                            chat_id_int,
                            media_file,

                            # Telegram audio metadata
                            duration=audio_duration,
                            title=audio_title,
                            performer=audio_performer,

                            # YouTube thumbnail as Telegram's audio cover.
                            thumbnail=thumb_url if thumb_url else None,

                            caption=caption,
                            reply_markup=markup,
                            reply_to_message_id=reply_to_id,
                            timeout=600,
                        )
                    else:
                        bot.send_photo(chat_id_int, media_file, caption=caption, reply_markup=markup, reply_to_message_id=reply_to_id)
            finally:
                if os.path.exists(filepath):
                    os.remove(filepath)

        elif len(valid_files) > 1:
            chunks = [valid_files[idx:idx + 10] for idx in range(0, len(valid_files), 10)]

            for chunk_idx, chunk in enumerate(chunks):
                media_group = []
                open_files = []

                try:
                    for item_idx, filepath in enumerate(chunk):
                        kind = platforms.media_kind(filepath)
                        f = open(filepath, "rb")
                        open_files.append(f)

                        item_caption = caption if chunk_idx == 0 and item_idx == 0 else ""

                        if kind == "video":
                            media_group.append(InputMediaVideo(f, caption=item_caption))
                        elif kind == "audio":
                            platforms.tag_audio_file(filepath, title=metadata_source.get('title', ''), artist=metadata_source.get('uploader') or metadata_source.get('channel', ''), cover_url=thumb_url)
                            media_group.append(InputMediaAudio(f, caption=item_caption))
                        else:
                            media_group.append(InputMediaPhoto(f, caption=item_caption))

                    bot.send_chat_action(chat_id_int, 'upload_document')
                    bot.send_media_group(chat_id_int, media_group, reply_to_message_id=reply_to_id if chunk_idx == 0 else None, timeout=600)
                finally:
                    for f in open_files:
                        f.close()

            for filepath in valid_files:
                if os.path.exists(filepath):
                    os.remove(filepath)

        store.record_download(platform)

    def _run_direct_download(
        chat_id_int,
        reply_to_id,
        url,
        quality,
        t,
        status_msg,
    ):
        """
        Shared direct-download worker.

        Technical errors are stored for the administrator and
        are never shown directly to the user.
        """

        progress_hook = _make_progress_hook(
            chat_id_int,
            status_msg,
            t,
        )

        if not _acquire_download_slot(
            bot,
            chat_id_int,
            status_msg,
            t,
        ):
            return

        platform = (
            platforms.detect_platform(url)
            or "unknown"
        )

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
                progress_hook=progress_hook,
            )

            bot.edit_message_text(
                t["uploading"],
                chat_id_int,
                status_msg.message_id,
            )

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
            )

            bot.delete_message(
                chat_id_int,
                status_msg.message_id,
            )

            _maybe_send_ad(
                chat_id_int
            )

        except platforms.FileTooLargeError as e:

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

        except Exception as e:

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

        finally:

            _download_semaphore.release()

    def _send_youtube_quality_picker(
        message,
        t,
        lang,
    ):
        chat_id_int = message.chat.id
        url = message.text.strip()

        status_msg = bot.reply_to(
            message,
            t["init"],
        )

        try:
            probe = platforms.probe_youtube_qualities(
                url
            )

        except Exception as e:
            store.record_error(
                platform="youtube",
                url=url,
                user_id=message.from_user.id,
                error=str(e),
            )

            logger.exception(
                "YouTube quality probe failed | url=%s",
                url,
            )

            bot.edit_message_text(
                t["youtube_failed"],
                chat_id_int,
                status_msg.message_id,
            )

            return

        if not probe.get("options"):
            bot.edit_message_text(
                t["yt_no_quality"],
                chat_id_int,
                status_msg.message_id,
            )
            return

        digits = str.maketrans(
            "0123456789",
            "۰۱۲۳۴۵۶۷۸۹",
        )

        markup = InlineKeyboardMarkup(
            row_width=2
        )

        buttons = []

        for opt in probe["options"]:

            if opt.get("size_bytes", 0) <= 0:
                size_label = (
                    "Unknown Size"
                    if lang == "en"
                    else "حجم نامشخص"
                )

            else:
                size_label = (
                    f"{round(opt['size_bytes'] / 1024 / 1024)} MB"
                )

                if lang == "fa":
                    size_label = (
                        size_label
                        .replace(
                            "MB",
                            "مگابایت",
                        )
                        .translate(digits)
                    )

            if opt["kind"] == "audio":

                text = (
                    f"🎵 Audio — {size_label}"
                    if lang == "en"
                    else f"🎵 صوت — {size_label}"
                )

                callback_data = (
                    f"ytq_{probe['id']}_audio"
                )

            else:

                height = opt.get("height")
                height_str = str(height)

                if lang == "fa":
                    height_str = (
                        height_str.translate(
                            digits
                        )
                    )

                text = (
                    f"🎬 {height_str}p — {size_label}"
                )

                callback_data = (
                    f"ytq_{probe['id']}_{height}"
                )

            buttons.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=callback_data,
                )
            )

        markup.add(*buttons)

        try:
            bot.delete_message(
                chat_id_int,
                status_msg.message_id,
            )
        except Exception:
            pass

        caption = (
            t["yt_choose_quality"].format(
                title=(
                    probe.get("title")
                    or ""
                )[:200]
            )
        )

        if probe.get("thumbnail"):

            bot.send_photo(
                chat_id_int,
                probe["thumbnail"],
                caption=caption,
                reply_markup=markup,
                reply_to_message_id=(
                    message.message_id
                ),
            )

        else:

            bot.send_message(
                chat_id_int,
                caption,
                reply_markup=markup,
                reply_to_message_id=(
                    message.message_id
                ),
            )

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

        user = store.get_user(
            user_settings,
            chat_id,
        )

        # New users default to English.
        # Existing users keep their saved language.
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

        user_settings[chat_id] = user

        store.save_user_settings(
            user_settings
        )

        t = TEXTS[language]

        bot.reply_to(
            message,
            t["welcome"],
            reply_markup=_start_language_markup(),
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

        bot.reply_to(
            message,
            t["welcome"],
            parse_mode="Markdown",
        )

    @bot.message_handler(commands=["whoami"])
    def whoami(message):
        bot.reply_to(message, f"🆔 `{message.from_user.id}`", parse_mode="Markdown")

    @bot.message_handler(commands=["settings"])
    def open_settings(message):
        if admin.is_owner(message.from_user.id):
            text, markup = admin.build_panel(_texts_for(message.chat.id))
            bot.reply_to(message, text, reply_markup=markup, parse_mode="Markdown")
            return

        user = store.get_user(user_settings, message.chat.id)
        t = TEXTS[user['lang']]
        bot.reply_to(message, t['settings_msg'], reply_markup=_settings_markup(user, t), parse_mode="Markdown")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('lang_') or call.data.startswith('quality_'))
    def handle_settings_callback(call):
        chat_id = str(call.message.chat.id)
        user = store.get_user(user_settings, chat_id)

        if call.data.startswith('lang_'):
            user['lang'] = call.data.split('_', 1)[1]
        else:
            user['quality'] = call.data.split('_', 1)[1]

        user_settings[chat_id] = user
        store.save_user_settings(user_settings)

        t = TEXTS[user['lang']]
        bot.edit_message_text(
            t['settings_msg'], int(chat_id), call.message.message_id,
            reply_markup=_settings_markup(user, t), parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)

    @bot.message_handler(func=lambda msg: bool(msg.text) and platforms.detect_platform(msg.text) is not None)
    def handle_media_link(message):
        chat_id_str = str(message.chat.id)
        chat_id_int = message.chat.id
        user_id = message.from_user.id

        if store.is_banned(user_id):
            return  # silently ignore banned users

        store.track_user(chat_id_int)
        user = store.get_user(user_settings, chat_id_str)
        t = TEXTS[user['lang']]

        unjoined = ads.get_unjoined_channels(bot, user_id)
        if unjoined:
            markup = InlineKeyboardMarkup()
            for ch in unjoined:
                markup.add(InlineKeyboardButton(text=t['sponsor_gate_join'].format(name=ch['name']), url=f"https://t.me/{ch['username'].lstrip('@')}"))
            bot.reply_to(message, f"{t['sponsor_gate_title']}\n\n{t['sponsor_gate_retry']}", reply_markup=markup)
            return

        url = message.text.strip()
        platform = platforms.detect_platform(url)

        if not flags.get(platform, True):
            bot.reply_to(message, t['not_launched'].format(platform=platforms.PLATFORM_NAMES[platform]))
            return

        # YouTube gets the thumbnail+buttons quality picker EXCEPT when the
        # user has already told us (via /settings) that they always want
        # audio only — in that case there's nothing to pick, so it goes
        # through the same direct-download path as every other platform.
        if platform == "youtube" and user['quality'] != 'audio':
            _send_youtube_quality_picker(message, t, user['lang'])
            return

        if _is_rate_limited(user_id):
            bot.reply_to(message, t['rate_limited'].format(limit=RATE_LIMIT_COUNT))
            return

        status_msg = bot.reply_to(message, t['init'])
        bot.send_chat_action(chat_id_int, 'typing')

        if platform == "spotify":
            progress_hook = _make_progress_hook(chat_id_int, status_msg, t)

            if not _acquire_download_slot(bot, chat_id_int, status_msg, t):
                return

            try:
                tracks = platforms.resolve_spotify_tracks(url)
                for i, track in enumerate(tracks):
                    if len(tracks) > 1:
                        bot.edit_message_text(
                            t['spotify_searching'].format(i=i + 1, total=len(tracks), name=f"{track['artists']} - {track['name']}"),
                            chat_id_int, status_msg.message_id,
                        )
                    filepath = platforms.download_spotify_track(track, progress_hook)
                    try:
                        bot.send_chat_action(chat_id_int, 'upload_audio')
                        caption = (
                            f"💿 {track['album']}\n\n"
                            f"🦆 @DuckLoaderBot"
                            if i == 0
                            else ""
                        )
                        with open(filepath, "rb") as audio_file:
                            bot.send_audio(
                                chat_id_int, audio_file,
                                title=track['name'], performer=track['artists'],
                                caption=caption, reply_to_message_id=message.message_id, timeout=600,
                            )
                    finally:
                        if os.path.exists(filepath):
                            os.remove(filepath)
                    store.record_download("spotify")
                bot.delete_message(chat_id_int, status_msg.message_id)
                _maybe_send_ad(chat_id_int)
            except Exception as e:

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
                    bot.edit_message_text(
                        t["spotify_failed"],
                        chat_id_int,
                        status_msg.message_id,
                    )
                except Exception:
                    pass
            finally:
                _download_semaphore.release()
            return

        _run_direct_download(chat_id_int, message.message_id, url, user['quality'], t, status_msg)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('thumb_'))
    def handle_thumbnail_callback(call):
        chat_id = str(call.message.chat.id)
        t = _texts_for(chat_id)

        post_id = call.data.split('thumb_')[1]
        thumb_url = thumb_cache.get(post_id)

        if thumb_url:
            bot.answer_callback_query(call.id, t['cover_loading'])
            bot.send_photo(int(chat_id), thumb_url, reply_to_message_id=call.message.message_id)
        else:
            bot.answer_callback_query(call.id, t['cover_error'], show_alert=True)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('audio_'))
    def handle_get_audio_callback(call):
        chat_id_str = str(call.message.chat.id)
        chat_id_int = call.message.chat.id
        user_id = call.from_user.id
        t = _texts_for(chat_id_str)

        if store.is_banned(user_id):
            bot.answer_callback_query(call.id)
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
        status_msg = bot.send_message(chat_id_int, t['init'])
        _run_direct_download(chat_id_int, None, url, 'audio', t, status_msg)

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

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith("ytq_")
    )
    def handle_youtube_quality_pick(call):
        chat_id_str = str(
            call.message.chat.id
        )
        chat_id_int = call.message.chat.id
        user_id = call.from_user.id

        t = _texts_for(
            chat_id_str
        )

        if store.is_banned(user_id):
            bot.answer_callback_query(
                call.id
            )
            return

        if _is_rate_limited(user_id):
            bot.answer_callback_query(
                call.id,
                t["rate_limited"].format(
                    limit=RATE_LIMIT_COUNT
                ),
                show_alert=True,
            )
            return

        try:
            _, video_id, choice = (
                call.data.split("_", 2)
            )
        except ValueError:
            bot.answer_callback_query(
                call.id,
                "Invalid selection.",
                show_alert=True,
            )
            return

        bot.answer_callback_query(
            call.id
        )

        try:
            bot.delete_message(
                chat_id_int,
                call.message.message_id,
            )
        except Exception:
            pass

        status_msg = bot.send_message(
            chat_id_int,
            t["init"],
        )

        progress_hook = _make_progress_hook(
            chat_id_int,
            status_msg,
            t,
        )

        if not _acquire_download_slot(
            bot,
            chat_id_int,
            status_msg,
            t,
        ):
            return

        try:

            info, entries, files = (
                platforms.download_youtube_quality(
                    video_id,
                    choice,
                    progress_hook,
                )
            )

            bot.edit_message_text(
                t["uploading"],
                chat_id_int,
                status_msg.message_id,
            )

            source_url = (
                f"https://www.youtube.com/watch?v={video_id}"
            )

            _send_download_result(
                chat_id_int,
                None,
                source_url,
                "youtube",
                choice,
                choice,
                info,
                entries,
                files,
                t,
            )

            try:
                bot.delete_message(
                    chat_id_int,
                    status_msg.message_id,
                )
            except Exception:
                pass

            _maybe_send_ad(
                chat_id_int
            )

        except platforms.FileTooLargeError as e:

            store.record_error(
                platform="youtube",
                url=(
                    f"https://www.youtube.com/watch?v={video_id}"
                ),
                user_id=user_id,
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

        except Exception as e:

            source_url = (
                f"https://www.youtube.com/watch?v={video_id}"
            )

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

            try:
                bot.edit_message_text(
                    t["youtube_failed"],
                    chat_id_int,
                    status_msg.message_id,
                )
            except Exception:
                pass

        finally:

            _download_semaphore.release()