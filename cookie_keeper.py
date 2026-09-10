import argparse
import asyncio
import json
import logging
import os
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

    context = (
        await playwright.chromium
        .launch_persistent_context(
            user_data_dir=str(profile),
            headless=headless,
            channel="chromium",
            args=[
                "--password-store=basic",
                "--disable-dev-shm-usage",
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
) -> bool:

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

        if looks_logged_out(
            site_name,
            page,
        ):
            logger.error(
                "%s session appears logged out.",
                site_name,
            )
            return False

        cookies = await context.cookies()

        logger.info(
            "%s session looks active "
            "(browser cookies available: %d).",
            site_name,
            len(cookies),
        )

        return True

    except Exception:
        logger.exception(
            "%s session refresh failed.",
            site_name,
        )
        return False


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


async def maintain_site(
    playwright,
    site_name: str,
) -> None:

    context = None
    logged_out_alert_sent = False

    while True:

        try:
            if (
                context is None
                or context.pages == []
            ):
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass

                context = await launch_browser(
                    playwright,
                    site_name,
                    headless=True,
                )

            page = await get_page(
                context
            )

            healthy = await touch_site(
                site_name,
                context,
                page,
            )

            if healthy:

                if logged_out_alert_sent:
                    send_owner_message(
                        f"🦆 DuckLoader {site_name} "
                        "browser session is active again."
                    )

                logged_out_alert_sent = False

            else:

                if not logged_out_alert_sent:
                    send_owner_message(
                        f"⚠️ DuckLoader {site_name} "
                        "browser session appears to be logged out.\n\n"
                        "Run cookie_keeper.py bootstrap "
                        f"{site_name} to log in again."
                    )

                    logged_out_alert_sent = True

        except Exception:
            logger.exception(
                "%s browser keeper cycle failed.",
                site_name,
            )

            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

                context = None

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