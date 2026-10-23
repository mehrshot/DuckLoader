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


_CASTBOX_EPISODE_RE = re.compile(r"/episode/(?P<slug>.*?)-?id(?P<cid>\d+)-id(?P<eid>\d+)")


def _castbox_episode_id(url):
    """The episode id in any Castbox link: /ep/<id>, /vb/<id>, the app's
    player link, an eid= parameter, or a dynamic link wrapping one of
    them (link=...)."""
    url = urllib.parse.unquote(url)
    for pattern in (r"/(?:ep|vb)/(\d{5,})", r"[?&]eid=(\d{5,})", r"/player/id\d+/id(\d{5,})", r"-id\d+-id(\d{5,})"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _resolve_castbox(url):
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    ids = _CASTBOX_EPISODE_RE.search(path)
    if not ids:
        # Every other kind of episode link: its id, then castbox.fm/ep/<id>,
        # which redirects to the full /episode/<title>-id<channel>-id<episode>
        # page. (Share links like /vb/<id> lead to a "dynamic link" page
        # instead of redirecting.)
        episode_id = _castbox_episode_id(url) or _castbox_episode_id(platforms._resolve_redirects(url))
        if episode_id:
            path = urllib.parse.unquote(urllib.parse.urlparse(
                platforms._resolve_redirects(f"https://castbox.fm/ep/{episode_id}")).path)
            ids = _CASTBOX_EPISODE_RE.search(path)
    if not ids:
        if "/channel/" in path or "/ch/" in path:
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

# A time in the show notes: "05:12", "1:02:30", "(08:01)", "▶ (08:01)".
_TIMESTAMP = re.compile(r"(?<![\d:/.])(?P<t>(?:\d{1,2}:)?\d{1,2}:\d{2})(?![\d:])")
# What surrounds a time but isn't part of its title.
_TITLE_EDGE = re.compile(r"^[\s\-–—•*|:.,)(\[\]▶►▸⏯⏱⏰🕐🔹🔸▪◾️\ufe0f]+|[\s\-–—•*|:.,(\[\]▶►▸⏯\ufe0f]+$")
# "1:", "02-", "3)", "4." numbering before a title (not a time: no digits after).
_NUMBERING = re.compile(r"(?:^|\s)\d{1,2}\s*[:\-.)]\s*(?=\D)")


def _clean_title(text):
    text = _TITLE_EDGE.sub("", text.strip())
    text = _NUMBERING.sub(" ", " " + text, count=1).strip() if _NUMBERING.match(" " + text) else text
    return _TITLE_EDGE.sub("", text)[:90]


def _chapters_from_notes(notes, duration=None):
    """Chapters written in the show notes, in any of the usual shapes:
    "05:12 Title" (time first), "Title ▶ (05:12)" (time after, on the same
    or the next line), or a whole list in one paragraph "1: A ▶(6:54) 2: B
    ▶(11:34)". This is also what Castbox and other apps show as sections:
    they make these times in the notes tappable."""
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", notes or "", flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
    text = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    stamps = list(_TIMESTAMP.finditer(text))
    if len(stamps) < 3:
        return []
    times = [_seconds(m.group("t")) for m in stamps]

    def same_line_after(i):
        end = stamps[i + 1].start() if i + 1 < len(stamps) else len(text)
        return text[stamps[i].end():end].split("\n", 1)[0]

    def text_before(i):
        start = stamps[i - 1].end() if i else 0
        segment = text[start:stamps[i].start()]
        lines = [line for line in segment.split("\n") if _clean_title(line)]
        last = lines[-1] if lines else ""
        # A whole paragraph before the first time: keep what follows its
        # last "1:"-style numbering.
        numbered = list(re.finditer(r"(?:^|\s)\d{1,2}\s*[:\-.)]\s*(?=\D)", last))
        return last[numbered[-1].end():] if numbered else last[-90:]

    def starts_line(i):
        line_start = text.rfind("\n", 0, stamps[i].start()) + 1
        return not _clean_title(text[line_start:stamps[i].start()])

    # Which side the titles are on: "05:12 Title" starts lines with the time
    # and has words after it ("Title ▶(05:12)" and one-paragraph lists don't).
    time_first = sum(
        starts_line(i) and bool(re.search(r"\w{2}", same_line_after(i))) for i in range(len(stamps))
    ) > len(stamps) * 0.6
    found = []
    for i, seconds in enumerate(times):
        title = _clean_title(same_line_after(i) if time_first else text_before(i))
        if title and (not duration or seconds < duration):
            found.append((seconds, title))
    found = _in_order(found)
    return found if len(found) >= 3 else []


def _in_order(chapters):
    """The longest run of chapters in time order: a typo in one time
    ("58:50" for "28:50", a time written twice) drops only that one."""
    best = [[c] for c in chapters]
    for i in range(len(chapters)):
        for j in range(i):
            if chapters[j][0] < chapters[i][0] and len(best[j]) + 1 > len(best[i]):
                best[i] = best[j] + [chapters[i]]
    return max(best, key=len) if best else []


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
    sources.append(lambda: _chapters_from_notes(item.get("notes"), item.get("duration")))
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


# Separators between a title's parts: "Book - Chapter 2 - 03", "Show | Ep",
# "Story: part one".
_TITLE_SPLIT = re.compile(r"\s*[-–—|]+\s+|\s+[-–—|]+\s*|\s*\|\s*|\s*[:：]\s+")
# What ends a series' base name but isn't part of it: a number, a Roman
# numeral, "part 2", "(3/5)".
_TRAILING_PART = re.compile(
    r"(?:\s+(?:\d{1,3}|[ivx]{1,5}|(?:part|pt|chapter|قسمت|بخش|پارت|فصل)\s*\S+))+\s*$", re.IGNORECASE,
)
_JUST_NUMBERING = re.compile(r"^(?:#|ep|episode|e|no|قسمت|اپیزود|بخش|شماره|part)?\s*\d{1,4}$", re.IGNORECASE)


_NUMBER_WORDS = {
    "یک", "یکم", "دو", "سه", "چهار", "پنج", "شش", "هفت", "هشت", "نه", "ده", "یازده", "دوازده", "سیزده", "چهارده",
    "پانزده", "شانزده", "هفده", "هجده", "نوزده", "بیست", "سی", "چهل", "پنجاه", "شصت", "هفتاد", "هشتاد", "نود",
    "صد", "یکصد", "دویست", "سیصد", "و", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "twenty", "thirty", "forty", "fifty",
}
# A show's own episode number at the start: "567. ", "#12 ", "Ep 4 - ",
# "اپیزود سی و سه - " - different on every episode, so never a series name.
_LEADING_NUMBER = re.compile(r"^\s*(?:#?\d{1,4}\s*[.):\-–—|]\s*|#\d{1,4}\s+|(?:ep|episode|e)\.?\s*\d{1,4}\s*[.:\-–—|]?\s+)", re.IGNORECASE)
# Signs that a title is one part of something.
_PART_WORDS = re.compile(r"(?:^|\s)(?:قسمت|بخش|فصل|پارت|part|pt|chapter|chap|vol|volume|مقدمه|پیشگفتار|پایانی|آخر|finale)(?:\s|$|\d)", re.IGNORECASE)


def _is_episode_number(segment):
    words = segment.split()
    return bool(words) and words[0] in ("اپیزود", "قسمت", "episode", "ep", "show", "no") and all(
        w in _NUMBER_WORDS or w.isdigit() for w in words[1:]
    )


def _title_segments(title):
    """A title's named segments, without the show's episode numbering."""
    title = _LEADING_NUMBER.sub("", title or "")
    return [seg for seg in _TITLE_SPLIT.split(title) if seg.strip() and not _is_episode_number(_norm(seg))]


def _series_base(title, show_prefix=None):
    """(base name, is clearly one part of something) of an episode title:
    "کتاب فقر احمق می‌کند - فصل دوم - 03" -> "کتاب فقر احمق میکند",
    "داستان زندگی راسپوتین قسمت دوم" -> "داستان زندگی راسپوتین",
    "713. King Arthur: The Once & Future King (Part 1)" -> "king arthur".
    `show_prefix`: a first segment the show puts on every title."""
    segments = _title_segments(title)
    if show_prefix and len(segments) > 1 and _norm(segments[0]) == show_prefix:
        segments = segments[1:]
    joined = " - ".join(segments)
    part_base, _ = _part_of(joined)
    normalized = [_norm(seg) for seg in segments]
    named = [seg for seg in normalized if seg and not _JUST_NUMBERING.match(seg)]
    if not named:
        return None, False
    if part_base:
        # "Story (Part 2)" or "Story: Episode name (Part 2)": the story is
        # the first segment (or the whole title before the part word).
        first = _norm(_TITLE_SPLIT.split(part_base)[0]) if len(segments) > 1 else _norm(part_base)
        return first or named[0], True
    first = _TRAILING_PART.sub("", named[0]).strip() or named[0]
    # One part of something: "Name - Chapter 2", "Name - Intro - 01",
    # "Name - 03" (a part word, or a number of its own at the end), or
    # "Name VI" / "Name 3" (a number ending the name itself).
    is_part = first != named[0] or len(segments) >= 2 and (
        any(_PART_WORDS.search(seg) for seg in normalized[1:]) or bool(_JUST_NUMBERING.match(normalized[-1]))
    )
    return first, is_part


def _show_prefix(items):
    """The first title segment most episodes share (the show's own name),
    if there is one."""
    firsts = [_norm(segs[0]) for segs in (_title_segments(it["title"]) for it in items) if len(segs) > 1]
    if len(items) < 5 or not firsts:
        return None
    common = max(set(firsts), key=firsts.count)
    return common if firsts.count(common) > len(items) * 0.4 else None


def _same_series(a, b):
    """Same base name, or one is the other plus a word or two ("کتاب اسب
    سیاه" / "اسب سیاه", "معرفی کتاب X" / "کتاب X")."""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short.split()) < 2 or short not in long_:
        return False
    return len(long_.split()) - len(short.split()) <= 2


def series(feed, item, limit=12):
    """The parts of the story `item` belongs to, oldest first (including
    it), or [] if it isn't one of several parts. Parts share a base name
    and were published one after another; a name nearly every episode
    shares is the show's own (not a story)."""
    items = feed.get("items") or []
    prefix = _show_prefix(items)
    base, is_part = _series_base(item["title"], prefix)
    if not base or not is_part or len(base.split()) < 2 and len(base) < 8:
        return []
    bases = [_series_base(other["title"], prefix) for other in items]
    members = [i for i, (other_base, other_is_part) in enumerate(bases)
               if _same_series(base, other_base) and (other_is_part or items[i] is item)]
    if len(members) > max(12, len(items) // 2):
        return []
    # Only the run of neighbouring episodes around this one (a gap of up to
    # two unrelated episodes is fine).
    try:
        here = next(i for i in members if items[i] is item or items[i]["audio"] == item["audio"])
    except StopIteration:
        return []
    run = [here]
    for step in (-1, 1):
        last = here
        for i in (members[::-1] if step < 0 else members):
            if (step < 0 and i < last and last - i <= 3) or (step > 0 and i > last and i - last <= 3):
                run.append(i)
                last = i
    if len(run) < 2:
        return []
    parts = [items[i] for i in sorted(set(run))]
    # Feeds list the newest first; a story is told oldest first.
    if all(p["date"] for p in parts):
        parts.sort(key=lambda p: p["date"])
    else:
        parts.reverse()
    return parts[:limit]


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
