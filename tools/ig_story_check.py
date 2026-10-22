"""
Checks why a page's stories can't be downloaded, using the bot's own
Instagram session: finds the account id, then asks Instagram for its
current stories through each known route once and prints what each one
returned, to see which route still works.

    cd /home/mahshot/DuckLoaderBot
    /home/mahshot/venv/bin/python tools/ig_story_check.py <username> [<username> ...]

Use an account that surely has a story right now (e.g. "instagram") as a
control. Each username costs ~6 requests to Instagram; don't loop it.
"""

import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import platforms  # noqa: E402


def _items(payload, user_id):
    """Story items in any of the shapes these endpoints return."""
    if not isinstance(payload, dict):
        return None
    reels = payload.get("reels") or {}
    if isinstance(reels, dict) and reels:
        reel = reels.get(str(user_id)) or next(iter(reels.values()))
        return len((reel or {}).get("items") or [])
    for key in ("reels_media", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            if value and isinstance(value[0], dict) and "items" in value[0]:
                return len(value[0].get("items") or [])
            return len(value)
    reel = payload.get("reel")
    if isinstance(reel, dict):
        return len(reel.get("items") or [])
    return 0 if payload.get("status") == "ok" else None


def check(ydl, username):
    print(f"\n=== {username}")
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
        print(f"Could not find the account id: {str(e)[:300]}")
        return

    routes = [
        ("web reels_media", f"https://www.instagram.com/api/v1/feed/reels_media/?reel_ids={user_id}"),
        ("app reels_media", f"https://i.instagram.com/api/v1/feed/reels_media/?reel_ids={user_id}"),
        ("web user story", f"https://www.instagram.com/api/v1/feed/user/{user_id}/story/"),
        ("app user story", f"https://i.instagram.com/api/v1/feed/user/{user_id}/story/"),
        ("web user reel_media", f"https://www.instagram.com/api/v1/feed/user/{user_id}/reel_media/"),
    ]
    for label, url in routes:
        try:
            payload = platforms._ig_api(ydl, url, f"https://www.instagram.com/stories/{username}/")
            print(f"  {label:20} OK   status={payload.get('status')} stories={_items(payload, user_id)}")
        except Exception as e:
            print(f"  {label:20} FAIL {str(e)[:160]}")

    try:
        html = platforms._ig_page(ydl, f"https://www.instagram.com/stories/{urllib.parse.quote(username)}/")
        print(
            f"  {'story page html':20} OK   video_versions={html.count('video_versions')} "
            f"image_versions2={html.count('image_versions2')} "
            f"reels_media_json={'xdt_api__v1__feed__reels_media' in html or 'reels_media' in html}"
        )
    except Exception as e:
        print(f"  {'story page html':20} FAIL {str(e)[:160]}")


def main() -> None:
    usernames = [u.strip().lstrip("@").strip("/") for u in sys.argv[1:] if u.strip()]
    if not usernames:
        raise SystemExit("Usage: python tools/ig_story_check.py <username> [<username> ...]")

    label, auth_opts = platforms._instagram_auth_sources()[0]
    with platforms._open_ydl(platforms._base_opts(**auth_opts)) as ydl:
        if not platforms._ig_logged_in(ydl):
            raise SystemExit(f"The {label} session has no Instagram login.")
        print(f"Session: {label}")
        for username in usernames[:3]:
            check(ydl, username)


if __name__ == "__main__":
    main()
