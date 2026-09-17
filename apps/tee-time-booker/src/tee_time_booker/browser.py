"""The browser every adapter drives, and the artifacts it leaves behind.

One Chromium context per run. Adapters get a `BrowserSession` rather than
raw Playwright objects so the launch options, timeouts and debugging
artifacts are the same whichever site is being booked.
"""

import random
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger
from playwright.sync_api import BrowserContext, Page, sync_playwright

# How long to wait for any single element / response before giving up.
STEP_TIMEOUT_MS = 15_000


def _user_agent(browser_version: str) -> str:
    """A normal desktop Chrome UA matching the launched Chromium's major version.

    Booking sites (foreUP among them) return 403 to the default headless UA
    ("HeadlessChrome/...").
    """
    major = browser_version.split(".")[0]
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def pause(low: float = 0.3, high: float = 0.9) -> None:
    """Small human-ish delay between UI steps. Not used for synchronisation."""
    time.sleep(random.uniform(low, high))


@dataclass
class BrowserSession:
    """The page an adapter works on, plus where its debugging output goes."""

    context: BrowserContext
    page: Page
    artifacts_dir: Path
    # Prefixed onto every artifact, so a run-log row points at its screenshots.
    run_id: str = ""

    def _path(self, name: str) -> Path:
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{self.run_id}-{name}".lstrip("-"))
        return self.artifacts_dir / stem

    def save_text(self, name: str, text: str) -> None:
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            self._path(name).write_text(text)
        except Exception as e:  # never mask the original error
            logger.warning(f"Could not save {name}: {e}")

    def save_artifacts(self, name: str, *, hide: str = "") -> None:
        """Screenshot plus page HTML; the HTML is what you need to fix a selector.

        Elements matching `hide` (a CSS selector, e.g. card inputs) are made
        invisible first. Typed values aren't part of ``page.content()`` and
        hosted card iframes are cross-origin, so the HTML is safe as is.
        """
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            stem = self._path(name)
            if hide:
                self.page.add_style_tag(
                    content=f"{hide} {{ visibility: hidden !important; }}"
                )
            self.page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
            stem.with_suffix(".html").write_text(self.page.content())
            logger.info(f"Saved {stem}.png and .html")
        except Exception as e:  # never mask the original error
            logger.warning(f"Could not save artifacts: {e}")


@contextmanager
def browser_session(
    *, headless: bool, artifacts_dir: Path, timezone: ZoneInfo, run_id: str = ""
) -> Iterator[BrowserSession]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1400, "height": 1000},
            timezone_id=str(timezone),
            user_agent=_user_agent(browser.version),
        )
        context.set_default_timeout(STEP_TIMEOUT_MS)
        page = context.new_page()
        try:
            yield BrowserSession(
                context=context,
                page=page,
                artifacts_dir=artifacts_dir,
                run_id=run_id,
            )
        finally:
            context.close()
            browser.close()
