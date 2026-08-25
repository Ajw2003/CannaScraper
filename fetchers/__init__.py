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


def scan_skip_ids() -> set[str]:
    """Stores the scan endpoint will not serve.

    config.SCAN_SKIP_STORES is the shipped default; settings.json may add to it
    so a packaged copy can be corrected without a rebuild. Merged, never
    replaced -- a local list should not silently drop a known-bad store.
    """
    ids = {str(s) for s in getattr(config, "SCAN_SKIP_STORES", set())}
    try:
        import auth
        extra = auth.load().get("scan_skip_stores") or []
        ids |= {str(s) for s in extra}
    except Exception:                                             # noqa: BLE001
        # Settings are optional; a missing or malformed file must not stop a
        # live check from running.
        pass
    return ids


def partition_scannable(stores: list[dict],
                        last_attempt: dict[str, str] | None = None
                        ) -> tuple[list[dict], list[dict]]:
    """Split stores into (worth contacting, skipping this time).

    A skipped store is re-tried once every config.SCAN_SKIP_RETRY_DAYS, so a
    store that gets fixed upstream returns to service on its own rather than
    staying dead until someone edits the config.

    `last_attempt` comes from db.last_scan_attempt(); pass None to skip
    unconditionally.
    """
    from datetime import datetime, timezone

    skip_ids = scan_skip_ids()
    if not skip_ids:
        return list(stores), []

    days = getattr(config, "SCAN_SKIP_RETRY_DAYS", 7)
    now = datetime.now(timezone.utc)

    keep, skipped = [], []
    for store in stores:
        sid = str(store.get("store_id") or "")
        if sid not in skip_ids:
            keep.append(store)
            continue

        due = False
        stamp = (last_attempt or {}).get(sid)
        if days <= 0:
            due = True
        elif stamp:
            try:
                when = datetime.fromisoformat(stamp)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                due = (now - when).total_seconds() >= days * 86400
            except (TypeError, ValueError):
                due = True
        else:
            # Never attempted, so there is nothing to base a skip on.
            due = True

        (keep if due else skipped).append(store)

    return keep, skipped


def get_fetcher(name: str | None = None):
    """Build the configured fetcher. `name` overrides config.FETCHER."""
    name = check_available(name)

    if name == "browser":
        from .browser_fetcher import BrowserFetcher
        return BrowserFetcher()
    from .api_fetcher import ApiFetcher
    return ApiFetcher()
