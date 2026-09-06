import logging
import os
import re
import random
import threading
import time
from collections import defaultdict
from datetime import datetime

from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaAudio,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
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
last_link_messages = {}
user_settings = store.load_user_settings()

# --- rate limiting + concurrency cap ---
RATE_LIMIT_COUNT = 5          # max downloads...
RATE_LIMIT_WINDOW = 60        # ...per this many seconds, per user
MAX_CONCURRENT_DOWNLOADS = 3  # how many downloads run at once, bot-wide

_recent_downloads = defaultdict(list)  # user_id -> [timestamps]
_download_semaphore = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)
AD_BUTTON_TEXT = "📣 تبلیغات در ربات"

_duck_status_messages = {}
_duck_complete_messages = {}

_duck_message_lock = threading.Lock()

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
        'welcome': (
            "🦆 **به DuckLoader خوش اومدی!**\n\n"
            "من اردک دانلودچیِ توام — لینک رو بفرست، "
            "می‌رم پیداش می‌کنم و برات برمی‌گردونمش. ⚡\n\n"
            "از Instagram، TikTok، SoundCloud، Spotify و YouTube "
            "پشتیبانی می‌کنم.\n\n"
            "کیفیت دلخواهت رو از /settings انتخاب کن.\n\n"
            "🦆 اگه لینک داشته باشی، منم یه راه برای آوردنش پیدا می‌کنم!"
        ),        'init': "⏳ در حال برقراری ارتباط...",
        'downloading': "🔄 **در حال دانلود** {bar} {percent}\n\n📦 حجم: {size}\n⏱ زمان: {eta}",
        'uploading': "✅ دانلود تکمیل شد! در حال آپلود...",
        'failed': "❌ خطا: {error}",
        'download_failed': "❌ دانلود این لینک در حال حاضر انجام نشد. لطفاً مطمئن شو لینک قابل دسترسیه و دوباره امتحان کن.",
        'instagram_failed': "❌ دریافت محتوای اینستاگرام انجام نشد. لطفاً لینک رو بررسی کن و دوباره امتحان کن.",
        'instagram_unavailable': "❌ این استوری اینستاگرام در حال حاضر برای ربات قابل دسترسی نیست. ممکنه نیاز به ورود به اینستاگرام داشته باشه یا استوری دیگه در دسترس نباشه.",
        'youtube_failed': "❌ دریافت این ویدیوی یوتیوب در حال حاضر انجام نشد. لطفاً چند لحظه بعد دوباره امتحان کن.",
        'tiktok_failed': (
            "❌ دریافت این ویدیوی TikTok در حال حاضر انجام نشد. "
            "لطفاً لینک را بررسی کنید و دوباره امتحان کنید."
        ),
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
        'sponsor_gate_title': "🔸 برای استفاده‌ی رایگان از این بات، در چنل‌های اسپانسر عضو بشید:",
        'sponsor_gate_join': "{name}",
        'sponsor_gate_retry': (
            "بعد از عضویت، روی دکمه «✅ بررسی عضویت و ادامه» بزنید "
            "تا وضعیت عضویت شما بررسی بشه و دانلود ادامه پیدا کنه."
        ),
        'sponsor_gate_check': "✅ بررسی عضویت و ادامه",
        'sponsor_gate_not_joined': (
            "❌ هنوز عضو همه کانال‌های حامی نشده‌اید.\n\n"
            "لطفاً عضو کانال‌های باقی‌مانده شوید و دوباره بررسی کنید."
        ),
        'sponsor_gate_confirmed': "✅ عضویت شما تأیید شد. دانلود شروع می‌شود.",
        'yt_choose_quality': "🎬 {title}\n\nکیفیت مورد نظر رو انتخاب کن:",
        'yt_no_quality': "⚠️ متأسفانه هیچ کیفیتی از این ویدیو زیر سقف مجاز نیست.",
        'ad_button': "📣 تبلیغات در ربات",
        'ad_channel_prompt': "📣 لطفاً آیدی، یوزرنیم یا لینک کانالی که می‌خواهید تبلیغ کنید را ارسال کنید:",
        'ad_display_name_prompt': "🏷 نام نمایشی موردنظرتان برای تبلیغ را وارد کنید:",
        'ad_type_prompt': "📌 نوع تبلیغ را انتخاب کنید:",
        'ad_type_sponsor_channel':
            "📢 کانال حامی — کاربر برای استفاده از ربات باید عضو کانال شما باشد",
        'ad_type_post_download':
            "📣 تبلیغ بعد از هر دانلود — تبلیغ شما بعد از محتوای دانلودشده نمایش داده می‌شود",
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
        'toggle_sponsor_channel_gate': "🔒 الزام عضویت در کانال‌های حامی",
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
            "/checksponsor <channel> — بررسی دسترسی بات به کانال حامی\n"
            "/addsponsor <channel> <name> — افزودن کانال حامی\n"
            "/removesponsor <channel> — حذف کانال حامی\n"
            "/sponsors — نمایش کانال‌های حامی"
        ),
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
        'adm_exempt_btn': "✅ معاف کردن از الزام عضویت",
        'adm_unexempt_btn': "❌ لغو معافیت عضویت",
        'adm_ask_setad': "متن تبلیغ جدید رو بفرست:",
        'adm_ask_addsponsor': "به این شکل بفرست: @یوزرنیم نام نمایشی",
        'adm_ask_removesponsor': "یوزرنیم کانالی که می‌خوای حذف کنی رو بفرست (با @):",
        'adm_ask_ban': "آی‌دی عددی کاربری که می‌خوای مسدود کنی رو بفرست:",
        'adm_ask_unban': "آی‌دی عددی کاربری که می‌خوای رفع مسدودیت کنی رو بفرست:",
        'adm_ask_broadcast': "متنی که می‌خوای برای همه‌ی کاربرها ارسال بشه رو بفرست:",
        'adm_ask_exempt': "آی‌دی عددی کاربری که می‌خوای از الزام عضویت در کانال‌های حامی معاف کنی رو بفرست:",
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
        'ad_type_sponsor_channel': "📢 کانال حامی",
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
            "🦆 You bring the link. I’ll bring the media."
        ),        'init': "⏳ Initializing connection...",
        'downloading': "🔄 **Downloading** {bar} {percent}\n\n📦 Size: {size}\n⏱ ETA: {eta}",
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
        'ad_type_sponsor_channel':
            "📢 Sponsor Channel — users must join your channel before downloading",
        'ad_type_post_download':
            "📣 Post-Download Ad — your advertisement is shown after every download",
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
            "/sponsors — List sponsor channels"
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

def _ad_lang_button(request):
    lang = request.get("form_lang", "fa")

    if lang == "fa":
        return InlineKeyboardButton(
            "🇺🇸 English",
            callback_data="ad_lang_en",
        )

    return InlineKeyboardButton(
        "🇮🇷 فارسی",
        callback_data="ad_lang_fa",
    )

def _ad_texts(request):
    return TEXTS.get(
        request.get("form_lang", "fa"),
        TEXTS["fa"],
    )

def _texts_for(chat_id) -> dict:
    user = store.get_user(user_settings, chat_id)
    return TEXTS[user['lang']]

def _ad_texts(request) -> dict:
    lang = request.get(
        "form_lang",
        "fa",
    )

    return TEXTS.get(
        lang,
        TEXTS["fa"],
    )

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

    if not is_instagram_story:
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

    if platform == "tiktok":

        return t[
            "tiktok_failed"
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

    def _main_reply_markup():
        current_flags = store.load_flags()

        if not current_flags.get(
            "ad_requests_button",
            False,
        ):
            return ReplyKeyboardRemove()

        markup = ReplyKeyboardMarkup(
            row_width=1,
            resize_keyboard=True,
        )

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
    ):
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
    ):
        _finish_duck_download(
            chat_id_int,
        )

        _send_duck_reaction(
            chat_id_int,
            "failed",
        )


    def _send_duck_download_complete(
        chat_id_int,
    ):
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

        approved_requests = (
            store.list_ad_requests(
                "approved"
            )
        )

        for request in approved_requests:
            if (
                request.get(
                    "ad_type"
                )
                != "post_download"
            ):
                continue

            channel = (
                request.get(
                    "channel",
                    "",
                )
                or ""
            ).strip()

            display_name = (
                request.get(
                    "display_name",
                    "",
                )
                or channel
            ).strip()

            if not channel:
                continue

            if channel.startswith(
                "https://t.me/"
            ):
                channel_url = channel
            elif channel.startswith(
                "http://t.me/"
            ):
                channel_url = channel
            elif channel.startswith(
                "t.me/"
            ):
                channel_url = (
                    "https://"
                    + channel
                )
            elif channel.startswith(
                "@"
            ):
                channel_url = (
                    "https://t.me/"
                    + channel[1:]
                )
            else:
                channel_url = ""

            try:
                if channel_url:
                    markup = InlineKeyboardMarkup()
                    markup.add(
                        InlineKeyboardButton(
                            display_name,
                            url=channel_url,
                        )
                    )

                    bot.send_message(
                        chat_id_int,
                        "📣 تبلیغ",
                        reply_markup=markup,
                    )
                else:
                    bot.send_message(
                        chat_id_int,
                        f"📣 {display_name}\n{channel}",
                    )
            except Exception:
                pass

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

        _send_duck_reaction(
            chat_id_int,
            "downloading",
            track_status=True,
        )

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

            _send_duck_download_complete(
                chat_id_int
            )

            bot.delete_message(
                chat_id_int,
                status_msg.message_id,
            )

            _maybe_send_ad(
                chat_id_int
            )

        except platforms.FileTooLargeError as e:

            _send_duck_download_failed(
                chat_id_int
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

        except Exception as e:

            _send_duck_download_failed(
                chat_id_int
            )

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

            _send_duck_download_failed(
                chat_id_int
            )

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

        user_settings[chat_id] = user

        store.save_user_settings(
            user_settings
        )

        t = TEXTS[language]

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
                reply_markup=_main_reply_markup(),
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
            reply_markup=_main_reply_markup(),
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
                call.id,
                "درخواست پیدا نشد.",
                show_alert=True,
            )
            return

        new_lang = (
            "en"
            if call.data == "ad_lang_en"
            else "fa"
        )

        updated = (
            store.update_ad_request(
                request["request_id"],
                form_lang=new_lang,
            )
        )

        bot.answer_callback_query(
            call.id
        )

        if updated:
            try:
                bot.edit_message_reply_markup(
                    chat_id_int,
                    call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass

            _send_ad_prompt(
                chat_id_int,
                updated,
                _ad_texts(updated),
            )

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
            t["ad_submitted"],
            reply_markup=_main_reply_markup(),
        )

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

        last_link_messages[
            (
                chat_id_int,
                user_id,
            )
        ] = message

        if not _check_sponsor_channel_gate(
            chat_id_int,
            user_id,
            t,
        ):
            return

        url = message.text.strip()

        _delete_previous_download_ducks(
            chat_id_int
        )

        platform = platforms.detect_platform(
            url
        )

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

            if not _acquire_download_slot(
                bot,
                chat_id_int,
                status_msg,
                t,
            ):
                return

            _send_duck_reaction(
                chat_id_int,
                "downloading",
                track_status=True,
            )

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
                bot.delete_message(
                    chat_id_int,
                    status_msg.message_id,
                )

                _send_duck_download_complete(
                    chat_id_int
                )

                _maybe_send_ad(
                    chat_id_int
                )
            except Exception as e:

                _send_duck_download_failed(
                    chat_id_int
                )

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
            return

        handle_media_link(
            last_message
        )

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

        if not _check_sponsor_channel_gate(
            chat_id_int,
            user_id,
            t,
        ):
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

        _send_duck_reaction(
            chat_id_int,
            "downloading",
            track_status=True,
        )

        try:

            info, entries, files = (
                platforms.download_youtube_quality(
                    video_id,
                    choice,
                    progress_hook,
                )
            )

            _finish_duck_download(
                chat_id_int
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

            _send_duck_download_complete(
                chat_id_int
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