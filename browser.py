"""Browser context management and store switching.

Design note: we build a FRESH CONTEXT PER STORE rather than mutating one
long-lived context. Two reasons, both learned the hard way against the live
site:

  1. The site's own JS re-derives a store from geolocation on navigation and
     will happily overwrite a store you set after load. Seeding via
     `add_init_script` runs before any page script, so our value wins.
  2. A fresh context cannot leak the previous store's cookies or cache into
     the next store's pages -- which is exactly the contamination that would
     silently label one store's prices as another's.

Contexts are cheap; only the browser launch is expensive, and that is reused.
"""

from __future__ import annotations

import json
import re

import config


def _store_state(store: dict) -> dict:
    """The full localStorage payload representing 'this store is selected'."""
    sid = str(store["store_id"])
    province = store.get("province") or config.PROVINCE

    state = dict(config.AGE_GATE_STATE)
    state.update({
        config.STORE_ID_KEY: sid,
        config.STORE_HANDLE_KEY: store.get("handle", ""),
        config.PROVINCE_KEY: province,
        config.STORE_PICKUP_KEY: "Pickup",
        config.STORE_STATUS_KEY: "Open",
        config.STORE_OBJ_KEY: json.dumps({
            "address": {
                "street1": store.get("street", ""),
                "city": store.get("city", ""),
                "province": province,
                "zip": store.get("zip", ""),
                "latitude": store.get("latitude"),
                "longitude": store.get("longitude"),
            },
            "handle": store.get("handle", ""),
            "title": store.get("name", ""),
            "store_id": sid,
        }),
    })

    # Pin geolocation to the store itself, so the site's auto-select agrees
    # with us instead of fighting us.
    lat, lng = store.get("latitude"), store.get("longitude")
    if lat is not None and lng is not None:
        state[config.GEO_KEYS[0]] = str(lat)
        state[config.GEO_KEYS[1]] = str(lng)
    return state


def _seed_script(state: dict) -> str:
    """JS run before any page script, on every navigation in the context."""
    return (
        "(() => { const kv = " + json.dumps(state) + ";"
        " try { for (const [k,v] of Object.entries(kv)) localStorage.setItem(k, v); }"
        " catch (e) {} })();"
    )


async def launch(pw, headless: bool | None = None):
    headless = config.HEADLESS if headless is None else headless
    return await pw.chromium.launch(headless=headless)


async def store_context(browser, store: dict):
    """A context locked to `store`, already past the age gate."""
    sid = str(store["store_id"])
    province = store.get("province") or config.PROVINCE

    ctx = await browser.new_context(
        user_agent=config.USER_AGENT,
        viewport={"width": 1440, "height": 900},
        locale="en-CA",
        timezone_id="America/Edmonton",
    )
    ctx.set_default_timeout(config.NAV_TIMEOUT_MS)
    await ctx.add_init_script(_seed_script(_store_state(store)))
    # The cookie is what the server sees, so per-store pricing can render
    # server-side. Set it alongside the localStorage state.
    await ctx.add_cookies([
        {"name": "global_store_id", "value": sid,
         "domain": ".cannacabana.com", "path": "/"},
        {"name": "global_province", "value": province,
         "domain": ".cannacabana.com", "path": "/"},
    ])
    return ctx


async def read_active_store(page) -> dict:
    return await page.evaluate(
        "() => ({ id: localStorage.getItem('global_store_id'),"
        "         handle: localStorage.getItem('global_handle'),"
        "         province: localStorage.getItem('global_province') })"
    )


_LABEL_RE = re.compile(r"(?:Pickup|Delivery)\s*\|\s*([^\n]{1,80})")


async def store_label(page) -> str:
    """The store the header is actually showing, e.g. 'Haxton, Fort McMurray'."""
    try:
        text = await page.inner_text("body", timeout=5000)
    except Exception:
        return ""
    m = _LABEL_RE.search(text)
    return m.group(1).strip() if m else ""


async def assert_store(page, store: dict) -> str:
    """Confirm the active store is the one we asked for. Raises if not.

    A silent no-op here labels one store's prices as many different stores --
    the worst failure mode in this project. Fail loudly instead.
    """
    active = await read_active_store(page)
    want = str(store["store_id"])
    if str(active.get("id")) != want:
        raise RuntimeError(
            f"store switch did not take: wanted store_id={want} "
            f"({store.get('name')}), page reports {active!r}"
        )
    return await store_label(page)
