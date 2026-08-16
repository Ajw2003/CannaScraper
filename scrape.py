"""Per-store product scraping.

One row per (store x variant). Prices are read from the rendered DOM after
client-side pricing JS has settled -- see the note in config.SEL_* about why
plain HTTP cannot work here.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from datetime import datetime, timezone

import config
import browser as B

_MONEY = re.compile(r"(\d+(?:[.,]\d{2})?)")

# The page prices itself by calling app.cannacabana.com/.../scan-single-item/<id>.
# That <id> is normally the same store_id we selected -- but not always. One
# observed case: selecting 8230 (Brentwood) priced against 3130 (District).
# We do not call this API; we only watch which store it was asked about, so a
# mispriced row can be flagged instead of silently trusted.
_SCAN_URL = re.compile(r"scan-(?:single|multiple)-items?/(\d+)")

# That same response carries richer data than the DOM ever renders:
#
#   {"data":{"scanned-items":{"203012":"3,0.00,35.99,each,98.10,0.60,4.00,588:203012"},
#            "elitePrices":{"203012":"29.52"}}}
#
# The value is a positional CSV string. Identified fields:
#   [0] stock quantity   -- exact count; the DOM only ever says "In Stock"
#   [1] member price     -- 0.00 when the item has no member discount
#   [2] retail price     -- matches the rendered market price
#   [6] gram equivalence -- the site multiplies this by qty for its 30 g limit
# Verified against rendered pages: "0,15.44,18.99,..." rendered as market
# $18.99 / member $15.44. Fields 4 and 5 remain unidentified, so the whole
# string is retained in `api_raw` rather than guessed at.
#
# We do NOT call this endpoint. The page requests it on every load; we just
# read the reply instead of discarding it. No extra requests, no credentials.
_SCAN_STOCK, _SCAN_MEMBER, _SCAN_PRICE, _SCAN_EQUIV = 0, 1, 2, 6


def _parse_scan(blob: str) -> dict:
    """Decode one positional CSV value from `scanned-items`."""
    out: dict = {"api_raw": str(blob)}
    parts = [p.strip() for p in str(blob).split(",")]

    def num(i):
        try:
            return float(parts[i])
        except (IndexError, ValueError):
            return None

    stock = num(_SCAN_STOCK)
    out["api_stock"] = int(stock) if stock is not None else None
    out["api_member_price"] = num(_SCAN_MEMBER) or None   # 0.00 == no discount
    out["api_price"] = num(_SCAN_PRICE)
    out["api_equiv_g"] = num(_SCAN_EQUIV)
    return out


_THC = re.compile(r"THC[^0-9]{0,20}([\d.]+\s*-?\s*[\d.]*\s*%?)", re.I)
_CBD = re.compile(r"CBD[^0-9]{0,20}([\d.]+\s*-?\s*[\d.]*\s*%?)", re.I)
_TAG = re.compile(r"<[^>]+>")


def _money(text: str | None) -> float | None:
    if not text:
        return None
    m = _MONEY.search(text.replace(",", ""))
    return float(m.group(1)) if m else None


def _cannabinoids(variant: dict) -> tuple[str, str]:
    """THC/CBD are product-level, so read them from the catalog, not the page."""
    blob = _TAG.sub(" ", variant.get("body_html", "") or "")
    blob += " " + " ".join(variant.get("tags") or [])
    thc = _THC.search(blob)
    cbd = _CBD.search(blob)
    return (thc.group(1).strip() if thc else "",
            cbd.group(1).strip() if cbd else "")


# Read both prices and the buy-button state in a single round trip.
_EXTRACT_JS = """
(sel) => {
  const pick = (s) => {
    const el = document.querySelector(s);
    return el ? el.textContent.trim() : null;
  };
  const btn = document.querySelector(sel.btn);
  const info = document.querySelector(sel.info);
  return {
    market: pick(sel.market),
    member: pick(sel.member),
    market_exists: !!document.querySelector(sel.market),
    info_present: !!info,
    btn_text: btn ? btn.innerText.trim() : null,
    btn_disabled: btn ? !!btn.disabled : null,
    info_text: info ? info.innerText : "",
  };
}
"""


async def _wait_for_price(page) -> bool:
    """Wait until the price cell has settled.

    Settled means a real number OR the site's "N.A." not-carried marker. We
    must explicitly exclude the "Loading" placeholder, otherwise we race the
    pricing JS and record a blank.
    """
    try:
        await page.wait_for_function(
            "(a) => { const e = document.querySelector(a.sel);"
            "         if (!e) return false;"
            "         const t = (e.textContent || '').trim().toLowerCase();"
            "         if (!t || t === a.placeholder) return false;"
            "         return /\\d/.test(t) || a.notCarried.some(x => t === x); }",
            arg={"sel": config.SEL_MARKET_PRICE,
                 "placeholder": config.PRICE_PLACEHOLDER,
                 "notCarried": list(config.NOT_CARRIED_TOKENS)},
            timeout=config.PRICE_SETTLE_MS,
        )
        return True
    except Exception:
        return False


async def scrape_variant(page, store: dict, variant: dict,
                         captured: dict | None = None) -> dict:
    """Scrape one product at the currently-selected store."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    handle = variant["handle"]
    url = f"{config.BASE}/products/{handle}?sID={store['store_id']}"
    thc, cbd = _cannabinoids(variant)

    row = {
        "scraped_at": now,
        "store_id": str(store["store_id"]),
        "store_name": store.get("name", ""),
        "city": store.get("city", ""),
        "province": store.get("province", ""),
        "sku": variant.get("sku", ""),
        "handle": handle,
        "title": variant.get("title", ""),
        "brand": variant.get("brand", ""),
        "category": variant.get("category", ""),
        "size": variant.get("size", ""),
        "url": url,
        "price": None,
        "member_price": None,
        "default_price": variant.get("default_price"),
        "available": None,
        "carried": None,
        "stock_text": "",
        "thc": thc,
        "cbd": cbd,
        "store_label": "",
        "api_store_id": "",
        "store_id_match": None,
        "api_stock": None,
        "api_member_price": None,
        "api_price": None,
        "api_elite_price": None,
        "api_equiv_g": None,
        "api_raw": "",
        "status": "ok",
        "error": "",
    }

    try:
        if captured is not None:
            captured["api_store_id"] = None
            captured["items"] = {}
            captured["elite"] = {}
        await page.goto(url, wait_until="domcontentloaded")
        settled = await _wait_for_price(page)

        d = await page.evaluate(_EXTRACT_JS, {
            "market": config.SEL_MARKET_PRICE,
            "member": config.SEL_MEMBER_PRICE,
            "btn": config.SEL_ADD_BUTTON,
            "info": config.SEL_PRODUCT_INFO,
        })

        market_raw = (d.get("market") or "").strip()
        row["store_label"] = await B.store_label(page)

        # Which store did the site actually price against?
        api_id = (captured or {}).get("api_store_id")
        if api_id:
            row["api_store_id"] = str(api_id)
            row["store_id_match"] = 1 if str(api_id) == str(store["store_id"]) else 0

        # Fold in the scan response the page already fetched. Its body is read
        # in a background task, so give it a brief moment to land -- but never
        # block for long, since the DOM remains the fallback.
        sku = str(variant.get("sku") or "")
        if captured is not None and sku:
            for _ in range(20):                      # <= ~2s
                if sku in captured.get("items", {}):
                    break
                await asyncio.sleep(0.1)

        scan = (captured or {}).get("items", {}).get(sku)
        if scan:
            row.update(_parse_scan(scan))
        elite = (captured or {}).get("elite", {}).get(sku)
        if elite not in (None, ""):
            try:
                row["api_elite_price"] = float(elite) or None
            except (TypeError, ValueError):
                pass

        # --- carried? -------------------------------------------------------
        # The API answer is authoritative: retail price 0 means the store does
        # not carry it. Only fall back to reading the DOM when no scan arrived,
        # because the DOM cannot distinguish "not carried" from "still loading".
        api_price = row.get("api_price")
        if api_price is not None:
            carried = api_price > 0
        else:
            blank = (not d.get("market_exists")) or market_raw == ""
            carried = (not blank) and market_raw.lower() not in config.NOT_CARRIED_TOKENS
        row["carried"] = 1 if carried else 0

        if carried:
            # Prefer the rendered value; fall back to the API's.
            row["price"] = _money(market_raw) or api_price
            row["member_price"] = (_money(d.get("member"))
                                   or row.get("api_member_price")) or None

        btn_text = (d.get("btn_text") or "").strip()
        info = d.get("info_text") or ""
        m = re.search(r"(?i)\b(out of stock|in stock|low stock|sold out)\b", info)

        if not carried:
            row["stock_text"] = "Not carried"
            row["available"] = 0
        elif row.get("api_stock") is not None:
            row["available"] = 1 if row["api_stock"] > 0 else 0
            row["stock_text"] = "In Stock" if row["api_stock"] > 0 else "Sold Out"
        else:
            row["stock_text"] = (m.group(1).title() if m else btn_text)
            sold_out = bool(d.get("btn_disabled")) or bool(
                re.search(r"(?i)sold out|out of stock", f"{btn_text} {row['stock_text']}")
            )
            row["available"] = 0 if sold_out else 1

        # "Carried, but we never resolved a price" is not a real state -- it
        # means the price cell was still on its placeholder. Error so it
        # retries, rather than silently recording a blank price as valid.
        if carried and row["price"] is None:
            row["status"] = "error"
            row["error"] = f"price never resolved (cell={market_raw[:30]!r})"

        # Only a page that never rendered its product block is a real failure.
        # A settled page with no price is a legitimate "not carried".
        if not settled and not d.get("info_present"):
            row["status"] = "error"
            row["error"] = "product page never rendered"
            await _dump(page, store, handle)

    except Exception as e:                                   # noqa: BLE001
        row["status"] = "error"
        row["error"] = f"{type(e).__name__}: {e}"[:300]
        await _dump(page, store, handle)

    return row


async def _dump(page, store: dict, handle: str) -> None:
    """Save the HTML of a failed page so it can be diagnosed later."""
    try:
        os.makedirs(config.RAW_DIR, exist_ok=True)
        path = os.path.join(config.RAW_DIR, f"{store['store_id']}__{handle}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(await page.content())
    except Exception:
        pass


async def scrape_store(browser, store: dict, variants: list[dict],
                       verbose: bool = True) -> list[dict]:
    """Scrape every target variant at one store, in a context locked to it."""
    ctx = await B.store_context(browser, store)
    page = await ctx.new_page()
    rows: list[dict] = []

    # Passively observe the pricing call the page makes anyway (see _SCAN_URL):
    # which store it asked about, and the richer payload it returns.
    captured: dict = {"api_store_id": None, "items": {}, "elite": {}}

    async def _read_scan(resp) -> None:
        try:
            data = await resp.json()
        except Exception:
            return
        payload = (data or {}).get("data") or {}
        for sku, blob in (payload.get("scanned-items") or {}).items():
            captured["items"][str(sku)] = blob
        for sku, val in (payload.get("elitePrices") or {}).items():
            captured["elite"][str(sku)] = val

    def _on_response(resp):
        m = _SCAN_URL.search(resp.url)
        if m:
            captured["api_store_id"] = m.group(1)
            # Read the body promptly, while it is still retained.
            asyncio.create_task(_read_scan(resp))

    page.on("response", _on_response)

    try:
        # Land on the product page first, then assert the store took. Doing
        # this before scraping means a bad switch fails fast, loudly, and
        # cannot be mistaken for real data.
        first = variants[0]["handle"]
        await page.goto(f"{config.BASE}/products/{first}?sID={store['store_id']}",
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        label = await B.assert_store(page, store)
        if verbose:
            print(f"    store confirmed: {label or '(label not found)'}")

        for i, v in enumerate(variants, 1):
            # Pricing JS occasionally never settles on a first load. Retry
            # before accepting an error, otherwise a transient blip is
            # indistinguishable from real "no price" data.
            for attempt in range(1, config.MAX_RETRIES + 1):
                row = await scrape_variant(page, store, v, captured)
                if row["status"] == "ok":
                    break
                if attempt < config.MAX_RETRIES:
                    if verbose:
                        print(f"    ... retry {attempt}/{config.MAX_RETRIES - 1} "
                              f"for {v.get('sku') or v['handle']}")
                    await asyncio.sleep(random.uniform(*config.DELAY_RANGE))
            rows.append(row)
            if verbose:
                flag = "OK " if row["status"] == "ok" else "ERR"
                price = f"${row['price']:.2f}" if row["price"] else "-"
                memb = f"${row['member_price']:.2f}" if row["member_price"] else "-"
                warn = "" if row["store_id_match"] in (1, None) else \
                    f"  <-- PRICED AS STORE {row['api_store_id']}"
                qty = "" if row["api_stock"] is None else f" x{row['api_stock']}"
                print(f"    [{flag}] {v['sku'] or '-':>8}  {price:>9} / {memb:>9}"
                      f"  {(row['stock_text'] or '-') + qty:<16} {v['title'][:34]}{warn}")
            if i < len(variants):
                await asyncio.sleep(random.uniform(*config.DELAY_RANGE))
    finally:
        await ctx.close()

    return rows
