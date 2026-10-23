import argparse
import asyncio
import logging
import os
import shutil
from pathlib import Path
from urllib import parse, request

from dotenv import load_dotenv
from playwright.async_api import BrowserContext
from playwright.async_api import Page
from playwright.async_api import async_playwright


load_dotenv()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(
    "duckloader.cookie_keeper"
)


REFRESH_INTERVAL = max(
    300,
    int(
        os.environ.get(
            "COOKIE_KEEPER_INTERVAL",
            "900",
        )
    ),
)

BROWSER_CHANNEL = os.environ.get(
    "COOKIE_KEEPER_BROWSER_CHANNEL",
    "chrome" if shutil.which("google-chrome") else "chromium",
)

BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    "",
).strip()

OWNER_ID = os.environ.get(
    "OWNER_ID",
    "",
).strip()


SITES = {
    "youtube": {
        "browser_profile": Path(
            os.environ.get(
                "YOUTUBE_BROWSER_PROFILE",
                "/home/mahshot/.duckloader-browser/youtube",
            )
        ).expanduser(),
        "url": "https://www.youtube.com/",
    },
    "instagram": {
        "browser_profile": Path(
            os.environ.get(
                "INSTAGRAM_BROWSER_PROFILE",
                "/home/mahshot/.duckloader-browser/instagram",
            )
        ).expanduser(),
        "url": "https://www.instagram.com/",
    },
}


def send_owner_message(text: str) -> None:
    if not BOT_TOKEN or not OWNER_ID:
        return

    try:
        payload = parse.urlencode(
            {
                "chat_id": OWNER_ID,
                "text": text,
            }
        ).encode("utf-8")

        url = (
            f"https://api.telegram.org/"
            f"bot{BOT_TOKEN}/sendMessage"
        )

        request.urlopen(
            request.Request(
                url,
                data=payload,
                method="POST",
            ),
            timeout=20,
        )

    except Exception:
        logger.exception(
            "Failed to send owner alert."
        )


async def launch_browser(
    playwright,
    site_name: str,
    headless: bool,
) -> BrowserContext:

    profile = SITES[site_name][
        "browser_profile"
    ]

    profile.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger.info(
        "Launching %s browser profile: %s",
        site_name,
        profile,
    )

    # The installed Google Chrome, without Playwright's automation flags:
    # Playwright's own "Chrome for Testing" with navigator.webdriver set
    # looks like a bot to Instagram and gets stuck in its checks at login.
    context = (
        await playwright.chromium
        .launch_persistent_context(
            user_data_dir=str(profile),
            headless=headless,
            channel=BROWSER_CHANNEL,
            ignore_default_args=["--enable-automation"],
            args=[
                "--password-store=basic",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={
                "width": 1440,
                "height": 900,
            },
        )
    )

    return context


async def get_page(
    context: BrowserContext,
) -> Page:

    if context.pages:
        page = context.pages[0]

        if not page.is_closed():
            return page

    return await context.new_page()


def looks_logged_out(
    site_name: str,
    page: Page,
) -> bool:

    current_url = (
        page.url or ""
    ).lower()

    if site_name == "youtube":
        if (
            "accounts.google.com" in current_url
            or "/signin" in current_url
            or "/login" in current_url
        ):
            return True

    elif site_name == "instagram":
        if (
            "/accounts/login" in current_url
            or "/accounts/login/" in current_url
        ):
            return True

    return False


async def touch_site(
    site_name: str,
    context: BrowserContext,
    page: Page,
) -> str:
    """"active", "logged_out" (the site sent us to its login page) or
    "error" (the page didn't load: network blip, timeout, browser trouble —
    says nothing about the session)."""

    url = SITES[site_name]["url"]

    logger.info(
        "Refreshing %s session...",
        site_name,
    )

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        await page.wait_for_timeout(
            5_000
        )
    except Exception as e:
        logger.warning(
            "%s session check failed (page did not load): %s",
            site_name,
            str(e).splitlines()[0][:200],
        )
        return "error"

    if looks_logged_out(
        site_name,
        page,
    ):
        logger.error(
            "%s session appears logged out.",
            site_name,
        )
        return "logged_out"

    cookies = await context.cookies()

    # Logged out, instagram.com shows its login form at the same address
    # (no redirect to /accounts/login), so the URL alone can miss it. The
    # session cookie can't: Instagram removes it when it ends the session.
    if site_name == "instagram" and not any(
        cookie.get("name") == "sessionid"
        and cookie.get("value")
        and "instagram.com" in (cookie.get("domain") or "")
        for cookie in cookies
    ):
        logger.error(
            "instagram session appears logged out (no sessionid cookie).",
        )
        return "logged_out"

    logger.info(
        "%s session looks active "
        "(browser cookies available: %d).",
        site_name,
        len(cookies),
    )

    return "active"


async def bootstrap_site(
    site_name: str,
) -> None:

    if site_name not in SITES:
        raise SystemExit(
            f"Unknown site: {site_name}"
        )

    async with async_playwright() as p:

        context = await launch_browser(
            p,
            site_name,
            headless=False,
        )

        page = await get_page(
            context
        )

        try:
            await page.goto(
                SITES[site_name]["url"],
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            logger.info(
                "Browser opened for %s.",
                site_name,
            )

            print()
            print(
                "=" * 70
            )
            print(
                f"Log in to {site_name.upper()} "
                "inside this browser."
            )
            print(
                "Complete any verification or 2FA "
                "steps if the platform asks for them."
            )
            print(
                "When you can browse normally while "
                "logged in, return here and press ENTER."
            )
            print(
                "=" * 70
            )
            print()

            await asyncio.get_running_loop().run_in_executor(
                None,
                input,
            )

            cookies = await context.cookies()

            logger.info(
                "%s bootstrap finished. "
                "Stored browser cookies: %d.",
                site_name,
                len(cookies),
            )

        finally:
            await context.close()


# A failed check is retried after a minute instead of the full interval;
# only several failures in a row are reported (as "can't check", not as a
# logout). If the browser driver itself is broken (e.g. "write EPIPE"),
# relaunching inside this process doesn't help: the service exits and
# systemd starts it fresh.
RETRY_INTERVAL = 60
FAILURES_BEFORE_ALERT = 3
FAILURES_BEFORE_EXIT = 6
CHECK_TIMEOUT = 150


async def _close_quietly(context) -> None:
    if context is None:
        return
    try:
        await asyncio.wait_for(context.close(), timeout=30)
    except Exception:
        pass


async def maintain_site(
    playwright,
    site_name: str,
) -> None:

    context = None
    alerted = None  # what the owner was last told: "logged_out" / "unreachable"
    failures = 0

    while True:

        status = "error"

        try:
            if (
                context is None
                or context.pages == []
            ):
                await _close_quietly(context)

                context = await asyncio.wait_for(
                    launch_browser(
                        playwright,
                        site_name,
                        headless=True,
                    ),
                    timeout=CHECK_TIMEOUT,
                )

            page = await get_page(
                context
            )

            status = await asyncio.wait_for(
                touch_site(
                    site_name,
                    context,
                    page,
                ),
                timeout=CHECK_TIMEOUT,
            )

        except Exception:
            logger.exception(
                "%s browser keeper cycle failed.",
                site_name,
            )

        if status == "error":
            # The page or the browser may be stuck: start fresh next time.
            await _close_quietly(context)
            context = None
            failures += 1

            if failures >= FAILURES_BEFORE_ALERT and alerted != "unreachable":
                send_owner_message(
                    f"⚠️ DuckLoader: {site_name} session could not be checked "
                    f"({failures} attempts in a row failed to load the page).\n\n"
                    "This is a network or browser problem, not a logout. "
                    "Downloads may still work."
                )
                alerted = "unreachable"

            if failures >= FAILURES_BEFORE_EXIT:
                logger.error(
                    "%s: %d failed checks in a row; exiting so systemd restarts the keeper.",
                    site_name,
                    failures,
                )
                raise SystemExit(1)

            await asyncio.sleep(RETRY_INTERVAL)
            continue

        failures = 0

        if status == "logged_out":
            if alerted != "logged_out":
                send_owner_message(
                    f"⚠️ DuckLoader {site_name} "
                    "browser session is logged out "
                    "(login page or no session cookie).\n\n"
                    "Run cookie_keeper.py bootstrap "
                    f"{site_name} to log in again."
                )
                alerted = "logged_out"

        elif alerted is not None:
            send_owner_message(
                f"🦆 DuckLoader {site_name} "
                "browser session is active again."
            )
            alerted = None

        await asyncio.sleep(
            REFRESH_INTERVAL
        )


async def run_service() -> None:

    logger.info(
        "DuckLoader browser session keeper "
        "starting. Interval: %d seconds.",
        REFRESH_INTERVAL,
    )

    async with async_playwright() as p:

        await asyncio.gather(
            maintain_site(
                p,
                "youtube",
            ),
            maintain_site(
                p,
                "instagram",
            ),
        )


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "command",
        choices=[
            "run",
            "bootstrap",
        ],
    )

    parser.add_argument(
        "site",
        nargs="?",
        choices=[
            "youtube",
            "instagram",
        ],
    )

    args = parser.parse_args()

    if args.command == "bootstrap":

        if not args.site:
            raise SystemExit(
                "bootstrap requires "
                "youtube or instagram"
            )

        asyncio.run(
            bootstrap_site(
                args.site
            )
        )

    else:

        asyncio.run(
            run_service()
        )


if __name__ == "__main__":
    main()