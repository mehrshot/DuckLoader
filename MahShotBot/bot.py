"""
Telegram bot entrypoint for @DropShotDLBot.

Setup
-----
    pip install pyTelegramBotAPI yt-dlp yt-dlp-ejs spotipy python-dotenv mutagen

ffmpeg must also be installed and on your PATH (needed to convert the
YouTube audio matched for Spotify tracks, and for "audio only" quality, into
mp3):
    Windows:  winget install ffmpeg      (or download from ffmpeg.org)
    macOS:    brew install ffmpeg
    Linux:    apt install ffmpeg

Since yt-dlp 2025.11, YouTube also requires an external JavaScript runtime
to solve its playback challenges — without one, extraction can still work
but the actual download often fails with "HTTP Error 403: Forbidden" even
though the title/thumbnail/format list came through fine. Deno is the
recommended runtime:
    Linux/macOS:  curl -fsSL https://deno.land/install.sh | sh
    Windows:      winget install --id=DenoLand.Deno
Then make sure yt-dlp-ejs is installed too (pip install -U yt-dlp yt-dlp-ejs)
— it ships the actual challenge-solving scripts that Deno runs.

The bot logs whether it found ffmpeg and a JS runtime once at startup
(check `journalctl -u dropshotdl` right after a restart) rather than
leaving you to guess from a downstream error.

If you still get "ffprobe and ffmpeg not found" after installing it: this
is almost always a PATH mismatch between your SSH shell and the systemd
service's own environment, not a missing install. Run `which ffmpeg` over
SSH, then set FFMPEG_LOCATION in .env to the folder it's in (e.g.
/usr/bin) — see the startup log line for exactly what this process itself
sees on its PATH.

Put your credentials in a `.env` file in this same folder — a template is
included, just open it and fill in your own values:

    BOT_TOKEN=...                # from @BotFather
    OWNER_ID=...                 # your own Telegram user ID — DM the bot /whoami to get it
    SPOTIFY_CLIENT_ID=...        # from https://developer.spotify.com/dashboard
    SPOTIFY_CLIENT_SECRET=...
    FFMPEG_LOCATION=...          # optional — only needed if ffmpeg isn't found on PATH

ROTATING_PROXIES (optional): one or more proxy URLs, comma-separated, e.g.
a local rotating SOCKS5 gateway. Used only for lightweight probing/search
calls, not for the actual file transfer — see the docstring on
_get_random_proxy() in platforms.py for why.

Admin commands (work only for OWNER_ID)
-----------------------------------------
    /settings               shows a button-based admin panel instead of the
                             normal user settings — manages everything below
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
import platforms

BOT_TOKEN = os.environ["BOT_TOKEN"]  # set in .env — see the setup notes above

apihelper.API_URL = "http://127.0.0.1:8081/bot{0}/{1}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

platforms.cleanup_stray_downloads()
platforms.check_dependencies()

bot = telebot.TeleBot(BOT_TOKEN)
bot_features.register_features(bot)

if __name__ == "__main__":
    bot.infinity_polling()