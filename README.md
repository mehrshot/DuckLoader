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

Instagram, TikTok, YouTube, and SoundCloud use `yt-dlp` for extraction. Instagram photo posts, photo carousels, photo/video stories and highlight share links (`instagram.com/s/...`) are also supported.

Spotify is handled through its Web API for track metadata. The matching audio is searched on YouTube Music first (it carries the official label-supplied recordings), then YouTube, then SoundCloud. Every candidate is scored on title, artists and duration; covers, remixes, live/sped-up/instrumental versions and same-titled songs by other artists are rejected, and the downloaded file's real length is verified before it is sent. Spotify's protected streams are never touched. Protected (DRM / Go+ preview-only) SoundCloud tracks use the same matcher to find the song elsewhere.

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

#### Post-download ad campaigns

An approved post-download ad runs as a campaign with impression and click tracking (stored in `ad_stats.json`):

- **Quota** — the number in the request's duration field (e.g. `1000`, `۱۰۰۰ نمایش`, `2k`) is the impression quota. When it is reached the campaign stops and the owner gets the final report.
- **Frequency rules** — no ad if the user saw any ad in the last 15 minutes; the same campaign at most 3 times per user, at least 12 hours apart; never again after the user clicks it. Users who have seen a campaign least, and campaigns furthest behind their quota, go first.
- **Banner** — the owner sends or forwards a photo / video / GIF post with caption (or a text post) and it is sent to users exactly like that, formatting included. Without a banner a short "📣 Ad" message with the button is sent.
- **Button or caption link** — per campaign, the owner chooses a "join the channel" button under the banner (clicks are counted: the first tap is recorded and the button turns into the channel link) or no button, with the channel written in the caption (clicks can't be counted). The button link and text can be changed from the panel.
- **Approval** — an approved post-download ad starts paused, so the banner can be set and previewed before pressing ▶️.
- **Global ad** — the owner's own ad (`/setad`, or panel → Ads → 🖼 Global ad) is a campaign too, with a banner, the same frequency rules and a report, but no quota; paid campaigns are always shown first.
- **Clicks** — the ad button is counted on the first tap and then turns into the channel link.
- **Report** — impressions vs. quota, unique viewers, clicks, unique clickers, CTR and a daily breakdown. From the admin panel the owner can pause/resume, raise the quota, and send the report to the advertiser.

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
| `/feedback` | Suggest a feature or report a problem (also the 💬 button under the chat) |

Feedback goes straight to the owner with the user's name, ID, language, start-link source and — for problem reports — that user's latest errors from the error log. Screenshots, videos and voice notes are forwarded as sent. The owner's copy has a **💬 Reply to user** button; the reply is delivered to the user by the bot. Everything is also kept in `feedback.json`. Users can send at most 5 messages per hour.

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
| `/setad <message>` | Set the global ad as a text banner (picture banners: panel → Ads → 🖼 Global ad) |
| `/checksponsor <channel>` | Check bot access to a sponsor channel |
| `/addsponsor <channel> <name>` | Add a sponsor channel |
| `/removesponsor <channel>` | Remove a sponsor channel |
| `/sponsors` | List sponsor channels |
| `/senddl <user_id> <link> [<link> ...]` | Download link(s) and deliver them to a user in the normal format (e.g. to make up for failed requests) |
| `/setcache` | Connect the private cache channel used by inline mode (then forward any message from that channel to the bot) |

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

Review pending requests and approve, reject, or contact advertisers. **📊 Post-download ad reports** lists every campaign with its progress and opens its report.

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

`ytmusicapi` (in `requirements.txt`) is what makes Spotify matching accurate; the startup log warns if it is missing.

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
| `INSTAGRAM_COOKIES_PROFILE` | No | Browser profile (name or path) for `INSTAGRAM_COOKIES_BROWSER` |
| `TIKTOK_COOKIE_FILE` / `TIKTOK_COOKIES_BROWSER` | No | Logged-in TikTok cookies (age-restricted videos) |
| `YTDLP_PLAYER_CLIENT` | No | Override YouTube player clients |
| `BOT_WORKER_THREADS` | No | Telegram handler threads (default 96) |
| `MAX_CONCURRENT_DOWNLOADS` | No | Downloads running at once, bot-wide (default 4) |
| `INSTAGRAM_MAX_CONCURRENT` | No | Instagram downloads running at once (default 3) |
| `MAX_QUEUE_WAITING` | No | Requests allowed to wait in the queue (default 40) |
| `MEDIA_CACHE_TTL_HOURS` | No | How long an uploaded file is re-sent by file_id for the same link (default 72) |
| `SPOTIFY_MAX_TRACKS` | No | Max tracks fetched from one album/playlist link (default 50) |
| `MAX_PLAYLIST_ENTRIES` | No | Max items from one link, e.g. a highlight or SoundCloud set (default 30) |

### Handling traffic spikes

- A link that was already downloaded (same media, same quality) is re-sent instantly by its Telegram `file_id` for `MEDIA_CACHE_TTL_HOURS`. When many people send the same link at the same moment, only the first one downloads it; the rest wait and get the uploaded copy.
- Sending the same link again while it is still downloading is ignored with a short notice.
- Queued users see their position in the queue.

### Groups

Add the bot to a group and it downloads every supported link members send — quietly: no progress messages, duck stickers, sponsor gate or ads. A 👀 reaction on the link means "working on it", the file is sent as a reply to the link, and 🤷 means it failed. One download runs at a time per group, and Spotify albums/playlists are redirected to the private chat.

YouTube shows the quality picker in groups too, but only the person who sent the link can use it. If the bot is a group admin, the picker is sent as an *ephemeral* message (Bot API 10.2+) that only that person sees; otherwise it's a normal message that ignores everyone else's taps.

With privacy mode on, a bot only sees plain links in groups where it is an admin — the bot says so when it's added. Set `/setprivacy` → Disable in @BotFather to make it work everywhere.

### Inline mode

In any chat, type `@DuckDownloader_Bot <link>`:

- Media that was already downloaded is offered instantly from the cache.
- Otherwise a "⏳" placeholder is sent; the bot downloads the file, uploads it to the private cache channel (an inline message can only be turned into media that's already on Telegram) and replaces the placeholder with the file.
- YouTube: the sent message becomes a quality picker right in that chat — only the sender's taps count — and then turns into the video.
- Users who haven't joined the sponsor channels get a button that takes them to the bot first.
- Nobody has to remember the username: files downloaded in the private chat have a **📤 Send to your friends** button (opens the chat picker with `@DuckDownloader_Bot <link>` already typed), and every inline file has a **🦆 Download another link** button that types `@DuckDownloader_Bot ` into the current chat. Telegram also lists recently used inline bots as soon as you type `@`.

Setup: in @BotFather enable `/setinline` and set `/setinlinefeedback` to 100%; create a private channel, make the bot an admin, then send `/setcache` to the bot and forward any message from that channel (or set `CACHE_CHAT_ID` in `.env`).

### Campaign links

Give every ad campaign its own start link, e.g. `https://t.me/DuckDownloader_Bot?start=insta_ali`. New users who arrive through it are counted per source, and `/stats` (or 📊 in the admin panel) shows the last 7 days of new users, active users and downloads — the numbers advertisers ask for.

`INSTAGRAM_COOKIE_FILE` defaults to `instagram_cookies.txt` in the bot folder when it exists. If both a browser and a cookie file are configured, both are tried in turn. Cookie files are only ever read — the bot works on a temporary copy, so an Instagram hiccup can no longer wipe the saved session out of the file.

When Instagram cookies stop working (expired session, logged-out responses), the owner receives a Telegram alert (at most once every 6 hours).

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
