# 🦆 DuckLoader

> A friendly Telegram media downloader for Instagram, TikTok, YouTube, SoundCloud, and Spotify.

DuckLoader turns supported links into downloadable media inside Telegram. It combines a simple user experience with per-user settings, a full owner-only admin panel, sponsor-channel monetization, advertising requests, download protection, error logging, and configurable Duck reactions.

## ✨ Features

### 📥 Supported platforms

- Instagram
- TikTok
- YouTube
- SoundCloud
- Spotify

Instagram, TikTok, YouTube, and SoundCloud use `yt-dlp` for extraction. Spotify is handled through its Web API for track metadata, with matching audio located through SoundCloud first and YouTube as a fallback rather than downloading Spotify's protected streams.

### 🎬 Download quality

Users can choose their default quality in `/settings`:

- **Best**
- **720p**
- **Audio only**

YouTube also has a per-video quality picker that inspects available formats and presents suitable resolutions. The bot can automatically reduce quality when a result would otherwise exceed the configured Telegram upload limit.

### ⚙️ Personal settings

Each user can configure:

- 🇮🇷 Persian or 🇺🇸 English interface
- Best, 720p, or audio-only download quality

### 🦆 Duck reactions

The owner can configure multiple Telegram stickers or GIF/Animation reactions for:

- Start
- Downloading
- Failed
- Complete

### 🔒 Sponsor-channel membership gate

DuckLoader can require users to join configured sponsor channels before downloading.

When enabled:

1. A supported link is received.
2. Sponsor membership is checked.
3. Users who are not members see a join button for each remaining sponsor channel.
4. **✅ Check Membership & Continue** rechecks membership without asking the user to resend the link.
5. The keyboard is updated so only channels the user still needs to join remain visible.
6. Once all memberships are confirmed, the original download resumes automatically.

The owner can temporarily disable the entire membership requirement or exempt individual users.

For a sponsor channel to be enforceable, the bot should be an administrator and Telegram must allow the bot to inspect membership. If Telegram refuses a membership check, that channel is skipped and the user is allowed to continue.

### 📣 Advertising

DuckLoader has two advertising systems.

#### Global advertisement

The owner can configure one global post-download advertisement and enable or disable it independently.

#### User advertising requests

Users can submit advertising requests with `/adrequest` or through the optional advertisement button.

The form starts in Persian and can be switched to English from inside the form. Advertisers choose between:

- **📢 Sponsor Channel** — users must join the advertiser's channel before using the downloader, when the sponsor gate is enabled.
- **📣 Post-Download Ad** — the advertiser's promotion is shown after successful downloads.

Requests are sent to the owner immediately for review and remain available in the admin panel. The owner can approve, reject, or contact the advertiser.

### 👥 User management

The owner can:

- Ban and unban users
- Exempt users from the sponsor membership requirement
- Remove exemptions
- Broadcast messages to known users

### 📊 Statistics and diagnostics

The admin panel includes:

- Total known users
- Download counts by platform
- Recent download errors
- Error details including time, platform, user, link, and message
- Sponsor-channel tools

`/checksponsor` is owner-only and can be used to inspect whether the bot can check membership in a sponsor channel.

## 🤖 User commands

| Command | Description |
|---|---|
| `/start` | Start or initialize DuckLoader |
| `/help` | Show the bot's main information |
| `/settings` | Choose language and download quality |
| `/whoami` | Show your Telegram user ID |
| `/adrequest` | Submit an advertising request |

For normal downloading, users simply send a supported URL.

## 🛠 Owner commands

These commands work only for the user configured as `OWNER_ID`.

| Command | Description |
|---|---|
| `/lock <platform>` | Disable a platform |
| `/unlock <platform>` | Enable a platform |
| `/toggle <setting>` | Toggle a bot-wide setting |
| `/stats` | Show bot statistics |
| `/broadcast <message>` | Broadcast a message to known users |
| `/ban <user_id>` | Ban a user |
| `/unban <user_id>` | Unban a user |
| `/setad <message>` | Set the global advertisement text |
| `/checksponsor <channel>` | Check bot access to a sponsor channel |
| `/addsponsor <channel> <name>` | Add a sponsor channel |
| `/removesponsor <channel>` | Remove a sponsor channel |
| `/sponsors` | List sponsor channels |

The owner can also access these functions through the button-based admin panel from `/settings`.

## 🛠 Admin panel

The current admin panel provides:

### 🔒 Platforms
Enable or disable downloading for each supported platform.

### ⚙️ General settings

- Auto quality fallback
- Global sponsor advertisement
- Advertisement-request button
- Sponsor-channel membership requirement

### 📢 Ads

- Set the global advertisement
- Add sponsor channels
- Remove sponsor channels
- View sponsor channels

### 📨 Advertising requests

Review pending requests and approve, reject, or contact advertisers.

### 👥 Users

- Ban
- Unban
- Broadcast
- Exempt from sponsor membership
- Remove sponsor exemption

### 📊 Statistics

View user totals, platform download counts, and recorded errors.

### 🚨 Download errors

Inspect recent errors with platform, user ID, source link, time, and the recorded error message.

### 🦆 Duck reactions

Configure and clear reactions for start, downloading, failure, and completion. Multiple reactions can be stored for every stage.

### 🌐 My own settings

The owner can still use the normal user language and quality settings.

### 📚 Admin commands

A built-in reference page lists the available owner commands.

## 🚀 Installation

### Requirements

- Python 3.12+ recommended
- FFmpeg
- Deno or another supported JavaScript runtime for full YouTube challenge solving
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Telegram user ID for `OWNER_ID`
- Spotify API credentials if Spotify support is needed

Install Python dependencies:

```bash
python -m pip install -r requirements.txt
```

Install FFmpeg on Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y ffmpeg
```

Install Deno:

```bash
curl -fsSL https://deno.land/install.sh | sh
```

## 🔐 Configuration

Create `.env` in the project root:

```env
BOT_TOKEN=your_telegram_bot_token
OWNER_ID=your_telegram_user_id

SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret

# Optional: explicit FFmpeg directory when it is not visible on PATH
FFMPEG_LOCATION=/usr/bin

# Optional: comma-separated proxy URLs for lightweight probing/search calls
ROTATING_PROXIES=socks5://127.0.0.1:9050,socks5://127.0.0.1:9051
```

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | Yes | Telegram Bot API token |
| `OWNER_ID` | Yes | Telegram user ID of the owner |
| `SPOTIFY_CLIENT_ID` | Spotify | Spotify application client ID |
| `SPOTIFY_CLIENT_SECRET` | Spotify | Spotify application client secret |
| `FFMPEG_LOCATION` | No | Explicit FFmpeg directory |
| `ROTATING_PROXIES` | No | Proxy URLs for lightweight probing/search |
| `MAX_UPLOAD_MB` | No | Upload limit override |
| `YOUTUBE_COOKIE_FILE` | No | YouTube cookie-file path |
| `INSTAGRAM_COOKIE_FILE` | No | Instagram cookie-file path |
| `YTDLP_COOKIES_BROWSER` | No | Browser source for YouTube cookies |
| `INSTAGRAM_COOKIES_BROWSER` | No | Browser source for Instagram cookies |
| `YTDLP_PLAYER_CLIENT` | No | Override YouTube player clients |

## 🍪 Cookies

Authenticated cookies can be used when a platform requires them, especially for some Instagram and YouTube content.

Typical files are:

```text
cookies.txt
instagram_cookies.txt
```

Treat cookie files as credentials. Keep them on the deployment machine and never commit them to Git.

## ▶️ Running

Run the bot directly with:

```bash
python bot.py
```

At startup the bot checks for FFmpeg and an external JavaScript runtime and logs what the running process can actually see.

## 🖥️ systemd deployment

A production VPS can run DuckLoader through `systemd`.

Example:

```ini
[Unit]
Description=DuckLoader Telegram Bot
After=network.target

[Service]
Type=simple
User=mahshot
WorkingDirectory=/home/mahshot/DuckLoaderBot
ExecStart=/home/mahshot/venv/bin/python /home/mahshot/DuckLoaderBot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart duckloader
sudo systemctl status duckloader
```

View live logs with:

```bash
sudo journalctl -u duckloader -f
```

## 🗂️ Project structure

```text
DuckLoader/
├── bot.py
├── bot_features.py
├── admin.py
├── ads.py
├── platforms.py
├── store.py
├── requirements.txt
└── .gitignore
```

### `bot.py`
Application entry point. Loads environment variables, configures the Telegram API, performs startup dependency checks, registers features, and starts polling.

### `bot_features.py`
Contains the main Telegram user experience: commands, settings, URL handling, sponsor checks, ad requests, download callbacks, progress messages, uploads, and Duck reactions.

### `admin.py`
Contains owner-only commands and the button-based management panel.

### `ads.py`
Handles sponsor-channel normalization, sponsor-channel storage, membership checks, and the global advertisement message.

### `platforms.py`
Handles platform detection, yt-dlp extraction/downloads, YouTube quality probing, authentication settings, Spotify matching, media classification, file-size handling, and cleanup.

### `store.py`
Stores persistent state in small JSON files: users, settings, feature flags, bans, statistics, errors, advertising requests, Duck reactions, and sponsor exemptions.

## 🔄 Platform and setting defaults

| Platform / setting | Default |
|---|---:|
| Instagram | ✅ Enabled |
| TikTok | ✅ Enabled |
| YouTube | ❌ Disabled |
| SoundCloud | ✅ Enabled |
| Spotify | ✅ Enabled |
| Auto quality fallback | ❌ Disabled |
| Advertisement button | ✅ Enabled |
| Sponsor membership requirement | ✅ Enabled |

The owner can change runtime settings from the admin panel.

## 📣 Sponsor-channel setup

To enforce sponsor-channel membership:

1. Add the channel with the admin panel or `/addsponsor`.
2. Make DuckLoader an administrator in the channel.
3. Run `/checksponsor @ChannelUsername` from the owner account.
4. Confirm that Telegram allows the bot to check membership.
5. Leave **Sponsor Channel Membership Requirement** enabled.

Example:

```text
/addsponsor @MyChannel My Channel
```

Then:

```text
/checksponsor @MyChannel
```

If Telegram refuses a membership check, that channel is skipped and the user can continue. This is intentional behavior.

## 📢 Advertising workflow

### Advertiser

```text
/adrequest
    ↓
Channel
    ↓
Display name
    ↓
Advertisement type
    ↓
Duration / displays
    ↓
Notes
    ↓
Confirmation
    ↓
Submit
```

The request form starts in Persian and can be switched to English.

### Owner

The owner receives a newly submitted request immediately and can approve, reject, or contact the advertiser.

Approved Sponsor Channel requests become active sponsor channels. Approved Post-Download requests are eligible to appear after successful downloads.

## 🛡️ Security and repository hygiene

The repository should contain source code and documentation, not secrets or runtime state.

Keep these out of Git:

```text
.env
cookies.txt
instagram_cookies.txt
*.pem
*.key
*.json
*.backup
.cache/
downloads/
venv/
```

Runtime JSON files should live on the deployment machine and be backed up separately from the Git repository.

If credentials or cookies are ever accidentally committed to a public repository, treat them as exposed and rotate/revoke them as appropriate.

## ⚠️ Third-party platform changes

DuckLoader depends on external services and extractors. Platforms can change their web pages, authentication, anti-bot protection, and APIs without notice.

When a platform stops working:

1. Update `yt-dlp` and related dependencies.
2. Check the startup logs for dependency problems.
3. Check the relevant yt-dlp extractor status.
4. Check authentication/cookie validity when applicable.

TikTok, Instagram, YouTube, and other services may require different fixes when their upstream behavior changes.

## 🧹 Maintenance

Use a deliberate Git workflow and review staged changes before committing:

```bash
git status
git add bot.py bot_features.py admin.py ads.py platforms.py store.py requirements.txt .gitignore README.md
git diff --cached
git commit -m "Describe the change"
git push
```

Avoid committing generated downloads, runtime JSON, cookies, local environments, or backup copies.

## 📜 License

No license is currently declared in this repository. If you plan to distribute DuckLoader as open source, add an explicit license before presenting it as an open-source project.

## 🦆 DuckLoader

**You bring the link. DuckLoader brings the media.** 🦆
