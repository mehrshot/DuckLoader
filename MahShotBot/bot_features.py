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

thumb_cache = {}
user_settings = store.load_user_settings()

# --- rate limiting + concurrency cap ---
RATE_LIMIT_COUNT = 5          # max downloads...
RATE_LIMIT_WINDOW = 60        # ...per this many seconds, per user
MAX_CONCURRENT_DOWNLOADS = 3  # how many downloads run at once, bot-wide

_recent_downloads = defaultdict(list)  # user_id -> [timestamps]
_download_semaphore = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)


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
        'welcome': "🎯 **به DropShot DL خوش آمدید!**\n\nلینک پابلیک از اینستاگرام، ساندکلاد، اسپاتیفای یا یوتیوب (به‌زودی) بفرستید تا با بالاترین کیفیت دانلود کنم.\n\nکیفیت دلخواهتون رو از /settings تنظیم کنید.",
        'init': "⏳ در حال برقراری ارتباط...",
        'downloading': "🔄 **در حال دانلود** {bar} {percent}\n\n📦 حجم: {size}\n⏱ زمان: {eta}",
        'uploading': "✅ دانلود تکمیل شد! در حال آپلود...",
        'failed': "❌ خطا: {error}",
        'not_launched': "🚧 دانلود از {platform} هنوز لانچ نشده. به‌زودی فعال می‌شود!",
        'too_large': "⚠️ حجم این فایل حدود {size} است و از سقف مجاز بیشتره، پس امکان ارسالش نیست.\n\nمی‌تونی از /settings کیفیت پایین‌تر یا «فقط صدا» رو انتخاب کنی.",
        'quality_reduced': "ℹ️ به‌خاطر محدودیت حجم تلگرام، کیفیت به‌صورت خودکار به «{quality}» کاهش یافت.",
        'rate_limited': "⏳ توی یک دقیقه‌ی اخیر بیش از حد مجاز ({limit} تا) دانلود کرده‌ای. کمی صبر کن و دوباره امتحان کن.",
        'queued': "📋 صف دانلود پر است — به‌محض آزاد شدن ظرفیت شروع می‌شود...",
        'spotify_searching': "🔎 در حال جست‌وجو ({i}/{total}): {name}",
        'view_link': "🔗 مشاهده در پلتفرم اصلی",
        'dl_cover': "🖼 دانلود کاور",
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
    },
    'en': {
        'welcome': "🎯 **Welcome to DropShot DL!**\n\nSend me a public link from Instagram, SoundCloud, Spotify, or YouTube (coming soon) and I will fetch it in high quality.\n\nSet your preferred quality in /settings.",
        'init': "⏳ Initializing connection...",
        'downloading': "🔄 **Downloading** {bar} {percent}\n\n📦 Size: {size}\n⏱ ETA: {eta}",
        'uploading': "✅ Download complete! Preparing upload...",
        'failed': "❌ Failed: {error}",
        'not_launched': "🚧 Downloading from {platform} hasn't launched yet. Stay tuned!",
        'too_large': "⚠️ This file is about {size}, over the allowed limit, so it can't be sent.\n\nYou can pick a lower quality or \"audio only\" in /settings.",
        'quality_reduced': "ℹ️ Quality was automatically reduced to \"{quality}\" to stay under Telegram's size limit.",
        'rate_limited': "⏳ You've hit the download limit ({limit}) for the last minute. Please wait a bit and try again.",
        'queued': "📋 The download queue is full — this will start as soon as a slot frees up...",
        'spotify_searching': "🔎 Searching ({i}/{total}): {name}",
        'view_link': "🔗 View Original",
        'dl_cover': "🖼 Download Cover",
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
    }
}


def _texts_for(chat_id) -> dict:
    user = store.get_user(user_settings, chat_id)
    return TEXTS[user['lang']]


def _build_caption(entry: dict) -> str:
    """Builds an Instagram/YouTube/SoundCloud caption from whatever yt-dlp metadata
    fields are available. Works across platforms because it only uses fields
    that are present and skips ones that aren't."""
    uploader = entry.get("uploader") or entry.get("channel") or "Unknown"
    safe_hashtag = re.sub(r"\W+", "_", uploader)

    timestamp = entry.get("timestamp") or entry.get("release_timestamp")
    date_str = datetime.fromtimestamp(timestamp).strftime('%Y/%m/%d, %H:%M') if timestamp else None

    likes = entry.get('like_count') or 0
    comments = entry.get('comment_count') or 0
    views = entry.get('view_count') or 0

    def format_num(n):
        return f"{n:,}" if isinstance(n, int) else n

    stats_line = f"❤️ {format_num(likes)} | 💬 {format_num(comments)} | 👁‍🗨 {format_num(views)}"

    description = entry.get('description') or ''
    max_length = 750
    if len(description) > max_length:
        description = description[:max_length] + "..."

    lines = [f"#{safe_hashtag}", f"👤 {uploader}"]
    if date_str:
        lines.append(f"📅 {date_str}")
    lines.append(stats_line)
    if description:
        lines.append(f"\n📝 {description}")
    lines.append("\n🤖 @DropShotDLBot")
    return "\n".join(lines)


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
    admin.register_admin(bot, flags, _texts_for)

    def _maybe_send_ad(chat_id_int):
        if not flags.get('sponsor_message', False):
            return
        ad_text = ads.load_ad_message()
        if ad_text:
            try:
                bot.send_message(chat_id_int, ad_text, parse_mode="Markdown")
            except Exception:
                pass

    def _send_youtube_quality_picker(message, t):
        chat_id_int = message.chat.id
        status_msg = bot.reply_to(message, t['init'])

        try:
            probe = platforms.probe_youtube_qualities(message.text.strip())
        except Exception as e:
            store.record_error()
            bot.edit_message_text(t['failed'].format(error=str(e)), chat_id_int, status_msg.message_id)
            return

        if not probe['options']:
            bot.edit_message_text(t['yt_no_quality'], chat_id_int, status_msg.message_id)
            return

        digits = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
        markup = InlineKeyboardMarkup(row_width=2)
        buttons = []
        for opt in probe['options']:
            size_label = platforms.format_size(opt['size_bytes'])
            if opt['kind'] == 'audio':
                text = f"🎵 صوت — {size_label}"
                cb = f"ytq_{probe['id']}_audio"
            else:
                height_fa = str(opt['height']).translate(digits)
                text = f"🎬 {height_fa}p — {size_label}"
                cb = f"ytq_{probe['id']}_{opt['height']}"
            buttons.append(InlineKeyboardButton(text=text, callback_data=cb))
        markup.add(*buttons)

        bot.delete_message(chat_id_int, status_msg.message_id)
        caption = t['yt_choose_quality'].format(title=(probe['title'] or '')[:200])
        if probe['thumbnail']:
            bot.send_photo(chat_id_int, probe['thumbnail'], caption=caption, reply_markup=markup, reply_to_message_id=message.message_id)
        else:
            bot.send_message(chat_id_int, caption, reply_markup=markup, reply_to_message_id=message.message_id)

    @bot.message_handler(commands=["start", "help"])
    def send_welcome(message):
        store.track_user(message.chat.id)
        t = _texts_for(message.chat.id)
        bot.reply_to(message, t['welcome'], parse_mode="Markdown")

    @bot.message_handler(commands=["whoami"])
    def whoami(message):
        bot.reply_to(message, f"🆔 `{message.from_user.id}`", parse_mode="Markdown")

    @bot.message_handler(commands=["settings"])
    def open_settings(message):
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

        if platform == "youtube":
            _send_youtube_quality_picker(message, t)
            return

        if _is_rate_limited(user_id):
            bot.reply_to(message, t['rate_limited'].format(limit=RATE_LIMIT_COUNT))
            return

        status_msg = bot.reply_to(message, t['init'])
        bot.send_chat_action(chat_id_int, 'typing')

        last_edit_time = 0

        def progress_hook(d):
            nonlocal last_edit_time
            if d.get('status') == 'downloading':
                now = time.time()
                if now - last_edit_time > 2.0:
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
                    last_edit_time = now

        acquired = _download_semaphore.acquire(blocking=False)
        if not acquired:
            bot.edit_message_text(t['queued'], chat_id_int, status_msg.message_id)
            _download_semaphore.acquire()

        try:
            if platform == "spotify":
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
                        caption = f"💿 {track['album']}\n\n🤖 @DropShotDLBot" if i == 0 else ""
                        with open(filepath, "rb") as audio_file:
                            bot.send_audio(
                                chat_id_int, audio_file,
                                title=track['name'], performer=track['artists'],
                                caption=caption, reply_to_message_id=message.message_id, timeout=120,
                            )
                    finally:
                        if os.path.exists(filepath):
                            os.remove(filepath)
                    store.record_download("spotify")
                bot.delete_message(chat_id_int, status_msg.message_id)
                _maybe_send_ad(chat_id_int)
                return

            allow_fallback = flags.get('auto_quality_fallback', False)
            info, entries, files, quality_used = platforms.download_direct(
                url, quality=user['quality'], allow_fallback=allow_fallback, progress_hook=progress_hook,
            )
            bot.edit_message_text(t['uploading'], chat_id_int, status_msg.message_id)

            if quality_used != user['quality']:
                bot.send_message(chat_id_int, t['quality_reduced'].format(quality=t[f'quality_{quality_used}']))

            metadata_source = entries[0] if entries else info
            caption = _build_caption(metadata_source)

            thumb_url = metadata_source.get('thumbnail')
            post_id = metadata_source.get('id', str(time.time()))
            if thumb_url:
                thumb_cache[post_id] = thumb_url

            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(text=t['view_link'], url=url))
            if thumb_url:
                markup.add(InlineKeyboardButton(text=t['dl_cover'], callback_data=f"thumb_{post_id}"))

            valid_files = [f for f in files if os.path.exists(f)]

            if len(valid_files) == 1:
                filepath = valid_files[0]
                kind = platforms.media_kind(filepath)
                
                if kind == "audio":
                    platforms.tag_audio_file(filepath, title=metadata_source.get('title', ''), artist=metadata_source.get('uploader') or metadata_source.get('channel', ''), cover_url=thumb_url)
                
                bot.send_chat_action(chat_id_int, 'upload_video' if kind == 'video' else 'upload_audio' if kind == 'audio' else 'upload_photo')
                
                try:
                    with open(filepath, "rb") as media_file:
                        if kind == "video":
                            bot.send_video(chat_id_int, media_file, caption=caption, reply_markup=markup, reply_to_message_id=message.message_id, timeout=600)
                        elif kind == "audio":
                            bot.send_audio(chat_id_int, media_file, caption=caption, reply_markup=markup, reply_to_message_id=message.message_id, timeout=600)
                        else:
                            bot.send_photo(chat_id_int, media_file, caption=caption, reply_markup=markup, reply_to_message_id=message.message_id)
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
                        bot.send_media_group(chat_id_int, media_group, reply_to_message_id=message.message_id if chunk_idx == 0 else None, timeout=600)
                    finally:
                        for f in open_files:
                            f.close()
                            
                for filepath in valid_files:
                    if os.path.exists(filepath):
                        os.remove(filepath)

            store.record_download(platform)
            bot.delete_message(chat_id_int, status_msg.message_id)
            _maybe_send_ad(chat_id_int)

        except platforms.FileTooLargeError as e:
            store.record_error()
            bot.edit_message_text(t['too_large'].format(size=str(e)), chat_id_int, status_msg.message_id)
        except Exception as e:
            store.record_error()
            try:
                bot.edit_message_text(t['failed'].format(error=str(e)), chat_id_int, status_msg.message_id)
            except Exception:
                pass
        finally:
            _download_semaphore.release()

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

    @bot.callback_query_handler(func=lambda call: call.data.startswith('ytq_'))
    def handle_youtube_quality_pick(call):
        chat_id_str = str(call.message.chat.id)
        chat_id_int = call.message.chat.id
        user_id = call.from_user.id
        t = _texts_for(chat_id_str)

        if store.is_banned(user_id):
            bot.answer_callback_query(call.id)
            return

        if _is_rate_limited(user_id):
            bot.answer_callback_query(call.id, t['rate_limited'].format(limit=RATE_LIMIT_COUNT), show_alert=True)
            return

        _, video_id, choice = call.data.split('_', 2)
        bot.answer_callback_query(call.id)

        bot.delete_message(chat_id_int, call.message.message_id)
        status_msg = bot.send_message(chat_id_int, t['init'])

        last_edit_time = 0

        def progress_hook(d):
            nonlocal last_edit_time
            if d.get('status') == 'downloading':
                now = time.time()
                if now - last_edit_time > 2.0:
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
                    last_edit_time = now

        acquired = _download_semaphore.acquire(blocking=False)
        if not acquired:
            bot.edit_message_text(t['queued'], chat_id_int, status_msg.message_id)
            _download_semaphore.acquire()

        try:
            info, entries, files = platforms.download_youtube_quality(video_id, choice, progress_hook)
            bot.edit_message_text(t['uploading'], chat_id_int, status_msg.message_id)

            metadata_source = entries[0] if entries else info
            caption = _build_caption(metadata_source)
            source_url = f"https://www.youtube.com/watch?v={video_id}"

            thumb_url = metadata_source.get('thumbnail')
            post_id = metadata_source.get('id', str(time.time()))
            if thumb_url:
                thumb_cache[post_id] = thumb_url

            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(text=t['view_link'], url=source_url))
            if thumb_url:
                markup.add(InlineKeyboardButton(text=t['dl_cover'], callback_data=f"thumb_{post_id}"))

            for j, filepath in enumerate(files):
                if not os.path.exists(filepath):
                    continue

                kind = platforms.media_kind(filepath)
                caption_to_send = caption if j == 0 else ""
                current_markup = markup if j == 0 else None

                if kind == "audio":
                    platforms.tag_audio_file(
                        filepath,
                        title=metadata_source.get('title', ''),
                        artist=metadata_source.get('uploader') or metadata_source.get('channel', ''),
                        cover_url=thumb_url,
                    )

                bot.send_chat_action(chat_id_int, 'upload_video' if kind == 'video' else 'upload_audio' if kind == 'audio' else 'upload_photo')

                try:
                    with open(filepath, "rb") as media_file:
                        if kind == "video":
                            bot.send_video(chat_id_int, media_file, caption=caption_to_send, reply_markup=current_markup, timeout=600)
                        elif kind == "audio":
                            bot.send_audio(chat_id_int, media_file, caption=caption_to_send, reply_markup=current_markup, timeout=600)
                        else:
                            bot.send_photo(chat_id_int, media_file, caption=caption_to_send, reply_markup=current_markup)
                finally:
                    if os.path.exists(filepath):
                        os.remove(filepath)

            store.record_download("youtube")
            bot.delete_message(chat_id_int, status_msg.message_id)
            _maybe_send_ad(chat_id_int)

        except platforms.FileTooLargeError as e:
            store.record_error()
            bot.edit_message_text(t['too_large'].format(size=str(e)), chat_id_int, status_msg.message_id)
        except Exception as e:
            store.record_error()
            try:
                bot.edit_message_text(t['failed'].format(error=str(e)), chat_id_int, status_msg.message_id)
            except Exception:
                pass
        finally:
            _download_semaphore.release()
