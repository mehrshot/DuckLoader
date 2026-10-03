"""
Post-download ad campaigns (approved ad requests of type "post_download"):
which campaign a user sees and how often, impression and click counting,
the impression quota, and the report the owner (and the advertiser) reads.

Frequency rules, instead of a flat "one ad per user":
  * no ad at all if the user saw any ad in the last 15 minutes, so someone
    downloading five files in a row sees one ad, not five;
  * the same campaign at most 3 times per user, at least 12 hours apart;
  * never again to a user who already clicked it;
  * among the campaigns a user may see, the one they have seen least wins,
    then the one furthest behind its quota (so several campaigns finish
    at a similar pace).
"""

import random
import re
import time

import store

STATS_FILE = "ad_stats.json"

MIN_GAP_ANY_AD = 15 * 60
MAX_PER_USER = 3
MIN_GAP_SAME_AD = 12 * 3600

ACTIVE = "approved"
PAUSED = "paused"
COMPLETED = "completed"
CAMPAIGN_STATUSES = (ACTIVE, PAUSED, COMPLETED)

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
    return (request.get("display_name") or request.get("channel") or "").strip()


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
            if not display_name(request):
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
            candidates.append(((seen, progress, random.random()), request))

        if not candidates:
            return None, False

        request = min(candidates, key=lambda item: item[0])[1]
        stats = _campaign(data, request["request_id"])
        stats["impressions"] += 1
        seen = stats["viewers"].get(uid, [0, 0])[0]
        stats["viewers"][uid] = [seen + 1, int(now)]
        day = stats["daily"].setdefault(time.strftime("%Y-%m-%d", time.localtime(now)), [0, 0])
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
        day = stats["daily"].get(time.strftime("%Y-%m-%d"))
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
        day = stats["daily"].setdefault(time.strftime("%Y-%m-%d"), [0, 0])
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
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def report_text(request, for_advertiser=False) -> str:
    stats = get_stats(request["request_id"])
    limit = quota(request)
    impressions = stats["impressions"]
    viewers = len(stats["viewers"])
    clickers = len(stats["clickers"])

    lines = [
        f"📊 گزارش تبلیغ {request['request_id']}",
        f"📢 {display_name(request)}"
        + (f" ({request.get('channel')})" if request.get("channel") and request.get("channel") != display_name(request) else ""),
    ]
    if not for_advertiser:
        lines.append(f"وضعیت: {STATUS_LABELS.get(request.get('status'), request.get('status') or '—')}")
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
