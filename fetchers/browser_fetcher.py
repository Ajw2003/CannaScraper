"""Playwright backend -- the proven path.

Drives a real Chromium so the site's own JavaScript prices each store, which
is required because the product HTML is byte-identical for every store (see
config.SEL_*). No credentials involved.

Cost: roughly 8 seconds per store. Correct, and slow.
"""

from __future__ import annotations

from playwright.async_api import async_playwright

import browser as B
import scrape


class BrowserFetcher:
    name = "browser"
    # No internal pacing: the caller adds config.DELAY_RANGE between stores to
    # keep the request rate polite.
    paces_itself = False

    def __init__(self, headless: bool | None = None):
        self._headless = headless
        self._pw = None
        self._browser = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        self._browser = await B.launch(self._pw, headless=self._headless)
        return self

    async def __aexit__(self, *exc):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def fetch(self, store: dict, variants: list[dict],
                    verbose: bool = True) -> list[dict]:
        """One row per variant at `store`."""
        if self._browser is None:
            raise RuntimeError("use BrowserFetcher as an async context manager")
        return await scrape.scrape_store(self._browser, store, variants,
                                         verbose=verbose)
