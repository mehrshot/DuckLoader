"""
Monetization: a post-download sponsor message (tier 1), and an optional
"join our sponsor channel(s) to use this bot" gate (tier 2).

Tier 2 is shown to users as a disclosed sponsor requirement, not a silent
technical gate — telling people why they're being asked to join is what
keeps this an ad rather than a dark pattern, and it's just as effective at
driving joins either way. The bot must be an admin in each sponsor channel,
or Telegram won't let it check membership there.

Both are backed by small JSON files the owner can edit directly, or manage
through the /addsponsor, /removesponsor, /sponsors, and /setad admin
commands in admin.py.
"""

import json
import os

CHANNELS_FILE = "sponsor_channels.json"
AD_FILE = "ad_message.json"


def _load(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def _save(path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


# --- tier 2: sponsor channel gate ---
# Each entry: {"username": "@channel", "name": "Display Name"}. An empty
# list (the default — the file doesn't need to exist at all) means no gate.

def load_sponsor_channels() -> list:
    return _load(CHANNELS_FILE, [])


def save_sponsor_channels(channels: list) -> None:
    _save(CHANNELS_FILE, channels)


def add_sponsor_channel(username: str, name: str) -> None:
    username = username if username.startswith("@") else f"@{username}"
    channels = [c for c in load_sponsor_channels() if c["username"].lower() != username.lower()]
    channels.append({"username": username, "name": name})
    save_sponsor_channels(channels)


def remove_sponsor_channel(username: str) -> bool:
    username = username if username.startswith("@") else f"@{username}"
    channels = load_sponsor_channels()
    kept = [c for c in channels if c["username"].lower() != username.lower()]
    if len(kept) == len(channels):
        return False
    save_sponsor_channels(kept)
    return True


def get_unjoined_channels(bot, user_id) -> list:
    """Checks Telegram membership for each configured sponsor channel and
    returns the ones the user hasn't joined. Skips (doesn't block the user
    over) any channel the bot can't check — e.g. if it hasn't been made an
    admin there yet, or the username was mistyped."""
    unjoined = []
    for ch in load_sponsor_channels():
        try:
            member = bot.get_chat_member(ch["username"], user_id)
            if member.status in ("left", "kicked"):
                unjoined.append(ch)
        except Exception:
            continue
    return unjoined


# --- tier 1: post-download sponsor message ---

def load_ad_message() -> str:
    return _load(AD_FILE, {}).get("text", "")


def save_ad_message(text: str) -> None:
    _save(AD_FILE, {"text": text})
