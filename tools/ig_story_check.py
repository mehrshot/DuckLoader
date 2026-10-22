"""
Checks Instagram story downloading with the bot's own session: runs every
story route the bot has (the same code the bot uses) once for a username
and prints what each returned, plus each route's recorded health.

    cd /home/mahshot/DuckLoaderBot
    /home/mahshot/venv/bin/python tools/ig_story_check.py <username> [<username> ...]

Use an account that surely has a story right now (e.g. "instagram") as a
control. Each username costs ~7 requests to Instagram; don't loop it.
"""

import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import platforms  # noqa: E402


def check(ydl, username):
    print(f"\n=== {username}")
    story_url = f"https://www.instagram.com/stories/{urllib.parse.quote(username)}/"
    try:
        user_id = platforms._ig_resolve_user_id(ydl, username, story_url)
        print("User id:", user_id)
        for source, private, note in platforms.ig_privacy_checks(ydl, user_id, username, story_url):
            print(f"  private? {source:17} {({True: 'yes', False: 'no', None: '?'})[private]:4} {note}")
    except Exception as e:
        print("Privacy check:", str(e)[:300])

    ctx = {"url": f"https://www.instagram.com/stories/{username}/", "username": username, "reel_id": None,
           "page_url": f"https://www.instagram.com/stories/{username}/"}
    for name, route in platforms.IG_STORY_ROUTES.items():
        try:
            outcome, reel = route(ydl, ctx)
            count = len((reel or {}).get("items") or [])
            print(f"  {name:12} {outcome:13} {f'{count} stories' if reel else ''}")
        except platforms.IgRouteBroken as e:
            print(f"  {name:12} {'BROKEN':13} {str(e)[:150]}")
        except Exception as e:
            print(f"  {name:12} {'ERROR':13} {str(e)[:150]}")


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

    print("\nRoute health recorded by the bot (ig_story_routes.json):")
    health = platforms.ig_story_route_health()
    if not health:
        print("  (nothing recorded yet)")
    for name in platforms.IG_STORY_ROUTES:
        state = health.get(name) or {}
        paused = state.get("demoted_until", 0) > time.time()
        print(f"  {name:12} ok={state.get('ok', 0)} broken={state.get('broken', 0)} streak={state.get('streak', 0)}"
              f"{'  PAUSED' if paused else ''}  {state.get('last_error', '')[:80] if paused else ''}")


if __name__ == "__main__":
    main()
