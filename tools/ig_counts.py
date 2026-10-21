"""
Shows the like / comment / view numbers Instagram returns for a post, using
the bot's own Instagram session (one request), and the view count the bot
will put in the caption.

    cd /home/mahshot/DuckLoaderBot
    /home/mahshot/venv/bin/python tools/ig_counts.py "https://www.instagram.com/reel/XXXX/"
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import platforms  # noqa: E402
from yt_dlp.extractor.instagram import _id_to_pk  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tools/ig_counts.py <instagram post or reel link>")

    url, _ = platforms.normalize_instagram_url(sys.argv[1])
    match = re.search(r"instagram\.com/(?:p|reel|tv)/([^/?#]+)", url)
    if not match:
        raise SystemExit("Not a post or reel link.")

    label, auth_opts = platforms._instagram_auth_sources()[0]
    with platforms._open_ydl(platforms._base_opts(**auth_opts)) as ydl:
        if not platforms._ig_logged_in(ydl):
            raise SystemExit(f"The {label} session has no Instagram login.")
        data = platforms._ig_api(ydl, f"https://www.instagram.com/api/v1/media/{_id_to_pk(match.group(1))}/info/", url)

    item = (data.get("items") or [{}])[0]
    counts = {key: value for key, value in item.items() if "count" in key.lower() and not isinstance(value, (dict, list))}
    print(f"Session: {label}")
    print("Count fields from Instagram:")
    for key, value in sorted(counts.items()):
        print(f"  {key}: {value}")
    print(f"View count the bot will show: {platforms._ig_views_in(item) or 0}")


if __name__ == "__main__":
    main()
