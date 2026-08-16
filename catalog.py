"""Public Shopify catalog + watchlist resolution.

`/products.json` is open (no auth, no age gate) and returns the whole catalog
250 products at a time. We flatten it to one row per *variant*, because the
variant is the unit that carries a SKU and a price.

IMPORTANT: the `price` and `available` fields here are shop-level defaults.
They are NOT per-store. Per-store values come from the browser pass in
scrape.py. Never report these as location-specific.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone

import config


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", errors="ignore"))


def fetch_catalog(verbose: bool = True) -> list[dict]:
    """Page through /products.json and flatten to variant rows."""
    rows, page = [], 1
    while True:
        data = _fetch_json(f"{config.PRODUCTS_JSON}?limit=250&page={page}")
        products = data.get("products", [])
        if not products:
            break
        for p in products:
            for v in p.get("variants", []):
                rows.append({
                    "product_id": p.get("id"),
                    "handle": p.get("handle", ""),
                    "title": p.get("title", ""),
                    "brand": p.get("vendor", ""),
                    "category": p.get("product_type", ""),
                    "tags": p.get("tags", []),
                    "body_html": p.get("body_html", "") or "",
                    "variant_id": v.get("id"),
                    "sku": str(v.get("sku") or ""),
                    "size": v.get("option1") or "",
                    "default_price": v.get("price"),
                    "default_compare_at": v.get("compare_at_price"),
                    "default_available": v.get("available"),
                })
        if verbose:
            print(f"  page {page:>2}: {len(products):>3} products  (variants so far: {len(rows)})")
        page += 1
        time.sleep(1.0)
    return rows


def _cache_age_hours(path: str) -> float:
    if not os.path.exists(path):
        return float("inf")
    return (time.time() - os.path.getmtime(path)) / 3600.0


def get_catalog(refresh: bool = False, verbose: bool = True) -> list[dict]:
    if not refresh and _cache_age_hours(config.CATALOG_CACHE) < config.CATALOG_MAX_AGE_H:
        with open(config.CATALOG_CACHE, encoding="utf-8") as f:
            return json.load(f)["variants"]

    if verbose:
        print("Fetching catalog from public Shopify JSON...")
    rows = fetch_catalog(verbose=verbose)
    with open(config.CATALOG_CACHE, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(),
                   "variants": rows}, f)
    return rows


# --- Watchlist -------------------------------------------------------------

_URL_HANDLE = re.compile(r"/products/([a-z0-9\-_]+)", re.I)


def _read_watchlist() -> list[str]:
    if not os.path.exists(config.WATCHLIST):
        return []
    out = []
    with open(config.WATCHLIST, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def resolve_watchlist(catalog: list[dict] | None = None,
                      verbose: bool = True) -> list[dict]:
    """Match each watchlist line to a catalog variant.

    Order: exact SKU -> exact handle -> case-insensitive title substring.
    Unmatched lines are reported loudly and skipped; a silently dropped entry
    is an easy way to end up with a quietly incomplete dataset.
    """
    catalog = catalog if catalog is not None else get_catalog(verbose=verbose)
    by_sku: dict[str, dict] = {}
    by_handle: dict[str, list[dict]] = {}
    for v in catalog:
        if v["sku"]:
            by_sku.setdefault(v["sku"], v)
        by_handle.setdefault(v["handle"], []).append(v)

    targets, unmatched = [], []
    seen: set = set()

    for line in _read_watchlist():
        hits: list[dict] = []

        if line in by_sku:
            hits = [by_sku[line]]
        else:
            m = _URL_HANDLE.search(line)
            handle = (m.group(1) if m else line).lower()
            if handle in by_handle:
                hits = by_handle[handle]
            else:
                needle = line.lower()
                hits = [v for v in catalog if needle in v["title"].lower()]

        if not hits:
            unmatched.append(line)
            continue

        for v in hits:
            key = v["variant_id"]
            if key not in seen:
                seen.add(key)
                targets.append(v)

    if verbose:
        print(f"\nWatchlist: {len(targets)} variant(s) resolved.")
        for v in targets:
            print(f"  [{v['sku'] or '-':>8}] {v['title']} ({v['size']}) — {v['brand']}")
        if unmatched:
            print(f"\n  !! {len(unmatched)} watchlist line(s) MATCHED NOTHING:")
            for u in unmatched:
                print(f"     - {u}")

    return targets


if __name__ == "__main__":
    import sys

    cat = get_catalog(refresh="--refresh" in sys.argv)
    print(f"\nCatalog: {len(cat)} variants")
    prods = len({v['product_id'] for v in cat})
    print(f"Distinct products: {prods}")
    cats: dict[str, int] = {}
    for v in cat:
        cats[v["category"]] = cats.get(v["category"], 0) + 1
    print("\nTop categories:")
    for c, n in sorted(cats.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {c or '(blank)':<28} {n}")
    resolve_watchlist(cat)
