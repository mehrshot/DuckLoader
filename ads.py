"""
Monetization: a post-download sponsor message (tier 1), and an optional
"join our sponsor channel(s) to use this bot" gate (tier 2).

Tier 2 is enforced only when Telegram successfully lets the bot check
membership. If Telegram refuses the membership check, that channel is
ignored so the user is allowed to continue.
"""

import json
import logging
import os
import re

CHANNELS_FILE = "sponsor_channels.json"
AD_FILE = "ad_message.json"

logger = logging.getLogger(__name__)


def _load(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def _save(path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=4,
        )


# ---------------------------------------------------------------------------
# Sponsor-channel normalization
# ---------------------------------------------------------------------------

def normalize_sponsor_channel(username: str) -> str:
    """
    Converts the common public-Telegram-channel formats into a value that
    Telegram's get_chat_member() can use:

        @channel
        channel
        t.me/channel
        https://t.me/channel
        http://t.me/channel

    Numeric Telegram chat IDs such as -1001234567890 are also preserved.

    Invite links such as https://t.me/+abcdef are not converted into a
    get_chat_member() target because Telegram cannot use an invite URL as
    the chat identifier for this check.
    """
    value = str(
        username or ""
    ).strip()

    if not value:
        return ""

    # Already a numeric Telegram chat ID.
    if re.fullmatch(
        r"-?\d+",
        value,
    ):
        return value

    value = value.strip()

    # Remove Telegram URL prefixes.
    value = re.sub(
        r"^https?://(?:www\.)?t\.me/",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"^t\.me/",
        "",
        value,
        flags=re.IGNORECASE,
    )

    # Invite links cannot be used as a chat identifier.
    if value.startswith("+"):
        return ""

    # Remove query/fragment if someone sent a normal channel URL with one.
    value = value.split(
        "?",
        1,
    )[0]

    value = value.split(
        "#",
        1,
    )[0]

    value = value.strip().strip("/")

    if not value:
        return ""

    if not value.startswith("@"):
        value = "@" + value

    return value


# ---------------------------------------------------------------------------
# Sponsor-channel list
# ---------------------------------------------------------------------------

def load_sponsor_channels() -> list:
    raw_channels = _load(
        CHANNELS_FILE,
        [],
    )

    if not isinstance(
        raw_channels,
        list,
    ):
        return []

    normalized_channels = []

    for channel in raw_channels:
        if not isinstance(
            channel,
            dict,
        ):
            continue

        raw_username = channel.get(
            "username",
            "",
        )

        normalized_username = (
            normalize_sponsor_channel(
                raw_username
            )
        )

        if not normalized_username:
            continue

        normalized_channels.append(
            {
                "username": normalized_username,
                "name": (
                    channel.get(
                        "name",
                        "",
                    )
                    or normalized_username
                ),
            }
        )

    return normalized_channels


def save_sponsor_channels(channels: list) -> None:
    _save(
        CHANNELS_FILE,
        channels,
    )


def add_sponsor_channel(
    username: str,
    name: str,
) -> bool:
    normalized_username = (
        normalize_sponsor_channel(
            username
        )
    )

    if not normalized_username:
        return False

    channels = load_sponsor_channels()

    channels = [
        channel
        for channel in channels
        if channel.get(
            "username",
            "",
        ).lower()
        != normalized_username.lower()
    ]

    channels.append(
        {
            "username": normalized_username,
            "name": (
                str(
                    name or ""
                ).strip()
                or normalized_username
            ),
        }
    )

    save_sponsor_channels(
        channels
    )

    return True


def remove_sponsor_channel(
    username: str,
) -> bool:
    normalized_username = (
        normalize_sponsor_channel(
            username
        )
    )

    if not normalized_username:
        return False

    channels = load_sponsor_channels()

    kept = [
        channel
        for channel in channels
        if channel.get(
            "username",
            "",
        ).lower()
        != normalized_username.lower()
    ]

    if len(kept) == len(channels):
        return False

    save_sponsor_channels(
        kept
    )

    return True


def get_unjoined_channels(
    bot,
    user_id,
) -> list:
    """
    Return sponsor channels the user has not joined.

    Important behavior:
    - joined/member/administrator/creator -> allowed
    - left/kicked -> blocked
    - restricted + is_member=False -> blocked
    - Telegram/API error -> channel is skipped, allowing the user to continue
      because membership could not be verified
    """
    unjoined = []

    for channel in load_sponsor_channels():
        username = channel.get(
            "username",
            "",
        )

        if not username:
            continue

        try:
            member = bot.get_chat_member(
                username,
                user_id,
            )

            status = getattr(
                member,
                "status",
                None,
            )

            if status in {
                "left",
                "kicked",
            }:
                unjoined.append(
                    channel
                )
                continue

            if (
                status == "restricted"
                and getattr(
                    member,
                    "is_member",
                    True,
                ) is False
            ):
                unjoined.append(
                    channel
                )

        except Exception as exc:
            logger.warning(
                "Could not check sponsor-channel membership | "
                "channel=%s user_id=%s error=%s",
                username,
                user_id,
                exc,
            )
            continue

    return unjoined


# ---------------------------------------------------------------------------
# Post-download sponsor message
# ---------------------------------------------------------------------------

def load_ad_message() -> str:
    return _load(
        AD_FILE,
        {},
    ).get(
        "text",
        "",
    )


def save_ad_message(
    text: str,
) -> None:
    _save(
        AD_FILE,
        {
            "text": text,
        },
    )