"""
Podcast episodes from Apple Podcasts, Castbox and Spotify.

Every link is resolved to the podcast's own RSS feed and the episode in it,
and the audio comes from the podcast's host (the MP3 in the feed's
enclosure) - the same file the apps play. Spotify only gives metadata, so
its episodes are looked up by show name in Apple's podcast directory and
matched in the feed by title, date and length; Spotify-exclusive shows have
no public feed and can't be fetched.

On top of the file:
- chapters, as Telegram timestamps (tapping one seeks the audio): from the
  feed's podcast:chapters JSON, the MP3's own ID3 chapters, or timestamp
  lines in the show notes;
- series: the other parts of a story told over several episodes
  ("... قسمت اول", "... part 2"), found in the feed.
"""

import difflib
import email.utils
import html
import logging
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

import platforms

logger = logging.getLogger(__name__)

FEED_TTL = 30 * 60
MAX_FEED_BYTES = 30 * 1024 * 1024
HEADERS = {"User-Agent": platforms.USER_AGENT}
_NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "podcast": "https://podcastindex.org/namespace/1.0",
    "content": "http://purl.org/rss/1.0/modules/content/",
}
_feeds = {}
_feeds_lock = threading.Lock()


class PodcastNotFound(Exception):
    """The episode has no public feed (Spotify exclusive) or isn't in it."""


# ---------------------------------------------------------------- links

def kind_of(url: str):
    """"apple" / "castbox" / "spotify" for an episode link, else None."""
    host = platforms._host(url)
    if host == "podcasts.apple.com" or host.endswith(".podcasts.apple.com"):
        return "apple"
    if host == "castbox.fm" or host.endswith(".castbox.fm"):
        return "castbox"
    if host.endswith("spotify.com") and "/episode/" in url:
        return "spotify"
    return None


# ---------------------------------------------------------------- feeds

def _text(element, path):
    value = element.findtext(path, default="", namespaces=_NS)
    return (value or "").strip()


def _seconds(text):
    """"1:03:50", "3830" or "63:50" -> seconds."""
    text = (text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not text:
        return None
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return total


def load_feed(feed_url: str) -> dict:
    """{"title", "author", "image", "items": [...]} of a podcast RSS feed,
    newest first as published; cached for half an hour."""
    with _feeds_lock:
        cached = _feeds.get(feed_url)
        if cached and time.time() - cached[0] < FEED_TTL:
            return cached[1]
    response = requests.get(feed_url, headers=HEADERS, timeout=30, stream=True)
    response.raise_for_status()
    body = response.raw.read(MAX_FEED_BYTES + 1, decode_content=True)
    if len(body) > MAX_FEED_BYTES:
        raise Exception("podcast feed too large")
    channel = ET.fromstring(body).find("channel")
    if channel is None:
        raise Exception("not a podcast feed")
    image = channel.find("itunes:image", _NS)
    feed = {
        "title": _text(channel, "title"),
        "author": _text(channel, "itunes:author") or _text(channel, "title"),
        "image": (image.get("href") if image is not None else None) or _text(channel, "image/url") or None,
        "items": [],
    }
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        audio = enclosure.get("url") if enclosure is not None else None
        if not audio:
            continue
        # podcast:chapters, under whichever URL the feed uses for the
        # Podcasting 2.0 namespace.
        chapters = next((child for child in item if str(child.tag).endswith("}chapters")), None)
        item_image = item.find("itunes:image", _NS)
        try:
            date = email.utils.parsedate_to_datetime(_text(item, "pubDate")).timestamp()
        except Exception:
            date = None
        feed["items"].append({
            "title": _text(item, "title"),
            "guid": _text(item, "guid"),
            "audio": audio,
            "date": date,
            "duration": _seconds(_text(item, "itunes:duration")),
            "notes": _text(item, "content:encoded") or _text(item, "description") or _text(item, "itunes:summary"),
            "chapters_url": chapters.get("url") if chapters is not None else None,
            "image": (item_image.get("href") if item_image is not None else None) or feed["image"],
            "number": _text(item, "itunes:episode"),
        })
    with _feeds_lock:
        _feeds[feed_url] = (time.time(), feed)
    return feed


def _norm(text):
    return platforms._normalize_text(re.sub(r"[_\-–—|:،,.!?؟«»\"'()\[\]]+", " ", text or ""))


def _similar(a, b):
    a, b = _norm(a), _norm(b)
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _bare_audio(url):
    return (url or "").split("?", 1)[0].rstrip("/")


def _match(items, title=None, date=None, duration=None, audio=None):
    """The feed item for an episode known by some of these, or None."""
    if audio:
        for item in items:
            if _bare_audio(item["audio"]) == _bare_audio(audio):
                return item
    best, best_score = None, 0.0
    for item in items:
        score = _similar(title, item["title"]) if title else 0.0
        if date and item["date"]:
            days = abs(date - item["date"]) / 86400
            score += 0.3 if days <= 2 else (-0.3 if days > 10 else 0)
        if duration and item["duration"]:
            score += 0.2 if abs(duration - item["duration"]) <= max(30, duration * 0.03) else -0.2
        if score > best_score:
            best, best_score = item, score
    return best if best_score >= 0.75 else None


# ---------------------------------------------------------------- the three platforms

def _itunes(path, **params):
    response = requests.get(f"https://itunes.apple.com/{path}", params=params, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.json().get("results") or []


def _resolve_apple(url):
    parsed = urllib.parse.urlparse(url)
    show = re.search(r"/id(\d+)", parsed.path)
    episode_id = (urllib.parse.parse_qs(parsed.query).get("i") or [None])[0]
    if not show:
        raise platforms.UnsupportedLinkError("podcast_unsupported")
    if not episode_id:
        raise platforms.UnsupportedLinkError("podcast_show")
    results = _itunes("lookup", id=show.group(1), entity="podcastEpisode", limit=300)
    show_info = next((r for r in results if r.get("wrapperType") == "track" and r.get("feedUrl")), None) or {}
    episode = next((r for r in results if str(r.get("trackId")) == str(episode_id)), None)
    feed = load_feed(show_info["feedUrl"]) if show_info.get("feedUrl") else None
    item = None
    if feed:
        date = None
        if episode and episode.get("releaseDate"):
            try:
                date = time.mktime(time.strptime(episode["releaseDate"][:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                pass
        item = _match(
            feed["items"], title=(episode or {}).get("trackName"), date=date,
            duration=((episode or {}).get("trackTimeMillis") or 0) / 1000 or None,
            audio=(episode or {}).get("episodeUrl"),
        )
    if not item and episode and episode.get("episodeUrl"):
        # Older than Apple's episode list in the feed, or no feed: Apple's
        # own record of it is enough to download it.
        item = {
            "title": episode.get("trackName") or "", "guid": "", "audio": episode["episodeUrl"], "date": None,
            "duration": (episode.get("trackTimeMillis") or 0) / 1000 or None,
            "notes": episode.get("description") or "", "chapters_url": None,
            "image": episode.get("artworkUrl600"), "number": "",
        }
    if not item:
        raise PodcastNotFound("Apple Podcasts episode not found")
    feed = feed or {"title": show_info.get("collectionName") or "", "author": show_info.get("artistName") or "",
                    "image": show_info.get("artworkUrl600"), "items": [item]}
    return feed, item


def _castbox_state(url):
    text = requests.get(url, headers=HEADERS, timeout=20).text
    match = re.search(r"__INITIAL_STATE__\s*=\s*\"([^\"]+)\"", text)
    if not match:
        return ""
    return urllib.parse.unquote(match.group(1))


def _resolve_castbox(url):
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    ids = re.search(r"/episode/(?P<slug>.*?)-?id(?P<cid>\d+)-id(?P<eid>\d+)", path)
    if not ids:
        if "/channel/" in path:
            raise platforms.UnsupportedLinkError("podcast_show")
        raise platforms.UnsupportedLinkError("podcast_unsupported")
    state = _castbox_state(f"https://castbox.fm/channel/id{ids.group('cid')}")
    feed_url = (re.findall(r"\"rss_url\":\"([^\"]+)\"", state) or [None])[0]
    if not feed_url:
        raise Exception("Castbox: no feed for this channel")
    feed = load_feed(feed_url.replace("\\/", "/"))
    # The channel page lists its latest episodes (with their audio); older
    # ones are matched by the title in the link.
    audio = None
    for episode in re.finditer(r"\{[^{}]*\"eid\":" + ids.group("eid") + r"[^{}]*\}", state):
        audio = (re.findall(r"\"url\":\"([^\"]+)\"", episode.group(0)) or [None])[0]
        if audio:
            break
    item = _match(feed["items"], title=ids.group("slug").replace("-", " "), audio=audio)
    if not item:
        raise PodcastNotFound("Castbox episode not found in its feed")
    return feed, item


def _resolve_spotify(url):
    match = re.search(r"/episode/([A-Za-z0-9]{22})", url)
    if not match:
        raise platforms.UnsupportedLinkError("podcast_unsupported")
    episode = platforms.get_spotify_client().episode(match.group(1), market="US")
    show_name = (episode.get("show") or {}).get("name") or ""
    date = None
    try:
        date = time.mktime(time.strptime(episode.get("release_date") or "", "%Y-%m-%d"))
    except ValueError:
        pass
    candidates = _itunes("search", term=show_name, media="podcast", limit=8)
    candidates.sort(key=lambda r: _similar(show_name, r.get("collectionName")), reverse=True)
    for candidate in candidates[:3]:
        if not candidate.get("feedUrl") or _similar(show_name, candidate.get("collectionName")) < 0.8:
            continue
        try:
            feed = load_feed(candidate["feedUrl"])
        except Exception as e:
            logger.info("Podcast feed unreadable | %s | %s", candidate["feedUrl"], str(e)[:200])
            continue
        item = _match(feed["items"], title=episode.get("name"), date=date,
                      duration=(episode.get("duration_ms") or 0) / 1000 or None)
        if item:
            return feed, item
    raise PodcastNotFound("Spotify episode has no public feed (Spotify exclusive?)")


def resolve(url):
    """(feed, item) for an episode link."""
    kind = kind_of(url)
    if kind == "apple":
        return _resolve_apple(url)
    if kind == "castbox":
        return _resolve_castbox(url)
    if kind == "spotify":
        return _resolve_spotify(url)
    raise platforms.UnsupportedLinkError("podcast_unsupported")


# ---------------------------------------------------------------- chapters

_TIMESTAMP_LINE = re.compile(
    r"^[\s\-–—•*▪️🔹🔸◾️⏱⏰🕐]*\(?\[?(?P<t>(?:\d{1,2}:)?\d{1,2}:\d{2})\]?\)?\s*[-–—:|.)]?\s*(?P<title>\S.{0,90})$"
)


def _chapters_from_notes(notes):
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", notes or "", flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    found = []
    for line in text.splitlines():
        match = _TIMESTAMP_LINE.match(line.strip())
        if match:
            found.append((_seconds(match.group("t")), match.group("title").strip()))
    times = [t for t, _ in found]
    # A real chapter list: at least three, in order.
    return found if len(found) >= 3 and times == sorted(times) else []


def _chapters_from_json(url):
    data = requests.get(url, headers=HEADERS, timeout=20).json()
    return [
        (float(c["startTime"]), str(c.get("title") or "").strip())
        for c in data.get("chapters") or []
        if c.get("startTime") is not None and c.get("title") and c.get("toc", True) is not False
    ]


def _chapters_from_id3(path):
    from mutagen.id3 import ID3

    chapters = []
    for frame in ID3(path).getall("CHAP"):
        title = frame.sub_frames.get("TIT2")
        if title and title.text:
            chapters.append((frame.start_time / 1000, str(title.text[0]).strip()))
    return sorted(chapters)


def chapters(item, audio_path=None):
    """[(seconds, title), ...] of an episode, or []."""
    sources = []
    if item.get("chapters_url"):
        sources.append(lambda: _chapters_from_json(item["chapters_url"]))
    if audio_path:
        sources.append(lambda: _chapters_from_id3(audio_path))
    sources.append(lambda: _chapters_from_notes(item.get("notes")))
    for source in sources:
        try:
            found = [(t, title) for t, title in source() if title]
        except Exception as e:
            logger.info("Podcast chapters unreadable: %s", str(e)[:200])
            continue
        if len(found) >= 2:
            return found
    return []


def clock(seconds):
    """Telegram makes "1:05:12" / "05:12" in a caption a seek link."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60:02d}:{rest % 60:02d}"


# ---------------------------------------------------------------- series

_ORDINALS = {
    "اول": 1, "یکم": 1, "دوم": 2, "سوم": 3, "چهارم": 4, "پنجم": 5, "ششم": 6, "هفتم": 7, "هشتم": 8,
    "نهم": 9, "دهم": 10, "یازدهم": 11, "دوازدهم": 12, "سیزدهم": 13, "چهاردهم": 14, "پانزدهم": 15,
    "آخر": 99, "پایانی": 99,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "final": 99, "last": 99,
}
_PART_RE = re.compile(
    r"\s*[\-–—|:,(\[]?\s*(?:قسمت|بخش|پارت|part|pt\.?|chapter)\s*"
    r"(?P<n>\d{1,3}|[۰-۹]{1,3}|" + "|".join(sorted(_ORDINALS, key=len, reverse=True)) + r")\b\s*[)\]]?\s*$",
    re.IGNORECASE,
)
# "(3)", "(3/5)", "3/5" at the end of a title.
_PART_TAIL_RE = re.compile(
    r"\s*(?:[(\[]\s*(?P<n>\d{1,2}|[۰-۹]{1,2})\s*(?:/\s*\d{1,2})?\s*[)\]]|(?<!\d)(?P<m>\d{1,2})\s*/\s*\d{1,2})\s*$"
)


def _part_of(title):
    """("base title", part number) if the title is one part of a series."""
    title = (title or "").strip()
    for pattern in (_PART_RE, _PART_TAIL_RE):
        match = pattern.search(title)
        if match and match.start() > 3:
            raw = (match.groupdict().get("m") or match.group("n")).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).lower()
            number = int(raw) if raw.isdigit() else _ORDINALS.get(raw)
            if number:
                return title[:match.start()].strip(" -–—|:,"), number
    return None, None


def series(feed, item, limit=10):
    """The parts of the story `item` belongs to, in order (including it),
    or [] if it isn't one of several parts."""
    base, _ = _part_of(item["title"])
    if not base:
        return []
    parts = {}
    for other in feed.get("items") or []:
        other_base, number = _part_of(other["title"])
        if other_base and _similar(base, other_base) >= 0.9 and number not in parts:
            parts[number] = other
    # Every episode numbered the same way is the show's own numbering, not
    # one story told in parts.
    if len(parts) < 2 or len(parts) > max(12, len(feed.get("items") or []) // 2):
        return []
    return [parts[n] for n in sorted(parts)][:limit]


# ---------------------------------------------------------------- download

def download(feed, item, progress_hook=None):
    """The episode's audio from its host, as (info, path)."""
    job_dir = platforms._new_job_dir()
    try:
        ext = os.path.splitext(urllib.parse.urlparse(item["audio"]).path)[1].lower()
        path = os.path.join(job_dir, "episode" + (ext if ext in (".mp3", ".m4a", ".aac", ".ogg", ".opus") else ".mp3"))
        with requests.get(item["audio"], headers=HEADERS, stream=True, timeout=60) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            if total > platforms.MAX_TELEGRAM_BYTES:
                raise platforms.FileTooLargeError(platforms.format_size(total))
            got = 0
            with open(path, "wb") as out:
                for chunk in response.iter_content(chunk_size=512 * 1024):
                    out.write(chunk)
                    got += len(chunk)
                    if got > platforms.MAX_TELEGRAM_BYTES:
                        raise platforms.FileTooLargeError(platforms.format_size(got))
                    if progress_hook and total:
                        progress_hook({
                            "status": "downloading", "downloaded_bytes": got, "total_bytes": total,
                            "_percent_str": f"{100 * got / total:.1f}%",
                        })
        platforms._check_file_size(path, platforms.MIN_VALID_FILE_BYTES)
    except BaseException:
        platforms._remove_tree(job_dir)
        raise
    info = {
        "id": item.get("guid") or _bare_audio(item["audio"]),
        "extractor": "Podcast",
        "extractor_key": "Podcast",
        "title": item["title"],
        "track": item["title"],
        "artist": feed.get("author") or feed.get("title"),
        "album": feed.get("title"),
        "uploader": feed.get("title"),
        "duration": item.get("duration"),
        "timestamp": item.get("date"),
        "thumbnail": item.get("image") or feed.get("image"),
        "duck_podcast": {"show": feed.get("title") or "", "chapters": chapters(item, path)},
    }
    return info, path
