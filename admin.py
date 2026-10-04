"""
Owner-only admin commands: platform lock/unlock, bot-wide toggles, usage
stats, broadcast, and bans.

Why is_owner() re-reads os.environ every call instead of caching OWNER_ID
once at import time: bot.py loads .env before it imports any other module,
so by the time any Telegram message can actually arrive, os.environ is
already populated — but caching at import time is a trap that's easy to
reintroduce (e.g. if an import ever moves above load_dotenv() again). Reading
fresh every call makes correctness independent of import order entirely.
"""

import os
import re
import time

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ForceReply

import ads
import campaigns
import platforms
import store

TOGGLE_KEYS = {
    "auto_quality_fallback",
    "ad_requests_button",
    "sponsor_channel_gate",
}

TOGGLE_LABELS = {
    "auto_quality_fallback": "auto_quality_fallback",
    "ad_requests_button": "toggle_ad_requests_button",
    "sponsor_channel_gate": "toggle_sponsor_channel_gate",
}
# user_id -> which text-input action a /settings panel button is waiting on
_pending_action = {}


def is_owner(user_id) -> bool:
    owner_id = int(os.environ.get("OWNER_ID", "0") or "0")
    return owner_id != 0 and user_id == owner_id


def has_pending_action(user_id) -> bool:
    return user_id in _pending_action


# --- admin panel (shown to the owner instead of the normal /settings menu) ---
def _panel_markup(t):
    m = InlineKeyboardMarkup(
        row_width=2
    )

    m.add(
        InlineKeyboardButton(
            t["adm_platforms"],
            callback_data="adm_menu_platforms",
        ),
        InlineKeyboardButton(
            t["adm_toggles"],
            callback_data="adm_menu_toggles",
        ),
    )

    m.add(
        InlineKeyboardButton(
            t["adm_ads"],
            callback_data="adm_menu_ads",
        ),
        InlineKeyboardButton(
            t["adm_ad_requests"],
            callback_data="adm_menu_ad_requests",
        ),
    )

    m.add(
        InlineKeyboardButton(
            t["adm_users"],
            callback_data="adm_menu_users",
        ),
        InlineKeyboardButton(
            t["adm_stats"],
            callback_data="adm_menu_stats",
        ),
    )

    m.add(
        InlineKeyboardButton(
            t["adm_errors"],
            callback_data="adm_menu_errors",
        ),
        InlineKeyboardButton(
            t["adm_duck"],
            callback_data="adm_menu_duck",
        ),
    )

    m.add(
        InlineKeyboardButton(
            t["adm_mysettings"],
            callback_data="adm_menu_mysettings",
        )
    )

    m.add(
        InlineKeyboardButton(
            t["adm_commands"],
            callback_data="adm_menu_commands",
        )
    )

    return m

def _platforms_markup(flags, t) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    for key, name in platforms.PLATFORM_NAMES.items():
        icon = "🔓" if flags.get(key, True) else "🔒"
        m.add(InlineKeyboardButton(f"{icon} {name}", callback_data=f"adm_lock_{key}"))
    m.add(InlineKeyboardButton(t['back'], callback_data='adm_menu_main'))
    return m


def _toggles_markup(
    flags,
    t,
) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(
        row_width=1
    )

    for key in sorted(
        TOGGLE_KEYS
    ):
        icon = (
            "✅"
            if flags.get(
                key,
                False,
            )
            else "❌"
        )

        label_key = (
            TOGGLE_LABELS.get(
                key,
                key,
            )
        )

        label = t.get(
            label_key,
            key,
        )

        m.add(
            InlineKeyboardButton(
                f"{icon} {label}",
                callback_data=(
                    f"adm_toggle_{key}"
                ),
            )
        )

    m.add(
        InlineKeyboardButton(
            t["back"],
            callback_data="adm_menu_main",
        )
    )

    return m


def _ads_markup(t) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    m.add(InlineKeyboardButton(t['adm_setad_btn'], callback_data='adm_camp_' + campaigns.HOUSE_ID))
    m.add(InlineKeyboardButton("📊 گزارش همه‌ی تبلیغ‌های بعد از دانلود", callback_data='adm_camp_list'))
    m.add(InlineKeyboardButton(t['adm_addsponsor_btn'], callback_data='adm_ask_addsponsor'))
    m.add(InlineKeyboardButton(t['adm_removesponsor_btn'], callback_data='adm_ask_removesponsor'))
    m.add(InlineKeyboardButton(t['adm_sponsorlist_btn'], callback_data='adm_sponsors_list'))
    m.add(InlineKeyboardButton(t['back'], callback_data='adm_menu_main'))
    return m


def _users_markup(t) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    m.add(InlineKeyboardButton(t['adm_ban_btn'], callback_data='adm_ask_ban'))
    m.add(InlineKeyboardButton(t['adm_unban_btn'], callback_data='adm_ask_unban'))
    m.add(InlineKeyboardButton(t['adm_broadcast_btn'], callback_data='adm_ask_broadcast'))
    m.add(InlineKeyboardButton(t['adm_exempt_btn'], callback_data='adm_ask_exempt'))
    m.add(InlineKeyboardButton(t['adm_unexempt_btn'], callback_data='adm_ask_unexempt'))
    m.add(InlineKeyboardButton(t['back'], callback_data='adm_menu_main'))
    return m

def _duck_markup(t) -> InlineKeyboardMarkup:
    reactions = store.load_duck_reactions()

    start_count = len(
        reactions.get(
            "start",
            [],
        )
    )

    downloading_count = len(
        reactions.get(
            "downloading",
            [],
        )
    )

    failed_count = len(
        reactions.get(
            "failed",
            [],
        )
    )

    complete_count = len(
        reactions.get(
            "complete",
            [],
        )
    )

    m = InlineKeyboardMarkup(
        row_width=2
    )

    m.add(
        InlineKeyboardButton(
            f"{t['adm_duck_start']} ({start_count})",
            callback_data="adm_duck_category_start",
        ),
        InlineKeyboardButton(
            f"{t['adm_duck_downloading']} ({downloading_count})",
            callback_data="adm_duck_category_downloading",
        ),
    )

    m.add(
        InlineKeyboardButton(
            f"{t['adm_duck_failed']} ({failed_count})",
            callback_data="adm_duck_category_failed",
        ),
        InlineKeyboardButton(
            f"{t['adm_duck_complete']} ({complete_count})",
            callback_data="adm_duck_category_complete",
        ),
    )

    m.add(
        InlineKeyboardButton(
            t["adm_duck_clear_all"],
            callback_data="adm_duck_clear_all",
        )
    )

    m.add(
        InlineKeyboardButton(
            t["back"],
            callback_data="adm_menu_main",
        )
    )

    return m

def _duck_category_markup(
    event,
    t,
) -> InlineKeyboardMarkup:
    reactions = store.load_duck_reactions()

    count = len(
        reactions.get(
            event,
            [],
        )
    )

    m = InlineKeyboardMarkup(
        row_width=1
    )

    m.add(
        InlineKeyboardButton(
            f"➕ Add ({event})",
            callback_data=f"adm_duck_add_{event}",
        )
    )

    m.add(
        InlineKeyboardButton(
            f"🗑 Clear ({count})",
            callback_data=f"adm_duck_clear_{event}",
        )
    )

    m.add(
        InlineKeyboardButton(
            t["back"],
            callback_data="adm_menu_duck",
        )
    )

    return m


def _back_markup(target, t) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton(t['back'], callback_data=target))
    return m


def _stats_text(t) -> str:
    """Totals, the last 7 days, and where users came from — the numbers an
    advertiser asks for."""
    stats = store.load_stats()
    users = store.load_known_users()

    lines = [f"👥 {t['stats_users']}: {len(users)}"]
    for platform_name, count in sorted(stats.get("downloads", {}).items(), key=lambda kv: -kv[1]):
        lines.append(f"  • {platform_name}: {count}")
    lines.append(f"❌ {t['stats_errors']}: {stats.get('errors', 0)}")

    groups = stats.get("groups") or {}
    lines.append(f"👨‍👩‍👧 {t['stats_groups']}: {sum(1 for g in groups.values() if g.get('active'))}")
    lines.append(f"🔎 {t['stats_inline']}: {stats.get('inline_sends', 0)}")

    daily = stats.get("daily") or {}
    if daily:
        lines.append("")
        lines.append(t["stats_daily_title"])
        for day in sorted(daily)[-7:][::-1]:
            bucket = daily[day]
            lines.append(
                f"{day}: +{bucket.get('new_users', 0)} | "
                f"{len(bucket.get('active_users') or [])} | "
                f"{bucket.get('downloads', 0)}"
            )

    sources = stats.get("sources") or {}
    if sources:
        lines.append("")
        lines.append(t["stats_sources_title"])
        for source, count in sorted(sources.items(), key=lambda kv: -kv[1])[:15]:
            lines.append(f"  • {source}: {count}")

    return "\n".join(lines)


def _broadcast(bot, text) -> tuple:
    """Sends `text` to every known chat. Paced under Telegram's ~30 msg/s
    limit, and a 429 "Too Many Requests" waits the time Telegram asks for
    and retries instead of silently dropping that user."""
    sent, failed = 0, 0

    for chat_id in list(store.load_known_users()):
        for _attempt in range(3):
            try:
                bot.send_message(chat_id, text)
                sent += 1
                break
            except Exception as e:
                retry_after = getattr(e, "result_json", {}) or {}
                retry_after = (retry_after.get("parameters") or {}).get("retry_after")
                if getattr(e, "error_code", None) == 429 and retry_after:
                    time.sleep(int(retry_after) + 1)
                    continue
                failed += 1  # blocked the bot, deleted account, left group...
                break
        else:
            failed += 1

        time.sleep(0.05)

    return sent, failed


def build_panel(t):
    """Returns (text, markup) for the admin panel's home screen."""
    return t['adm_title'], _panel_markup(t)

def _ad_request_status_label(
    request,
    t,
):
    status = request.get(
        "status"
    )

    if status == "pending":
        return t[
            "ad_admin_request_status_pending"
        ]

    if status == "approved":
        return t[
            "ad_admin_request_status_approved"
        ]

    if status == "rejected":
        return t[
            "ad_admin_request_status_rejected"
        ]

    if status in campaigns.STATUS_LABELS:
        return campaigns.STATUS_LABELS[status]

    return status or "—"


def _format_ad_request_for_admin(
    request,
    t,
):
    username = (
        request.get(
            "telegram_username"
        )
        or "ندارد"
    )

    return (
        f"{t['ad_admin_new']}\n\n"
        f"{t['ad_admin_request_id']}: "
        f"{request.get('request_id', '—')}\n"
        f"Status: "
        f"{_ad_request_status_label(request, t)}\n\n"
        f"{t['ad_admin_user']}\n"
        f"{t['ad_admin_username']}: "
        f"{username}\n"
        f"{t['ad_admin_user_id']}: "
        f"{request.get('user_id', '—')}\n"
        f"{t['ad_admin_name']}: "
        f"{request.get('telegram_name', '—')}\n\n"
        f"{t['ad_admin_channel']}: "
        f"{request.get('channel', '—')}\n"
        f"{t['ad_admin_display_name']}: "
        f"{request.get('display_name', '—')}\n"
        f"{t['ad_admin_type']}: "
        f"{request.get('ad_type', '—')}\n"
        f"{t['ad_admin_duration']}: "
        f"{request.get('duration', '—')}\n"
        f"{t['ad_admin_notes']}: "
        f"{request.get('notes', '—')}\n"
        f"{t['ad_admin_created_at']}: "
        f"{request.get('created_at', '—')}"
    )


def _ad_requests_markup(
    requests,
    t,
) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(
        row_width=1
    )

    for request in requests[:10]:
        request_id = request.get(
            "request_id"
        )

        display_name = (
            request.get(
                "display_name"
            )
            or request.get(
                "channel"
            )
            or request_id
            or "—"
        )

        display_name = str(
            display_name
        )[:35]

        m.add(
            InlineKeyboardButton(
                f"🆕 {request_id} — {display_name}",
                callback_data=(
                    "adm_adreq_view_"
                    + str(request_id)
                ),
            )
        )

    m.add(
        InlineKeyboardButton(
            "📊 گزارش تبلیغ‌های بعد از دانلود",
            callback_data="adm_camp_list",
        )
    )

    m.add(
        InlineKeyboardButton(
            "🔄",
            callback_data="adm_menu_ad_requests",
        )
    )

    m.add(
        InlineKeyboardButton(
            t["back"],
            callback_data="adm_menu_main",
        )
    )

    return m


def _ad_request_action_markup(
    request,
    t,
) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(
        row_width=2
    )

    if request.get(
        "status"
    ) == "pending":
        m.add(
            InlineKeyboardButton(
                t["ad_admin_approve"],
                callback_data=(
                    "adm_adreq_approve_"
                    + str(
                        request["request_id"]
                    )
                ),
            ),
            InlineKeyboardButton(
                t["ad_admin_reject"],
                callback_data=(
                    "adm_adreq_reject_"
                    + str(
                        request["request_id"]
                    )
                ),
            ),
        )

    if (
        request.get("ad_type") == "post_download"
        and request.get("status") in campaigns.CAMPAIGN_STATUSES
    ):
        m.add(
            InlineKeyboardButton(
                "📊 گزارش نمایش و کلیک",
                callback_data="adm_camp_" + str(request["request_id"]),
            )
        )

    m.add(
        InlineKeyboardButton(
            t["ad_admin_contact"],
            url=(
                "tg://user?id="
                + str(
                    request["user_id"]
                )
            ),
        )
    )

    m.add(
        InlineKeyboardButton(
            t["back"],
            callback_data="adm_menu_ad_requests",
        )
    )

    return m

def _campaigns_list_markup(requests, t) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    for request in requests[:20]:
        stats = campaigns.get_stats(request["request_id"])
        limit = campaigns.quota(request)
        progress = f"{stats['impressions']}/{limit}" if limit else str(stats["impressions"])
        icon = campaigns.STATUS_LABELS.get(request.get("status"), "•").split(" ")[0]
        m.add(
            InlineKeyboardButton(
                f"{icon} {campaigns.display_name(request)[:30]} — {progress}",
                callback_data="adm_camp_" + str(request["request_id"]),
            )
        )
    m.add(InlineKeyboardButton(t["back"], callback_data="adm_menu_ad_requests"))
    return m


def campaign_report_markup(request, t) -> InlineKeyboardMarkup:
    """Buttons under a campaign report (also used for the "quota reached"
    message the bot sends the owner)."""
    request_id = str(request["request_id"])
    m = InlineKeyboardMarkup(row_width=3)
    if request.get("status") == campaigns.ACTIVE:
        m.add(InlineKeyboardButton("⏸ توقف نمایش", callback_data="adm_campt_" + request_id))
    elif request.get("status") == campaigns.PAUSED:
        m.add(InlineKeyboardButton("▶️ شروع / ادامه‌ی نمایش", callback_data="adm_campt_" + request_id))
    m.add(
        InlineKeyboardButton("🖼 تنظیم بنر", callback_data="adm_campb_" + request_id),
        InlineKeyboardButton("👁 پیش‌نمایش", callback_data="adm_campv_" + request_id),
    )
    m.add(
        InlineKeyboardButton(
            "🔘 دکمه زیر بنر: " + ("روشن ✅" if campaigns.wants_button(request) else "خاموش"),
            callback_data="adm_campk_" + request_id,
        ),
        InlineKeyboardButton("🔗 لینک دکمه", callback_data="adm_campl_" + request_id),
    )
    if not campaigns.is_house(request):
        m.add(
            InlineKeyboardButton("🎯 +100", callback_data=f"adm_campq_{request_id}_100"),
            InlineKeyboardButton("🎯 +500", callback_data=f"adm_campq_{request_id}_500"),
            InlineKeyboardButton("🎯 +1000", callback_data=f"adm_campq_{request_id}_1000"),
        )
        m.add(InlineKeyboardButton("📤 ارسال گزارش برای تبلیغ‌دهنده", callback_data="adm_camps_" + request_id))
    m.add(
        InlineKeyboardButton("🔄", callback_data="adm_camp_" + request_id),
        InlineKeyboardButton(t["back"], callback_data="adm_camp_list"),
    )
    return m


CAMPAIGN_BANNER_PROMPT = (
    "🖼 پست بنر رو بفرست یا از هر کانالی فوروارد کن:\n"
    "• عکس، ویدیو یا گیف با کپشن\n"
    "• یا فقط متن\n\n"
    "فرمت‌بندی کپشن (بولد، لینک، ایموجی و …) همون‌طور حفظ میشه. "
    "ایموجی‌های پریمیوم به ایموجی معمولی تبدیل میشن.\n\n"
    "/cancel برای انصراف"
)

CAMPAIGN_LINK_PROMPT = (
    "🔗 لینک کانال رو بفرست، مثل @sut_tw یا https://t.me/sut_tw\n"
    "اگه می‌خوای متن دکمه چیز دیگه‌ای باشه، توی خط دوم بنویسش.\n"
    "برای حذف لینک: -\n\n"
    "/cancel برای انصراف"
)


def _set_house_ad_text(owner_id, text):
    """/setad: the owner's own ad as a plain text banner."""
    campaigns.ensure_house_campaign(owner_id)
    if text:
        store.update_ad_request(
            campaigns.HOUSE_ID,
            banner={"type": "text", "file_id": None, "text": text, "entities": [], "markdown": True},
        )
    else:
        store.update_ad_request(campaigns.HOUSE_ID, banner=None, status=campaigns.PAUSED)


def _handle_campaign_input(bot, message, action, t):
    """The owner's answer to "send the banner" / "send the button link"."""
    kind, _, request_id = action.partition(":")
    chat_id = message.chat.id
    request = store.get_ad_request(request_id)
    if not request:
        bot.reply_to(message, "Request not found.")
        return
    if (message.text or "").strip().startswith("/"):
        bot.reply_to(message, t["adm_cancelled"])
        return

    if kind == "campbanner":
        banner = campaigns.banner_from_message(message)
        if not banner:
            _pending_action[message.from_user.id] = action
            bot.reply_to(message, "این نوع پیام رو نمی‌تونم بنر کنم. عکس، ویدیو، گیف یا متن بفرست (یا /cancel).")
            return
        request = store.update_ad_request(request_id, banner=banner) or request
        bot.reply_to(message, "✅ بنر ذخیره شد. کاربرها دقیقاً این رو می‌بینن 👇")
    else:
        lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
        if not lines:
            _pending_action[message.from_user.id] = action
            bot.reply_to(message, CAMPAIGN_LINK_PROMPT)
            return
        if lines[0] in ("-", "حذف"):
            changes = {"channel": "", "button_text": ""}
        else:
            link = lines[0]
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", link):
                link = "@" + link
            if not campaigns.channel_url({"channel": link}):
                _pending_action[message.from_user.id] = action
                bot.reply_to(message, "این لینک معتبر نیست.\n\n" + CAMPAIGN_LINK_PROMPT)
                return
            changes = {"channel": link, "button_text": lines[1][:60] if len(lines) > 1 else ""}
        request = store.update_ad_request(request_id, **changes) or request
        bot.reply_to(message, "✅ لینک دکمه ذخیره شد. پیش‌نمایش 👇")

    try:
        campaigns.send_ad(bot, chat_id, request, preview=True)
    except ValueError:
        bot.send_message(chat_id, "(هنوز بنر یا لینکی برای نمایش تنظیم نشده.)")
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ پیش‌نمایش ارسال نشد: {e}")
    bot.send_message(chat_id, campaigns.report_text(request), reply_markup=campaign_report_markup(request, t))


def register_admin(bot, flags: dict, texts_for, my_settings_view):
    """`texts_for(chat_id)` returns that chat's TEXTS dict so admin replies
    respect the sender's language like everything else in the bot.
    `my_settings_view(chat_id)` returns (text, markup) for the normal
    per-user settings menu, so the panel's "my own settings" button can
    show it to the owner too — being an admin doesn't mean losing access to
    your own language/quality preferences."""

    @bot.message_handler(commands=["lock", "unlock"])
    def toggle_lock(message):
        t = texts_for(message.chat.id)
        if not is_owner(message.from_user.id):
            bot.reply_to(message, t['not_owner'])
            return

        parts = message.text.split()
        cmd = parts[0].lstrip("/").split("@")[0]  # strip "@BotName" if used as /lock@DropShotDLBot
        target = parts[1].lower() if len(parts) == 2 else None

        if target not in platforms.PLATFORM_NAMES:
            bot.reply_to(message, t['lock_usage'].format(cmd=cmd, options="|".join(platforms.PLATFORM_NAMES)))
            return

        flags[target] = (cmd == "unlock")
        store.save_flags(flags)
        icon = "🔓" if flags[target] else "🔒"
        status = t['status_unlocked'] if flags[target] else t['status_locked']
        bot.reply_to(message, t['lock_done'].format(icon=icon, name=platforms.PLATFORM_NAMES[target], status=status))

    @bot.message_handler(commands=["toggle"])
    def toggle_feature(message):
        t = texts_for(message.chat.id)
        if not is_owner(message.from_user.id):
            bot.reply_to(message, t['not_owner'])
            return

        parts = message.text.split()
        target = parts[1].lower() if len(parts) == 2 else None

        if target not in TOGGLE_KEYS:
            bot.reply_to(message, t['toggle_usage'].format(options="|".join(TOGGLE_KEYS)))
            return

        flags[target] = not flags.get(target, False)
        store.save_flags(flags)
        icon = "✅" if flags[target] else "❌"
        status = t['status_on'] if flags[target] else t['status_off']
        bot.reply_to(message, t['lock_done'].format(icon=icon, name=target, status=status))

    @bot.message_handler(commands=["stats"])
    def show_stats(message):
        t = texts_for(message.chat.id)
        if not is_owner(message.from_user.id):
            bot.reply_to(message, t['not_owner'])
            return

        bot.reply_to(message, _stats_text(t))

    @bot.message_handler(commands=["broadcast"])
    def broadcast(message):
        t = texts_for(message.chat.id)
        if not is_owner(message.from_user.id):
            bot.reply_to(message, t['not_owner'])
            return

        text = message.text.partition(" ")[2].strip()
        if not text:
            bot.reply_to(message, t['broadcast_usage'])
            return

        sent, failed = _broadcast(bot, text)
        bot.reply_to(message, t['broadcast_done'].format(sent=sent, failed=failed))

    @bot.message_handler(commands=["ban", "unban"])
    def ban_toggle(message):
        t = texts_for(message.chat.id)
        if not is_owner(message.from_user.id):
            bot.reply_to(message, t['not_owner'])
            return

        parts = message.text.split()
        cmd = parts[0].lstrip("/").split("@")[0]

        if len(parts) != 2 or not parts[1].isdigit():
            bot.reply_to(message, t['ban_usage'].format(cmd=cmd))
            return

        target_id = int(parts[1])
        if cmd == "ban":
            store.ban_user(target_id)
            bot.reply_to(message, t['ban_done'].format(id=target_id))
        else:
            found = store.unban_user(target_id)
            bot.reply_to(message, t['unban_done'].format(id=target_id) if found else t['unban_not_found'])

    @bot.message_handler(commands=["setad"])
    def set_ad(message):
        t = texts_for(message.chat.id)
        if not is_owner(message.from_user.id):
            bot.reply_to(message, t['not_owner'])
            return

        text = message.text.partition(" ")[2].strip()
        _set_house_ad_text(message.from_user.id, text)
        bot.reply_to(message, t['setad_done'] if text else t['setad_cleared'])

    @bot.message_handler(
        commands=["checksponsor"]
    )
    def check_sponsor_command(
        message
    ):
        if not is_owner(
            message.from_user.id
        ):
            return

        parts = (
            message.text or ""
        ).split()

        if len(parts) != 2:
            bot.reply_to(
                message,
                "Usage:\n/checksponsor @ChannelUsername",
            )
            return

        channel_username = (
            ads.normalize_sponsor_channel(
                parts[1]
            )
        )

        if not channel_username:
            bot.reply_to(
                message,
                "Invalid channel username.",
            )
            return

        try:
            chat = bot.get_chat(
                channel_username
            )

            bot_info = bot.get_me()

            administrators = (
                bot.get_chat_administrators(
                    chat.id
                )
            )

            bot_admin = None

            for admin in administrators:
                if (
                    admin.user.id
                    == bot_info.id
                ):
                    bot_admin = admin
                    break

            result_lines = [
                f"Channel: {chat.title or channel_username}",
                f"Chat ID: {chat.id}",
                f"Username: {getattr(chat, 'username', None) or channel_username}",
            ]

            if bot_admin is None:
                result_lines.append(
                    "❌ Bot is NOT an administrator in this channel."
                )

                bot.reply_to(
                    message,
                    "\n".join(
                        result_lines
                    ),
                )
                return

            result_lines.append(
                f"✅ Bot administrator status: {bot_admin.status}"
            )

            result_lines.append(
                f"can_manage_chat: "
                f"{getattr(bot_admin, 'can_manage_chat', None)}"
            )

            result_lines.append(
                "Now testing getChatMember..."
            )

            try:
                member = (
                    bot.get_chat_member(
                        chat.id,
                        message.from_user.id,
                    )
                )

                result_lines.append(
                    "✅ getChatMember works."
                )

                result_lines.append(
                    f"Test user status: "
                    f"{member.status}"
                )

            except Exception as exc:
                result_lines.append(
                    "❌ getChatMember FAILED."
                )

                result_lines.append(
                    f"Error: {exc}"
                )

            bot.reply_to(
                message,
                "\n".join(
                    result_lines
                ),
            )

        except Exception as exc:
            bot.reply_to(
                message,
                (
                    "❌ Could not inspect channel.\n\n"
                    f"Error: {exc}"
                ),
            )

    @bot.message_handler(commands=["addsponsor"])
    def add_sponsor(message):
        t = texts_for(message.chat.id)
        if not is_owner(message.from_user.id):
            bot.reply_to(message, t['not_owner'])
            return

        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(message, t['addsponsor_usage'])
            return

        username, name = parts[1], parts[2]
        ads.add_sponsor_channel(username, name)
        bot.reply_to(message, t['addsponsor_done'].format(name=name))
        bot.reply_to(message, t['addsponsor_reminder'])

    @bot.message_handler(commands=["removesponsor"])
    def remove_sponsor(message):
        t = texts_for(message.chat.id)
        if not is_owner(message.from_user.id):
            bot.reply_to(message, t['not_owner'])
            return

        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, t['removesponsor_usage'])
            return

        found = ads.remove_sponsor_channel(parts[1])
        bot.reply_to(message, t['removesponsor_done'] if found else t['removesponsor_not_found'])

    @bot.message_handler(commands=["sponsors"])
    def list_sponsors(message):
        t = texts_for(message.chat.id)
        if not is_owner(message.from_user.id):
            bot.reply_to(message, t['not_owner'])
            return

        channels = ads.load_sponsor_channels()
        if not channels:
            bot.reply_to(message, t['sponsors_empty'])
            return

        lines = [f"• {c['username']} — {c['name']}" for c in channels]
        bot.reply_to(message, "\n".join(lines))

    @bot.callback_query_handler(func=lambda call: call.data.startswith('adm_'))
    def handle_admin_panel(call):
        user_id = call.from_user.id
        if not is_owner(user_id):
            bot.answer_callback_query(call.id)
            return

        chat_id_int = call.message.chat.id
        t = texts_for(chat_id_int)
        data = call.data

        def edit(text, markup):
            try:
                bot.edit_message_text(text, chat_id_int, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            except Exception as e:
                if "message is not modified" in str(e):
                    return
                # Fall back to plain text if Markdown can't be parsed.
                edit_plain(text, markup)

        def edit_plain(text, markup):
            try:
                bot.edit_message_text(
                    text,
                    chat_id_int,
                    call.message.message_id,
                    reply_markup=markup,
                )
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise

        if data == 'adm_menu_main':
            edit(t['adm_title'], _panel_markup(t))
        elif data == "adm_menu_commands":
            edit(
                t["adm_commands_title"],
                _back_markup(
                    "adm_menu_main",
                    t,
                ),
            )
        elif data == 'adm_menu_platforms':
            edit(t['adm_platforms_title'], _platforms_markup(flags, t))
        elif data == 'adm_menu_toggles':
            edit(t['adm_toggles_title'], _toggles_markup(flags, t))
        elif data == 'adm_menu_ads':
            edit(
                t["adm_ads_title"],
                _ads_markup(t),
            )

        elif data == "adm_menu_ad_requests":
            pending_requests = (
                store.list_ad_requests(
                    "pending"
                )
            )

            if not pending_requests:
                edit_plain(
                    t["adm_ad_requests_empty"],
                    _ad_requests_markup(
                        [],
                        t,
                    ),
                )
            else:
                text = (
                    t["adm_ad_requests_title"]
                    + "\n\n"
                    + "⏳ Pending: "
                    + str(
                        len(
                            pending_requests
                        )
                    )
                )

                edit_plain(
                    text,
                    _ad_requests_markup(
                        pending_requests,
                        t,
                    ),
                )

        elif data.startswith(
            "adm_adreq_view_"
        ):
            request_id = data.split(
                "adm_adreq_view_",
                1,
            )[1]

            request = (
                store.get_ad_request(
                    request_id
                )
            )

            if not request:
                bot.answer_callback_query(
                    call.id,
                    "Request not found.",
                    show_alert=True,
                )
                return

            edit_plain(
                _format_ad_request_for_admin(
                    request,
                    t,
                ),
                _ad_request_action_markup(
                    request,
                    t,
                ),
            )

        elif data.startswith(
            "adm_adreq_approve_"
        ):
            request_id = data.split(
                "adm_adreq_approve_",
                1,
            )[1]

            request = (
                store.get_ad_request(
                    request_id
                )
            )

            if not request:
                bot.answer_callback_query(
                    call.id,
                    "Request not found.",
                    show_alert=True,
                )
                return

            if request.get(
                "status"
            ) != "pending":
                bot.answer_callback_query(
                    call.id,
                    "This request has already been processed.",
                    show_alert=True,
                )
                return

            updated = (
                store.update_ad_request(
                    request_id,
                    status="approved",
                    reviewed_at=store.iran_time("%Y-%m-%d %H:%M:%S"),
                )
            )

            if not updated:
                bot.answer_callback_query(
                    call.id,
                    "Could not update request.",
                    show_alert=True,
                )
                return

            if updated.get("ad_type") == "post_download":
                updated = store.update_ad_request(request_id, status=campaigns.PAUSED) or updated

            if (
                updated.get(
                    "ad_type"
                )
                == "sponsor_channel"
            ):
                channel_username = (
                    updated.get(
                        "channel",
                        "",
                    )
                    or ""
                ).strip()

                display_name = (
                    updated.get(
                        "display_name",
                        "",
                    )
                    or ""
                ).strip()

                normalized_channel = (
                    ads.normalize_sponsor_channel(
                        channel_username
                    )
                )

                if normalized_channel:
                    ads.add_sponsor_channel(
                        normalized_channel,
                        display_name
                        or normalized_channel,
                    )

            try:
                user_t = texts_for(
                    updated["user_id"]
                )

                bot.send_message(
                    updated["user_id"],
                    user_t[
                        "ad_admin_approved"
                    ],
                )
            except Exception:
                pass

            edit_plain(
                _format_ad_request_for_admin(
                    updated,
                    t,
                ),
                _ad_request_action_markup(
                    updated,
                    t,
                ),
            )

            if updated.get("ad_type") == "post_download":
                bot.answer_callback_query(
                    call.id,
                    "✅ تأیید شد. تبلیغ فعلاً متوقفه: از «📊 گزارش نمایش و کلیک» بنر رو تنظیم کن، "
                    "پیش‌نمایشش رو ببین و ▶️ رو بزن.",
                    show_alert=True,
                )
                return

        elif data.startswith(
            "adm_adreq_reject_"
        ):
            request_id = data.split(
                "adm_adreq_reject_",
                1,
            )[1]

            request = (
                store.get_ad_request(
                    request_id
                )
            )

            if not request:
                bot.answer_callback_query(
                    call.id,
                    "Request not found.",
                    show_alert=True,
                )
                return

            if request.get(
                "status"
            ) != "pending":
                bot.answer_callback_query(
                    call.id,
                    "This request has already been processed.",
                    show_alert=True,
                )
                return

            updated = (
                store.update_ad_request(
                    request_id,
                    status="rejected",
                    reviewed_at=store.iran_time("%Y-%m-%d %H:%M:%S"),
                )
            )

            if not updated:
                bot.answer_callback_query(
                    call.id,
                    "Could not update request.",
                    show_alert=True,
                )
                return

            try:
                user_t = texts_for(
                    updated["user_id"]
                )

                bot.send_message(
                    updated["user_id"],
                    user_t[
                        "ad_admin_rejected"
                    ],
                )
            except Exception:
                pass

            edit_plain(
                _format_ad_request_for_admin(
                    updated,
                    t,
                ),
                _ad_request_action_markup(
                    updated,
                    t,
                ),
            )

        elif data == "adm_camp_list":
            running = campaigns.list_campaigns()
            text = (
                "📊 تبلیغ‌های بعد از دانلود\n\n"
                "🟢 در حال نمایش  ⏸ متوقف  ✅ تمام شده\n"
                "عدد جلوی هر تبلیغ: نمایش داده‌شده / سقف نمایش"
                if running else
                "هنوز هیچ تبلیغ «بعد از دانلود» تأیید نشده."
            )
            edit_plain(text, _campaigns_list_markup(running, t))

        elif data.startswith((
            "adm_camp_", "adm_campt_", "adm_campq_", "adm_camps_",
            "adm_campb_", "adm_campv_", "adm_campk_", "adm_campl_",
        )):
            action, rest = data.split("_", 2)[1], data.split("_", 2)[2]
            request_id, _, amount = rest.partition("_")
            request = store.get_ad_request(request_id)
            if not request and request_id == campaigns.HOUSE_ID:
                request = campaigns.ensure_house_campaign(user_id)
            if not request:
                bot.answer_callback_query(call.id, "Request not found.", show_alert=True)
                return

            if action == "campt":
                if request.get("status") == campaigns.ACTIVE:
                    request = store.update_ad_request(request_id, status=campaigns.PAUSED) or request
                elif request.get("status") == campaigns.PAUSED:
                    if not campaigns.can_show(request):
                        bot.answer_callback_query(call.id, "اول بنر یا لینک دکمه رو تنظیم کن.", show_alert=True)
                        return
                    request = store.update_ad_request(request_id, status=campaigns.ACTIVE) or request

            elif action == "campb":
                _pending_action[user_id] = f"campbanner:{request_id}"
                bot.send_message(chat_id_int, CAMPAIGN_BANNER_PROMPT)

            elif action == "campl":
                _pending_action[user_id] = f"camplink:{request_id}"
                bot.send_message(chat_id_int, CAMPAIGN_LINK_PROMPT)

            elif action == "campv":
                try:
                    campaigns.send_ad(bot, chat_id_int, request, preview=True)
                    bot.answer_callback_query(call.id)
                except ValueError:
                    bot.answer_callback_query(call.id, "هنوز بنر یا لینکی تنظیم نشده.", show_alert=True)
                except Exception as e:
                    bot.answer_callback_query(call.id, f"پیش‌نمایش ارسال نشد: {e}"[:200], show_alert=True)
                return

            elif action == "campk":
                if not campaigns.channel_url(request):
                    bot.answer_callback_query(call.id, "اول «🔗 لینک دکمه» رو تنظیم کن.", show_alert=True)
                    return
                request = store.update_ad_request(request_id, button=not campaigns.wants_button(request)) or request

            elif action == "campq":
                shown = campaigns.get_stats(request_id)["impressions"]
                new_quota = max(campaigns.quota(request), shown) + int(amount or 0)
                changes = {"max_impressions": new_quota}
                if request.get("status") == campaigns.COMPLETED:
                    changes["status"] = campaigns.ACTIVE
                request = store.update_ad_request(request_id, **changes) or request
                edit_plain(campaigns.report_text(request), campaign_report_markup(request, t))
                bot.answer_callback_query(call.id, f"🎯 سقف نمایش جدید: {new_quota:,}")
                return

            elif action == "camps":
                try:
                    bot.send_message(
                        request["user_id"],
                        campaigns.report_text(request, for_advertiser=True),
                    )
                    bot.answer_callback_query(call.id, "📤 گزارش برای تبلیغ‌دهنده ارسال شد.", show_alert=True)
                except Exception:
                    bot.answer_callback_query(
                        call.id,
                        "ارسال نشد؛ احتمالاً تبلیغ‌دهنده ربات رو بلاک کرده.",
                        show_alert=True,
                    )
                return

            edit_plain(campaigns.report_text(request), campaign_report_markup(request, t))

        elif data == 'adm_menu_users':
            edit(
                t["adm_users_title"],
                _users_markup(t),
            )
        elif data == 'adm_menu_mysettings':
            text, markup = my_settings_view(
                chat_id_int
            )

            markup.add(
                InlineKeyboardButton(
                    t["back"],
                    callback_data="adm_menu_main",
                )
            )

            edit(
                text,
                markup,
            )
        elif data == 'adm_menu_stats':
            edit_plain(_stats_text(t), _back_markup('adm_menu_main', t))
        elif data == "adm_menu_duck":
            reactions = (
                store.load_duck_reactions()
            )

            def reaction_status(event):
                # load_duck_reactions() always has every key, so check
                # whether the list is non-empty rather than key presence.
                return (
                    "✅"
                    if reactions.get(event)
                    else "❌"
                )

            text = (
                t["adm_duck_title"]
                + "\n\n"
                + f"{reaction_status('start')} "
                + t["adm_duck_start"]
                + "\n"
                + f"{reaction_status('downloading')} "
                + t["adm_duck_downloading"]
                + "\n"
                + f"{reaction_status('failed')} "
                + t["adm_duck_failed"]
                + "\n"
                + f"{reaction_status('complete')} "
                + t["adm_duck_complete"]
            )

            edit_plain(
                text,
                _duck_markup(t),
            )

        elif data == "adm_duck_clear_all":
            # Must be checked before the startswith("adm_duck_clear_")
            # branch below, which used to swallow it as an invalid "all"
            # category — the Clear All button never worked.
            store.clear_all_duck_reactions()

            edit_plain(
                t["adm_duck_all_cleared"],
                _duck_markup(t),
            )

        elif data.startswith(
            "adm_duck_clear_"
        ):
            event = data.split(
                "adm_duck_clear_",
                1,
            )[1]

            if event not in {
                "start",
                "downloading",
                "failed",
                "complete",
            }:
                bot.answer_callback_query(
                    call.id,
                    "Invalid category.",
                    show_alert=True,
                )
                return

            store.clear_duck_reaction(
                event
            )

            reactions = (
                store.load_duck_reactions()
            )

            text = (
                t["adm_duck_title"]
                + "\n\n"
                + f"{t['adm_duck_start']}: "
                + str(
                    len(
                        reactions.get(
                            "start",
                            [],
                        )
                    )
                )
                + "\n"
                + f"{t['adm_duck_downloading']}: "
                + str(
                    len(
                        reactions.get(
                            "downloading",
                            [],
                        )
                    )
                )
                + "\n"
                + f"{t['adm_duck_failed']}: "
                + str(
                    len(
                        reactions.get(
                            "failed",
                            [],
                        )
                    )
                )
                + "\n"
                + f"{t['adm_duck_complete']}: "
                + str(
                    len(
                        reactions.get(
                            "complete",
                            [],
                        )
                    )
                )
            )

            edit_plain(
                text,
                _duck_markup(t),
            )

        elif data == "adm_menu_errors":

            errors = (
                store.load_error_log()
            )

            if not errors:

                edit_plain(
                    t["adm_errors_empty"],
                    _back_markup(
                        "adm_menu_main",
                        t,
                    ),
                )

            else:

                recent = errors[-20:]

                lines = [
                    t["adm_errors_title"],
                    "",
                ]

                for index, event in enumerate(
                    reversed(recent),
                    start=1,
                ):

                    lines.append(
                        f"#{index} "
                        f"{event.get('time', '')}"
                    )

                    lines.append(
                        "Platform: "
                        f"{event.get('platform', 'unknown')}"
                    )

                    if event.get("user_id") is not None:
                        lines.append(
                            "User: "
                            f"{event.get('user_id')}"
                        )

                    lines.append(
                        "Link: "
                        f"{event.get('url', '')}"
                    )

                    lines.append(
                        "Error: "
                        f"{event.get('error', '')}"
                    )

                    lines.append(
                        "────────────────"
                    )

                text = "\n".join(lines)

                # Telegram message length safety.
                text = text[:3900]

                edit_plain(
                    text,
                    _back_markup(
                        "adm_menu_main",
                        t,
                    ),
                )
        elif data == 'adm_sponsors_list':
            channels = ads.load_sponsor_channels()
            text = "\n".join(f"• {c['username']} — {c['name']}" for c in channels) if channels else t['sponsors_empty']
            # Channel usernames often contain "_", which breaks Markdown.
            edit_plain(text, _back_markup('adm_menu_ads', t))
        elif data.startswith("adm_duck_set_"):
            event = data.split(
                "adm_duck_set_",
                1,
            )[1]

            if event not in {
                "start",
                "downloading",
                "failed",
                "complete",
            }:
                bot.answer_callback_query(
                    call.id,
                    "Invalid duck reaction.",
                    show_alert=True,
                )
                return

            _pending_action[user_id] = (
                f"duck_{event}"
            )

            bot.send_message(
                chat_id_int,
                t[
                    f"adm_ask_duck_{event}"
                ],
                reply_markup=ForceReply(
                    selective=True
                ),
            )

        elif data.startswith('adm_lock_'):
            key = data.split('adm_lock_', 1)[1]
            flags[key] = not flags.get(key, True)
            store.save_flags(flags)
            edit(t['adm_platforms_title'], _platforms_markup(flags, t))
        elif data.startswith('adm_toggle_'):
            key = data.split('adm_toggle_', 1)[1]
            flags[key] = not flags.get(key, False)
            store.save_flags(flags)
            edit(t['adm_toggles_title'], _toggles_markup(flags, t))
        elif data.startswith('adm_ask_'):
            action = data.split('adm_ask_', 1)[1]
            _pending_action[user_id] = action
            bot.send_message(chat_id_int, t[f'adm_ask_{action}'], reply_markup=ForceReply(selective=True))

        bot.answer_callback_query(call.id)

    @bot.message_handler(
        func=lambda msg:
            msg.from_user is not None
            and msg.from_user.id in _pending_action,
        content_types=[
            "text",
            "sticker",
            "animation",
            "photo",
            "video",
        ],
    )
    def handle_admin_reply(message):
        user_id = message.from_user.id

        action = _pending_action.pop(
            user_id,
            None,
        )

        if not action or not is_owner(
            user_id
        ):
            return

        t = texts_for(
            message.chat.id
        )

        if action.startswith(("campbanner:", "camplink:")):
            _handle_campaign_input(bot, message, action, t)
            return

        # -----------------------------------------------------------
        # Duck reaction upload
        # -----------------------------------------------------------

        if action.startswith(
            "duck_"
        ):
            event = action.split(
                "duck_",
                1,
            )[1]

            if event not in {
                "start",
                "downloading",
                "failed",
                "complete",
            }:
                bot.reply_to(
                    message,
                    "Invalid duck reaction category.",
                )
                return

            if message.sticker:
                media_type = "sticker"
                file_id = (
                    message.sticker.file_id
                )

            elif message.animation:
                media_type = "animation"
                file_id = (
                    message.animation.file_id
                )

            else:
                _pending_action[
                    user_id
                ] = action

                bot.reply_to(
                    message,
                    t[
                        "adm_duck_invalid_media"
                    ],
                )
                return

            store.save_duck_reaction(
                event,
                media_type,
                file_id,
            )

            reactions = (
                store.load_duck_reactions()
            )

            count = len(
                reactions.get(
                    event,
                    [],
                )
            )

            event_names = {
                "start": t["adm_duck_start"],
                "downloading": t["adm_duck_downloading"],
                "failed": t["adm_duck_failed"],
                "complete": t["adm_duck_complete"],
            }

            bot.reply_to(
                message,
                t["adm_duck_saved"].format(
                    event=event_names.get(
                        event,
                        event,
                    )
                )
                + f"\n📦 Total: {count}",
            )

            return

        # -----------------------------------------------------------
        # Existing text-based admin actions
        # -----------------------------------------------------------

        text = (
            message.text
            or ""
        ).strip()

        if text.startswith("/"):
            bot.reply_to(
                message,
                t["adm_cancelled"],
            )
            return

        if action == "setad":
            _set_house_ad_text(
                user_id,
                text,
            )

            bot.reply_to(
                message,
                (
                    t["setad_done"]
                    if text
                    else t["setad_cleared"]
                ),
            )

        elif action == "addsponsor":
            parts = text.split(
                maxsplit=1
            )

            if len(parts) < 2:
                bot.reply_to(
                    message,
                    t["addsponsor_usage"],
                )
            else:
                ads.add_sponsor_channel(
                    parts[0],
                    parts[1],
                )

                bot.reply_to(
                    message,
                    t["addsponsor_done"].format(
                        name=parts[1]
                    ),
                )

                bot.reply_to(
                    message,
                    t["addsponsor_reminder"],
                )

        elif action == "removesponsor":
            found = ads.remove_sponsor_channel(
                text
            )

            bot.reply_to(
                message,
                (
                    t["removesponsor_done"]
                    if found
                    else t["removesponsor_not_found"]
                ),
            )

        elif action == "ban":
            if text.isdigit():
                store.ban_user(
                    int(text)
                )

                bot.reply_to(
                    message,
                    t["ban_done"].format(
                        id=text
                    ),
                )
            else:
                bot.reply_to(
                    message,
                    t["ban_usage"].format(
                        cmd="ban"
                    ),
                )

        elif action == "unban":
            if (
                text.isdigit()
                and store.unban_user(
                    int(text)
                )
            ):
                bot.reply_to(
                    message,
                    t["unban_done"].format(
                        id=text
                    ),
                )
            else:
                bot.reply_to(
                    message,
                    t["unban_not_found"],
                )
        elif action == 'exempt':
            if text.isdigit():
                store.add_exempt_user(text)
                bot.reply_to(message, f"✅ {text} از دروازه‌ی عضویت معاف شد.")
        elif action == 'unexempt':
            if text.isdigit() and store.remove_exempt_user(text):
                bot.reply_to(message, f"✅ معافیت {text} برداشته شد.")

        elif action == "broadcast":
            sent, failed = _broadcast(bot, text)

            bot.reply_to(
                message,
                t["broadcast_done"].format(
                    sent=sent,
                    failed=failed,
                ),
            )