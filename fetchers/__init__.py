"""Pluggable ways of getting per-store price/stock.

Everything upstream (main.py, server.py) talks to a Fetcher and never learns
which backend ran. That keeps the browser-vs-internal-API decision open, and
lets the two be diffed against each other for correctness.

    async with get_fetcher() as f:
        rows = await f.fetch(store, variants)
"""

from __future__ import annotations

import config


def get_fetcher(name: str | None = None):
    """Build the configured fetcher. `name` overrides config.FETCHER."""
    name = (name or config.FETCHER or "browser").lower()

    if name == "browser":
        from .browser_fetcher import BrowserFetcher
        return BrowserFetcher()
    if name == "api":
        from .api_fetcher import ApiFetcher
        return ApiFetcher()

    raise ValueError(
        f"unknown fetcher {name!r}; expected 'browser' or 'api' "
        f"(set config.FETCHER)"
    )
