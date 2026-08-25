"""Pluggable ways of getting per-store price/stock.

Everything upstream (main.py, server.py) talks to a Fetcher and never learns
which backend ran. That keeps the browser-vs-internal-API decision open, and
lets the two be diffed against each other for correctness.

    async with get_fetcher() as f:
        rows = await f.fetch(store, variants)
"""

from __future__ import annotations

import importlib.util

import config


def have_browser() -> bool:
    """Is the browser backend usable here?

    The packaged app leaves Playwright out -- it and its Chromium are ~350 MB
    for a backend that `--compare` showed agrees with the API on every field.
    The source checkout still has it, so this is a runtime question, not a
    build-time one, and the UI asks it before offering the option.
    """
    return importlib.util.find_spec("playwright") is not None


def available() -> list[str]:
    """Backend names this installation can actually run."""
    return ["api"] + (["browser"] if have_browser() else [])


def check_available(name: str | None = None) -> str:
    """Resolve a backend name, or explain why it cannot be used."""
    name = (name or config.FETCHER or "browser").lower()
    if name not in ("api", "browser"):
        raise ValueError(f"unknown fetcher {name!r}; expected 'browser' or 'api'")
    if name == "browser" and not have_browser():
        raise ValueError(
            "the browser backend needs Playwright, which is not installed in "
            "the packaged app -- use the fast API source instead"
        )
    return name


def get_fetcher(name: str | None = None):
    """Build the configured fetcher. `name` overrides config.FETCHER."""
    name = check_available(name)

    if name == "browser":
        from .browser_fetcher import BrowserFetcher
        return BrowserFetcher()
    from .api_fetcher import ApiFetcher
    return ApiFetcher()
