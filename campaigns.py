"""
Post-download ad campaigns (approved ad requests of type "post_download"):
which campaign a user sees and how often, impression and click counting,
the impression quota, and the report the owner (and the advertiser) reads.

Frequency rules, instead of a flat "one ad per user":
  * no ad at all if the user saw any ad in the last 15 minutes, so someone
    downloading five files in a row sees one ad, not five;
  * the same campaign at most 3 times per user, at least 12 hours apart;
  * never again to a user who already clicked it;
  * paid campaigns go before the owner's own "house" ad; among them, the
    one the user has seen least wins, then the one furthest behind its
    quota (so several campaigns finish at a similar pace).

Each campaign can have a banner (a photo / video / GIF with caption, or a
text post, sent exactly as the owner sent or forwarded it to the bot) and,
optionally, a "join the channel" button under it. The button is what makes
clicks countable; a link written in the caption is not.
"""

import logging
import random
import re
import time

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

import ads
import store

logger = logging.getLogger(__name__)

STATS_FILE = "ad_stats.json"

MIN_GAP_ANY_AD = 15 * 60
MAX_PER_USER = 3
MIN_GAP_SAME_AD = 12 * 3600

ACTIVE = "approved"
PAUSED = "paused"
COMPLETED = "completed"
CAMPAIGN_STATUSES = (ACTIVE, PAUSED, COMPLETED)

# The owner's own ad (the old global /setad text) is a campaign too, so it
# gets the same banner, frequency rules and report. It never has a quota.
HOUSE_ID = "GLOBAL"

CLICK_PREFIX = "adk_"

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "0123456789" * 2)


def parse_quota(text):
    """"1000", "۱۰۰۰ نمایش", "1,000 views", "2k" -> number of impressions.
    None when the field holds a duration ("1 week") rather than a count."""
    s = (text or "").translate(_DIGITS).lower()
    s = s.replace(",", "").replace("٬", "").replace("،", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|هزار)?", s)
    if not m:
        return None
    n = int(float(m.group(1)) * (1000 if m.group(2) else 1))
    return n if n >= 100 else None


def quota(request) -> int:
    """Impressions to deliver; 0 = no limit."""
    value = request.get("max_impressions")
    if value is None:
        value = parse_quota(request.get("duration", ""))
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def channel_url(request) -> str:
    channel = (request.get("channel") or "").strip()
    if channel.startswith(("https://t.me/", "http://t.me/")):
        return channel
    if channel.startswith("t.me/"):
        return "https://" + channel
    if channel.startswith("@") and len(channel) > 1:
        return "https://t.me/" + channel[1:]
    return ""


def display_name(request) -> str:
    if is_house(request) and not request.get("display_name"):
        return "تبلیغ عمومی"
    return (request.get("display_name") or request.get("channel") or "").strip()


def is_house(request) -> bool:
    return request.get("request_id") == HOUSE_ID


def wants_button(request) -> bool:
    return request.get("button", True) is not False


def can_show(request) -> bool:
    return bool(request.get("banner") or channel_url(request))


def button_text(request) -> str:
    return (request.get("button_text") or f"↗️ ورود به {display_name(request)}")[:60]


def ad_markup(request, tracked=True, force=False):
    """The button under the ad, or None. `tracked` makes it a callback that
    counts the click and then turns into the real link."""
    url = channel_url(request)
    if not url or not (force or wants_button(request)):
        return None
    m = InlineKeyboardMarkup()
    if tracked:
        m.add(InlineKeyboardButton(button_text(request), callback_data=CLICK_PREFIX + request["request_id"]))
    else:
        m.add(InlineKeyboardButton(button_text(request), url=url))
    return m


# --- banner ---

BANNER_LABELS = {
    "photo": "عکس",
    "video": "ویدیو",
    "animation": "گیف",
    "text": "متن",
}


def banner_from_message(message):
    """A photo / video / GIF / text message (sent or forwarded to the bot)
    -> what is needed to send it again: file_id, caption and its formatting."""
    if message.photo:
        kind, file_id = "photo", message.photo[-1].file_id
    elif message.animation:
        kind, file_id = "animation", message.animation.file_id
    elif message.video:
        kind, file_id = "video", message.video.file_id
    elif message.text:
        kind, file_id = "text", None
    else:
        return None

    if kind == "text":
        text, entities = message.text, message.entities
    else:
        text, entities = message.caption, message.caption_entities

    # Bots can't send custom (premium) emoji; the plain emoji stays in the
    # text.
    entities = [
        {k: v for k, v in e.to_dict().items() if v is not None}
        for e in (entities or [])
        if e.type != "custom_emoji"
    ]
    return {"type": kind, "file_id": file_id, "text": text or "", "entities": entities}


def send_banner(bot, chat_id, banner, reply_markup=None):
    kind = banner.get("type")
    text = banner.get("text") or None
    entities = [MessageEntity.de_json(dict(e)) for e in banner.get("entities") or []] or None

    def send(parse_mode):
        if kind == "photo":
            return bot.send_photo(chat_id, banner["file_id"], caption=text, caption_entities=entities,
                                  parse_mode=parse_mode, reply_markup=reply_markup)
        if kind == "animation":
            return bot.send_animation(chat_id, banner["file_id"], caption=text, caption_entities=entities,
                                      parse_mode=parse_mode, reply_markup=reply_markup)
        if kind == "video":
            return bot.send_video(chat_id, banner["file_id"], caption=text, caption_entities=entities,
                                  parse_mode=parse_mode, reply_markup=reply_markup, supports_streaming=True)
        return bot.send_message(chat_id, text or "📣", entities=entities,
                                parse_mode=parse_mode, reply_markup=reply_markup)

    if banner.get("markdown") and not entities:
        # The old /setad text was written in Markdown.
        try:
            return send("Markdown")
        except Exception:
            pass
    return send(None)


def send_ad(bot, chat_id, request, preview=False):
    """Sends the ad as users see it. A preview's button is the plain link,
    so the owner's own taps aren't counted."""
    banner = request.get("banner")
    markup = ad_markup(request, tracked=not preview, force=not banner)
    if banner:
        return send_banner(bot, chat_id, banner, markup)
    if markup is None:
        raise ValueError("campaign has neither a banner nor a link")
    return bot.send_message(chat_id, "📣 تبلیغ", reply_markup=markup)


def ensure_house_campaign(owner_id) -> dict:
    """Creates the owner's own campaign the first time, carrying over the old
    global ad text (and whether it was switched on)."""
    with store._ad_requests_lock:
        requests = store._load(store.AD_REQUESTS_FILE, {})
        if isinstance(requests.get(HOUSE_ID), dict):
            return requests[HOUSE_ID]

        legacy_text = (ads.load_ad_message() or "").strip()
        legacy_on = bool(store.load_flags().get("sponsor_message", False))
        now = store.iran_time()
        request = {
            "request_id": HOUSE_ID,
            "status": ACTIVE if legacy_text and legacy_on else PAUSED,
            "ad_type": "post_download",
            "user_id": owner_id,
            "telegram_username": "",
            "telegram_name": "",
            "channel": "",
            "display_name": "",
            "duration": "",
            "max_impressions": 0,
            "button": False,
            "created_at": now,
            "updated_at": now,
        }
        if legacy_text:
            request["banner"] = {"type": "text", "file_id": None, "text": legacy_text,
                                 "entities": [], "markdown": True}
        requests[HOUSE_ID] = request
        store._save(store.AD_REQUESTS_FILE, requests)
        return request


# --- storage ---

def _load() -> dict:
    data = store._load(STATS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("campaigns", {})
    data.setdefault("last_ad", {})
    return data


def _campaign(data, request_id) -> dict:
    c = data["campaigns"].setdefault(str(request_id), {})
    c.setdefault("impressions", 0)
    c.setdefault("clicks", 0)
    c.setdefault("viewers", {})   # user_id -> [times shown, last shown]
    c.setdefault("clickers", {})  # user_id -> time of first click
    c.setdefault("daily", {})     # "YYYY-MM-DD" -> [impressions, clicks]
    return c


def get_stats(request_id) -> dict:
    with store._io_lock:
        return _campaign(_load(), request_id)


def choose_and_record(user_id, requests, now=None):
    """Picks the campaign to show this user right now (or None) and counts
    the impression. Returns (request, just_completed) — just_completed is
    True for exactly one call: the one that delivers the last impression of
    the quota."""
    now = now or time.time()
    uid = str(user_id)
    with store._io_lock:
        data = _load()
        if now - data["last_ad"].get(uid, 0) < MIN_GAP_ANY_AD:
            return None, False

        candidates = []
        for request in requests:
            if request.get("status") != ACTIVE or request.get("ad_type") != "post_download":
                continue
            if not can_show(request):
                continue
            stats = _campaign(data, request["request_id"])
            limit = quota(request)
            if limit and stats["impressions"] >= limit:
                continue
            if uid in stats["clickers"]:
                continue
            seen, last_seen = stats["viewers"].get(uid, [0, 0])
            if seen >= MAX_PER_USER or (seen and now - last_seen < MIN_GAP_SAME_AD):
                continue
            progress = stats["impressions"] / (limit or 1000)
            candidates.append(((is_house(request), seen, progress, random.random()), request))

        if not candidates:
            return None, False

        request = min(candidates, key=lambda item: item[0])[1]
        stats = _campaign(data, request["request_id"])
        stats["impressions"] += 1
        seen = stats["viewers"].get(uid, [0, 0])[0]
        stats["viewers"][uid] = [seen + 1, int(now)]
        day = stats["daily"].setdefault(store.iran_time("%Y-%m-%d", now), [0, 0])
        day[0] += 1
        stats.setdefault("first_impression", int(now))
        stats["last_impression"] = int(now)

        data["last_ad"][uid] = int(now)
        data["last_ad"] = {k: v for k, v in data["last_ad"].items() if now - v < MIN_GAP_ANY_AD}

        store._save(STATS_FILE, data)

        limit = quota(request)
        return request, bool(limit and stats["impressions"] == limit)


def undo_impression(request_id, user_id) -> None:
    """The ad message could not be sent (e.g. the user blocked the bot)."""
    uid = str(user_id)
    with store._io_lock:
        data = _load()
        stats = _campaign(data, request_id)
        stats["impressions"] = max(0, stats["impressions"] - 1)
        seen, last_seen = stats["viewers"].get(uid, [0, 0])
        if seen <= 1:
            stats["viewers"].pop(uid, None)
        else:
            stats["viewers"][uid] = [seen - 1, last_seen]
        day = stats["daily"].get(store.iran_time("%Y-%m-%d"))
        if day and day[0] > 0:
            day[0] -= 1
        data["last_ad"].pop(uid, None)
        store._save(STATS_FILE, data)


def record_click(request_id, user_id) -> bool:
    """Counts a click; returns True if it is this user's first one."""
    uid = str(user_id)
    with store._io_lock:
        data = _load()
        if str(request_id) not in data["campaigns"]:
            return False
        stats = _campaign(data, request_id)
        stats["clicks"] += 1
        first = uid not in stats["clickers"]
        if first:
            stats["clickers"][uid] = int(time.time())
        day = stats["daily"].setdefault(store.iran_time("%Y-%m-%d"), [0, 0])
        day[1] += 1
        store._save(STATS_FILE, data)
        return first


def list_campaigns() -> list:
    """Post-download ads that were approved at some point, newest first."""
    return [
        r for r in store.list_ad_requests()
        if r.get("ad_type") == "post_download" and r.get("status") in CAMPAIGN_STATUSES
    ]


# --- report ---

STATUS_LABELS = {
    ACTIVE: "🟢 در حال نمایش",
    PAUSED: "⏸ متوقف شده",
    COMPLETED: "✅ تمام شده",
}


def _bar(fraction, width=10) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "▓" * filled + "░" * (width - filled)


def _pct(part, whole) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "—"


def _date(ts) -> str:
    return store.iran_time("%Y-%m-%d %H:%M", ts) if ts else "—"


def report_text(request, for_advertiser=False) -> str:
    stats = get_stats(request["request_id"])
    limit = quota(request)
    impressions = stats["impressions"]
    viewers = len(stats["viewers"])
    clickers = len(stats["clickers"])

    if is_house(request):
        lines = ["📊 گزارش تبلیغ عمومی (تبلیغ خودت)"]
    else:
        lines = [
            f"📊 گزارش تبلیغ {request['request_id']}",
            f"📢 {display_name(request)}"
            + (f" ({request.get('channel')})" if request.get("channel") and request.get("channel") != display_name(request) else ""),
        ]
    if not for_advertiser:
        lines.append(f"وضعیت: {STATUS_LABELS.get(request.get('status'), request.get('status') or '—')}")
        banner = request.get("banner")
        if banner:
            kind = BANNER_LABELS.get(banner.get("type"), "؟")
            lines.append(f"🖼 بنر: {kind}" + (" با کپشن" if banner.get("type") != "text" and banner.get("text") else ""))
        else:
            lines.append("🖼 بنر: تنظیم نشده (فقط پیام کوتاه «📣 تبلیغ» با دکمه نمایش داده میشه)")
        url = channel_url(request)
        lines.append(f"🔗 لینک دکمه: {url or 'ندارد'}")
        if not url:
            lines.append("🔘 دکمه: — (اول لینک رو تنظیم کن)")
        elif wants_button(request) or not banner:
            lines.append(f"🔘 دکمه زیر بنر: «{button_text(request)}» — کلیک‌ها شمرده میشن")
        else:
            lines.append("🔘 دکمه: ندارد (لینک توی کپشنه؛ کلیک شمرده نمیشه)")
        if not can_show(request):
            lines.append("⚠️ تا بنر یا لینک تنظیم نشه، این تبلیغ به کسی نشون داده نمیشه.")
    lines += [
        f"شروع نمایش: {_date(stats.get('first_impression'))}",
        f"آخرین نمایش: {_date(stats.get('last_impression'))}",
        "",
    ]
    if limit:
        lines += [
            f"👁 نمایش: {impressions:,} از {limit:,} ({_pct(impressions, limit)})",
            _bar(impressions / limit),
        ]
    else:
        lines.append(f"👁 نمایش: {impressions:,} (بدون سقف)")
    lines += [
        f"👤 کاربر یکتا که تبلیغ رو دیدن: {viewers:,}",
        f"🖱 کلیک: {stats['clicks']:,} ({clickers:,} نفر)",
        f"📈 نرخ کلیک (CTR): {_pct(clickers, impressions)}",
    ]

    days = sorted(stats["daily"].items())[-10:]
    if days:
        lines += ["", "📅 روزانه (نمایش / کلیک):"]
        lines += [f"{day}: {i:,} / {c:,}" for day, (i, c) in days]

    if not for_advertiser:
        lines += [
            "",
            f"قانون نمایش: هر کاربر حداکثر {MAX_PER_USER} بار، با فاصله‌ی حداقل "
            f"{MIN_GAP_SAME_AD // 3600} ساعت؛ بعد از کلیک دیگه نمایش داده نمیشه.",
        ]
    return "\n".join(lines)
