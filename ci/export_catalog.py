"""Export the product catalogue to a static JSON file, for the static site.

`catalog.py` already reads/writes `data/catalog.json` (the cache used by the
running server and CLI, one row per variant, everything). This script slims
that down to what the browser-side port of the API (site/static-api.js)
actually needs to answer /api/search, /api/categories and /api/results, and
writes it under a distinct output directory so it does not collide with the
full cache the scraper itself uses.

    python ci/export_catalog.py --out DIR

Also copies stores.json and geocode.json into DIR, since the static site
needs those published alongside the province files too, and this is the one
step that runs independently of any particular province's scrape.

Runs anywhere with Python 3.11 and no third-party packages -- it reads the
catalogue already on disk (catalog.load_catalog()) rather than downloading
one, and refreshes it first only when it has gone stale, exactly like the
running app does on startup (server.py:_refresh_stale_catalog).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import catalog  # noqa: E402  (needs the repo root on sys.path first)
import config  # noqa: E402
import scrape  # noqa: E402  (scrape._cannabinoids: the live check's THC/CBD source)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export the product catalogue to JSON")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--refresh", action="store_true",
                    help="download a fresh catalogue first, unconditionally")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    # Same rule the running app uses on startup: top up a stale catalogue,
    # but never make a plain export block on a multi-minute download when the
    # one on disk is still fine.
    if args.refresh or catalog.catalog_is_stale():
        try:
            catalog.refresh_catalog(verbose=True)
        except Exception as e:                          # noqa: BLE001
            print(f"warning: catalogue refresh failed ({e}); "
                  f"exporting the copy already on disk", file=sys.stderr)

    try:
        rows = catalog.load_catalog()
    except catalog.CatalogUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with open(config.CATALOG_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("fetched_at"):
            generated_at = cached["fetched_at"]
    except (OSError, ValueError, KeyError):
        pass

    seen: set[str] = set()
    products = []
    for v in rows:
        sku = v.get("sku") or ""
        if not sku or sku in seen:
            continue
        seen.add(sku)
        products.append({
            "sku": sku, "title": v.get("title", ""), "brand": v.get("brand", ""),
            "size": v.get("size", ""), "category": v.get("category", ""),
            "handle": v.get("handle", ""), "image": v.get("image", ""),
            # Needed by the browser's own live check (static-api.js): the
            # scan-multiple-items payload is keyed by {sku: variant_id}, same
            # as fetchers/api_fetcher.py.
            "variant_id": v.get("variant_id"),
        })
        # A live check's row takes THC/CBD from the catalogue text, not the
        # scan (fetchers/api_fetcher.py:_row -> scrape._cannabinoids). The
        # page can't run that regex over body_html it doesn't have, so the
        # result is published instead; empty values are left out to save space.
        thc, cbd = scrape._cannabinoids(v)
        if thc:
            products[-1]["thc"] = thc
        if cbd:
            products[-1]["cbd"] = cbd

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / "catalog.json"
    # The live check's skip list (config.SCAN_SKIP_STORES; see
    # fetchers.partition_scannable) travels with the catalogue so the page
    # skips the same stores the app did.
    text = json.dumps({"generated_at": generated_at, "products": products,
                       "scan_skip_stores": sorted(str(s) for s in config.SCAN_SKIP_STORES)},
                      separators=(",", ":"), ensure_ascii=False)
    out_path.write_text(text, encoding="utf-8")

    for name in ("stores.json", "geocode.json"):
        src = Path(config.STORES_CACHE if name == "stores.json"
                   else config.GEOCODE_CACHE)
        if src.exists():
            shutil.copyfile(src, out_dir / name)
        else:
            print(f"note: {src} does not exist yet; not copied", file=sys.stderr)

    print(f"catalog: {len(products)} products, {len(text.encode('utf-8'))} bytes "
          f"-> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
