"""
Telegram bot entrypoint for @DropShotDLBot.

Setup
-----
    pip install pyTelegramBotAPI yt-dlp spotipy python-dotenv mutagen

ffmpeg must also be installed and on your PATH (needed to convert the
YouTube audio matched for Spotify tracks, and for "audio only" quality, into
mp3):
    Windows:  winget install ffmpeg      (or download from ffmpeg.org)
    macOS:    brew install ffmpeg
    Linux:    apt install ffmpeg

If you still get "ffprobe and ffmpeg not found" after installing: close and
reopen your terminal (PATH changes don't apply to a terminal that was
already open — same issue as the BOT_TOKEN/setx step earlier). If it still
doesn't find it, set FFMPEG_LOCATION in .env to ffmpeg's folder (find it
with `where ffmpeg` in a new terminal) as a PATH-independent fallback.

Put your credentials in a `.env` file in this same folder — a template is
included, just open it and fill in your own values:

    BOT_TOKEN=...                # from @BotFather
    OWNER_ID=...                 # your own Telegram user ID — DM the bot /whoami to get it
    SPOTIFY_CLIENT_ID=...        # from https://developer.spotify.com/dashboard
    SPOTIFY_CLIENT_SECRET=...
    FFMPEG_LOCATION=...          # optional — only needed if ffmpeg isn't found on PATH

Admin commands (work only for OWNER_ID)
-----------------------------------------
    /lock <platform>       e.g. /lock youtube
    /unlock <platform>     e.g. /unlock youtube
    /toggle auto_quality_fallback
    /stats
    /broadcast <message>
    /ban <user_id>  /unban <user_id>
    /whoami                anyone can run this — shows your own Telegram user ID
"""

from dotenv import load_dotenv

load_dotenv()  # must run before importing bot_features/admin, which read env vars at import time

import logging
import os

import telebot
from telebot import apihelper

import bot_features

BOT_TOKEN = os.environ["BOT_TOKEN"]  # set in .env — see the setup notes above

apihelper.API_URL = "http://127.0.0.1:8081/bot{0}/{1}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN)
bot_features.register_features(bot)

if __name__ == "__main__":
    bot.infinity_polling()
