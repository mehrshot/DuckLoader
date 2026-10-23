"""
Sends a broadcast written as a Telegram Rich Message (headings, lists,
collapsible blocks... see "Rich Message Formatting Options" in the Bot API
docs) to the owner as a preview, with the usual "send to N users" /
"cancel" buttons. Tapping "send" lets the running bot copy that preview to
every user, exactly like /broadcast.

The owner can't type rich messages without Telegram Premium; bots can.

    cd /home/mahshot/DuckLoaderBot
    /home/mahshot/venv/bin/python tools/rich_broadcast.py broadcasts/<file>.md
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import requests  # noqa: E402
import telebot  # noqa: E402
from telebot import apihelper  # noqa: E402

import admin  # noqa: E402

API = "http://127.0.0.1:8081/bot{0}/{1}"  # the bot's local Bot API server (bot.py)


def call(token, method, payload):
    response = requests.post(API.format(token, method), json=payload, timeout=60).json()
    if not response.get("ok"):
        raise SystemExit(f"{method} failed: {response.get('description')}")
    return response["result"]


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tools/rich_broadcast.py <markdown file>")
    markdown = open(sys.argv[1], encoding="utf-8").read().strip()
    token = os.environ["BOT_TOKEN"]
    owner = int(os.environ.get("OWNER_ID", "0") or 0)
    if not owner:
        raise SystemExit("OWNER_ID is not set.")

    # Persian text must be laid out right-to-left (headings, lists and
    # alignment); Telegram doesn't guess it for rich messages.
    rtl = len(re.findall(r"[؀-ۿ]", markdown)) > len(re.findall(r"[A-Za-z]", markdown))
    preview = call(token, "sendRichMessage", {
        "chat_id": owner, "rich_message": {"markdown": markdown, "is_rtl": rtl},
    })

    # The broadcast copies this preview to everyone: make sure a rich
    # message survives copying (a test copy, deleted right away).
    copy = call(token, "copyMessage", {"chat_id": owner, "from_chat_id": owner, "message_id": preview["message_id"]})
    call(token, "deleteMessage", {"chat_id": owner, "message_id": copy["message_id"]})

    apihelper.API_URL = API
    admin._ask_broadcast_confirm(telebot.TeleBot(token), owner, preview["message_id"])
    print(f"Preview sent to the owner (message {preview['message_id']}); confirm or cancel it in Telegram.")


if __name__ == "__main__":
    main()
