"""
Checks why a page's stories can't be downloaded, using the bot's own
Instagram session: finds the account id, asks Instagram for its current
stories once, and prints what came back (or the exact error).

    cd /home/mahshot/DuckLoaderBot
    /home/mahshot/venv/bin/python tools/ig_story_check.py <username>
"""

import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import platforms  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tools/ig_story_check.py <instagram username>")
    username = sys.argv[1].strip().lstrip("@").strip("/")

    label, auth_opts = platforms._instagram_auth_sources()[0]
    with platforms._open_ydl(platforms._base_opts(**auth_opts)) as ydl:
        if not platforms._ig_logged_in(ydl):
            raise SystemExit(f"The {label} session has no Instagram login.")
        print(f"Session: {label}")

        profile_url = f"https://www.instagram.com/{urllib.parse.quote(username)}/"
        try:
            html = platforms._ig_page(ydl, profile_url)
            private = re.search(r'"is_private":\s*(true|false)', html)
            print("Private account:", private.group(1) if private else "unknown (not in the page)")
        except Exception as e:
            print("Profile page:", str(e)[:300])

        try:
            user_id = platforms._ig_resolve_user_id(ydl, username, profile_url)
            print("Account id:", user_id)
        except Exception as e:
            raise SystemExit(f"Could not find the account id: {str(e)[:300]}")

        try:
            payload = platforms._ig_api(
                ydl, f"https://www.instagram.com/api/v1/feed/reels_media/?reel_ids={user_id}", profile_url,
            )
        except Exception as e:
            raise SystemExit(f"Story feed error: {str(e)[:500]}")

    reel = (payload.get("reels") or {}).get(str(user_id)) or {}
    items = reel.get("items") or []
    print("Story feed status:", payload.get("status"))
    print("Active stories:", len(items))
    if not items:
        print("-> No stories visible to the bot's account: expired, none posted, or a private account it doesn't follow.")


if __name__ == "__main__":
    main()
