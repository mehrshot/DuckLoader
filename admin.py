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
import time

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ForceReply

import ads
import platforms
import store

TOGGLE_KEYS = {"auto_quality_fallback", "sponsor_message"}  # bot-wide behavior flags, not platforms

# user_id -> which text-input action a /settings panel button is waiting on
_pending_action = {}


def is_owner(user_id) -> bool:
    owner_id = int(os.environ.get("OWNER_ID", "0") or "0")
    return owner_id != 0 and user_id == owner_id


def has_pending_action(user_id) -> bool:
    return user_id in _pending_action


# --- admin panel (shown to the owner instead of the normal /settings menu) ---

def _panel_markup(t) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=2)
    m.add(
        InlineKeyboardButton(t['adm_platforms'], callback_data='adm_menu_platforms'),
        InlineKeyboardButton(t['adm_toggles'], callback_data='adm_menu_toggles'),
    )
    m.add(
        InlineKeyboardButton(t['adm_ads'], callback_data='adm_menu_ads'),
        InlineKeyboardButton(t['adm_users'], callback_data='adm_menu_users'),
    )
    m.add(InlineKeyboardButton(t['adm_stats'], callback_data='adm_menu_stats'))
    m.add(InlineKeyboardButton(t['adm_mysettings'], callback_data='adm_menu_mysettings'))
    return m


def _platforms_markup(flags, t) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    for key, name in platforms.PLATFORM_NAMES.items():
        icon = "🔓" if flags.get(key, True) else "🔒"
        m.add(InlineKeyboardButton(f"{icon} {name}", callback_data=f"adm_lock_{key}"))
    m.add(InlineKeyboardButton(t['back'], callback_data='adm_menu_main'))
    return m


def _toggles_markup(flags, t) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    for key in sorted(TOGGLE_KEYS):
        icon = "✅" if flags.get(key, False) else "❌"
        m.add(InlineKeyboardButton(f"{icon} {key}", callback_data=f"adm_toggle_{key}"))
    m.add(InlineKeyboardButton(t['back'], callback_data='adm_menu_main'))
    return m


def _ads_markup(t) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    m.add(InlineKeyboardButton(t['adm_setad_btn'], callback_data='adm_ask_setad'))
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
    m.add(InlineKeyboardButton(t['back'], callback_data='adm_menu_main'))
    return m


def _back_markup(target, t) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton(t['back'], callback_data=target))
    return m


def build_panel(t):
    """Returns (text, markup) for the admin panel's home screen."""
    return t['adm_title'], _panel_markup(t)


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

        stats = store.load_stats()
        users = store.load_known_users()

        lines = [f"👥 {t['stats_users']}: {len(users)}"]
        downloads = stats.get("downloads", {})
        if downloads:
            for platform, count in sorted(downloads.items(), key=lambda kv: -kv[1]):
                lines.append(f"  • {platform}: {count}")
        lines.append(f"❌ {t['stats_errors']}: {stats.get('errors', 0)}")
        bot.reply_to(message, "\n".join(lines))

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

        sent, failed = 0, 0
        for chat_id in store.load_known_users():
            try:
                bot.send_message(chat_id, text)
                sent += 1
            except Exception:
                failed += 1
            time.sleep(0.05)  # stay well under Telegram's rate limits on a large broadcast

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
        ads.save_ad_message(text)
        bot.reply_to(message, t['setad_done'] if text else t['setad_cleared'])

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
            bot.edit_message_text(text, chat_id_int, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

        if data == 'adm_menu_main':
            edit(t['adm_title'], _panel_markup(t))
        elif data == 'adm_menu_platforms':
            edit(t['adm_platforms_title'], _platforms_markup(flags, t))
        elif data == 'adm_menu_toggles':
            edit(t['adm_toggles_title'], _toggles_markup(flags, t))
        elif data == 'adm_menu_ads':
            edit(t['adm_ads_title'], _ads_markup(t))
        elif data == 'adm_menu_users':
            edit(t['adm_users_title'], _users_markup(t))
        elif data == 'adm_menu_mysettings':
            text, markup = my_settings_view(chat_id_int)
            edit(text, markup)
        elif data == 'adm_menu_stats':
            stats = store.load_stats()
            users = store.load_known_users()
            lines = [f"👥 {t['stats_users']}: {len(users)}"]
            for p, c in sorted(stats.get('downloads', {}).items(), key=lambda kv: -kv[1]):
                lines.append(f"  • {p}: {c}")
            lines.append(f"❌ {t['stats_errors']}: {stats.get('errors', 0)}")
            edit("\n".join(lines), _back_markup('adm_menu_main', t))
        elif data == 'adm_sponsors_list':
            channels = ads.load_sponsor_channels()
            text = "\n".join(f"• {c['username']} — {c['name']}" for c in channels) if channels else t['sponsors_empty']
            edit(text, _back_markup('adm_menu_ads', t))
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

    @bot.message_handler(func=lambda msg: msg.from_user is not None and msg.from_user.id in _pending_action)
    def handle_admin_reply(message):
        user_id = message.from_user.id
        action = _pending_action.pop(user_id, None)
        if not action or not is_owner(user_id):
            return

        t = texts_for(message.chat.id)
        text = (message.text or "").strip()

        if text.startswith('/'):
            bot.reply_to(message, t['adm_cancelled'])
            return

        if action == 'setad':
            ads.save_ad_message(text)
            bot.reply_to(message, t['setad_done'] if text else t['setad_cleared'])

        elif action == 'addsponsor':
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                bot.reply_to(message, t['addsponsor_usage'])
            else:
                ads.add_sponsor_channel(parts[0], parts[1])
                bot.reply_to(message, t['addsponsor_done'].format(name=parts[1]))
                bot.reply_to(message, t['addsponsor_reminder'])

        elif action == 'removesponsor':
            found = ads.remove_sponsor_channel(text)
            bot.reply_to(message, t['removesponsor_done'] if found else t['removesponsor_not_found'])

        elif action == 'ban':
            if text.isdigit():
                store.ban_user(int(text))
                bot.reply_to(message, t['ban_done'].format(id=text))
            else:
                bot.reply_to(message, t['ban_usage'].format(cmd='ban'))

        elif action == 'unban':
            if text.isdigit() and store.unban_user(int(text)):
                bot.reply_to(message, t['unban_done'].format(id=text))
            else:
                bot.reply_to(message, t['unban_not_found'])

        elif action == 'broadcast':
            sent, failed = 0, 0
            for chat_id in store.load_known_users():
                try:
                    bot.send_message(chat_id, text)
                    sent += 1
                except Exception:
                    failed += 1
                time.sleep(0.05)
            bot.reply_to(message, t['broadcast_done'].format(sent=sent, failed=failed))
