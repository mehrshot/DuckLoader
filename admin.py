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

import ads
import platforms
import store

TOGGLE_KEYS = {"auto_quality_fallback", "sponsor_message"}  # bot-wide behavior flags, not platforms


def is_owner(user_id) -> bool:
    owner_id = int(os.environ.get("OWNER_ID", "0") or "0")
    return owner_id != 0 and user_id == owner_id


def register_admin(bot, flags: dict, texts_for):
    """`texts_for(chat_id)` returns that chat's TEXTS dict so admin replies
    respect the sender's language like everything else in the bot."""

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
