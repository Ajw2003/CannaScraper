"""Canna Cabana cross-location scraper — entry point.

    python main.py --limit 2            # smoke test on two stores
    python main.py                      # full province sweep
    python main.py --resume <run_id>    # continue an interrupted sweep

Run `python main.py --help` for everything.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from datetime import datetime, timezone

import catalog
import config
import db
import fetchers
import report
import stores as S


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Canna Cabana cross-location scraper")
    p.add_argument("--province", default=config.PROVINCE,
                   help=f"province to sweep (default: {config.PROVINCE})")
    p.add_argument("--limit", type=int, default=None,
                   help="only scrape the first N stores")
    p.add_argument("--store", action="append", default=None,
                   help="scrape a specific store_id (repeatable)")
    p.add_argument("--product", action="append", default=None, metavar="SKU",
                   help="scrape THIS product instead of watchlist.txt "
                        "(SKU, handle, URL, or title text; repeatable)")
    p.add_argument("--refresh-stores", action="store_true",
                   help="re-fetch the store registry")
    p.add_argument("--refresh-catalog", action="store_true",
                   help="re-fetch the product catalog")
    p.add_argument("--headed", action="store_true",
                   help="show the browser window")
    p.add_argument("--resume", metavar="RUN_ID", default=None,
                   help="continue a previous run, skipping finished stores")
    p.add_argument("--near", default=None, metavar="PLACE",
                   help='check the stores nearest here: "Calgary, AB", '
                        'a postal code, or "lat,lng" (default: config.HOME)')
    p.add_argument("--top", type=int, default=None, metavar="N",
                   help=f"how many nearest stores to check "
                        f"(default {config.DEFAULT_TOP}); use --all for every store")
    p.add_argument("--all", action="store_true",
                   help="check every store in the province, not just the nearest")
    p.add_argument("--cached", action="store_true",
                   help="don't scrape at all -- show what we already know, instantly")
    p.add_argument("--refresh", action="store_true",
                   help="scrape even if recent cached results exist")
    p.add_argument("--no-report", action="store_true",
                   help="skip building/opening the HTML report")
    p.add_argument("--fetcher", default=None, choices=["browser", "api"],
                   help=f"backend to fetch prices with (default {config.FETCHER})")
    p.add_argument("--csv", default=config.CSV_PATH)
    p.add_argument("--db", default=config.DB_PATH)
    return p.parse_args(argv)


def pick_target(terms: list[str], cat: list[dict]) -> list[dict]:
    """Resolve a product, asking which one when the text is ambiguous."""
    exact = catalog.resolve_terms(terms, cat, verbose=False, label="Product")
    if len(exact) <= 1:
        return exact

    # Several variants matched. If they're all one product, take them all;
    # otherwise ask, because "grape gas" could mean a vape or a pre-roll.
    if len({v["product_id"] for v in exact}) == 1:
        return exact

    print(f"\n{len(exact)} products match {' '.join(terms)!r}:\n")
    for i, v in enumerate(exact, 1):
        print(f"  {i:>2}) {v['title'][:44]:<44} {v['size']:<9} {v['brand'][:18]}")
    print(f"  {0:>2}) all of them")

    try:
        raw = input("\nWhich one? [1] ").strip() or "1"
        n = int(raw)
    except (ValueError, EOFError, KeyboardInterrupt):
        n = 1
    if n == 0:
        return exact
    if 1 <= n <= len(exact):
        return [exact[n - 1]]
    return [exact[0]]


async def run(args: argparse.Namespace) -> int:
    started = time.time()
    run_id = args.resume or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print("=" * 74)
    print(f"Canna Cabana scraper — run {run_id}")
    print("=" * 74)

    # --- targets -----------------------------------------------------------
    cat = catalog.get_catalog(refresh=args.refresh_catalog)
    if args.product:
        targets = pick_target(args.product, cat)
        if not targets:
            print(f"\nNothing matched {' '.join(args.product)!r}. "
                  'Try: python catalog.py --find "part of the name"')
            return 2
        print(f"\nLooking for: {targets[0]['title']} "
              f"({targets[0]['size']}) — {targets[0]['brand']}"
              + (f"  +{len(targets)-1} more variant(s)" if len(targets) > 1 else ""))
    else:
        targets = catalog.resolve_watchlist(cat)
        if not targets:
            print("\nNothing to scrape: watchlist.txt resolved to 0 products.")
            return 2

    # --- stores ------------------------------------------------------------
    store_list = S.get_stores(province=args.province,
                              refresh=args.refresh_stores,
                              limit=args.limit)
    if args.store:
        wanted = {str(s) for s in args.store}
        store_list = [s for s in store_list if s["store_id"] in wanted]
    if not store_list:
        print(f"\nNo stores matched province={args.province!r}.")
        return 2

    # Rank by distance unless the user named specific stores or asked for all.
    location_label = ""
    if not args.store and not args.all:
        loc = S.resolve_location(args.near)
        if loc:
            top = args.top if args.top is not None else config.DEFAULT_TOP
            store_list = S.nearest(store_list, loc[0], loc[1], top)
            location_label = args.near or str(config.HOME)
            print(f"Nearest {len(store_list)} store(s) to {location_label} "
                  f"({loc[0]:.3f}, {loc[1]:.3f})")
        elif args.near:
            print(f"\nCouldn't locate {args.near!r}. Use a city, postal code, "
                  f'or "lat,lng".')
            return 2

    conn = db.connect(args.db)

    # --- cached first ------------------------------------------------------
    skus = [str(v.get("sku")) for v in targets if v.get("sku")]
    store_ids = [s["store_id"] for s in store_list]
    cached = db.latest_observations(conn, skus, store_ids)
    age = db.cache_age_hours(cached)

    if cached and (args.cached or
                   (not args.refresh and age is not None
                    and age < config.CACHE_FRESH_H)):
        by_id = {s["store_id"]: s for s in store_list}
        for r in cached:
            r["distance_km"] = by_id.get(r["store_id"], {}).get("distance_km")
        show_results(cached, age, args, targets, location_label,
                     live=False, conn=conn)
        if not args.cached:
            print(f"\n(Cached from {age:.1f}h ago. Use --refresh to check now.)")
        return 0

    if args.cached:
        print("\nNothing cached for that product yet — run without --cached "
              "to check the stores.")
        return 1

    skip: set[str] = set()
    if args.resume:
        skip = db.done_store_ids(conn, run_id)
        print(f"\nResuming: {len(skip)} store(s) already complete, skipping them.")

    todo = [s for s in store_list if s["store_id"] not in skip]
    print(f"\nProvince : {args.province}")
    print(f"Stores   : {len(todo)} to scrape ({len(store_list)} in scope)")
    print(f"Products : {len(targets)} variant(s)")
    print(f"Requests : ~{len(todo) * len(targets)} product page loads\n")

    ok_rows = err_rows = 0
    failed_stores: list[tuple[str, str]] = []

    fetcher = fetchers.get_fetcher(args.fetcher)
    if args.headed and hasattr(fetcher, "_headless"):
        fetcher._headless = False

    async with fetcher as f:
        for i, st in enumerate(todo, 1):
            dist = (f"  {st['distance_km']:.1f} km"
                    if st.get("distance_km") is not None else "")
            print(f"[{i}/{len(todo)}] {st['name']} — {st['city']}"
                  f" (id={st['store_id']}){dist}")
            try:
                rows = await f.fetch(st, targets)
                db.write_rows(conn, run_id, rows)     # persist per store
                ok_rows += sum(1 for r in rows if r["status"] == "ok")
                err_rows += sum(1 for r in rows if r["status"] != "ok")
            except Exception as e:                     # noqa: BLE001
                # One bad store must never kill a 92-store sweep.
                print(f"    !! store failed: {type(e).__name__}: {e}")
                failed_stores.append((st["store_id"], str(e)[:120]))

            if i < len(todo):
                await asyncio.sleep(random.uniform(*config.DELAY_RANGE))

    # --- output ------------------------------------------------------------
    n = db.export_csv(conn, run_id, args.csv)
    elapsed = time.time() - started

    print("\n" + "=" * 74)
    print(f"Done in {elapsed/60:.1f} min — {ok_rows} rows ok, {err_rows} errored")
    if failed_stores:
        print(f"{len(failed_stores)} store(s) failed entirely:")
        for sid, msg in failed_stores:
            print(f"   {sid}: {msg}")
        print(f"Re-run to retry just those:  python main.py --resume {run_id}")
    print(f"CSV : {args.csv} ({n} rows)")
    print(f"DB  : {args.db} (run_id {run_id})")

    bad = db.mismatched_stores(conn, run_id)
    if bad:
        print(f"\n!! {len(bad)} store(s) were priced as a DIFFERENT store —"
              f" treat these rows as suspect (store_id_match=0):")
        for sid, name, city, api_id in bad:
            print(f"   {sid} {name} ({city})  ->  priced as store {api_id}")

    fresh = db.latest_observations(conn, skus, store_ids)
    by_id = {s["store_id"]: s for s in store_list}
    for r in fresh:
        r["distance_km"] = by_id.get(r["store_id"], {}).get("distance_km")
    show_results(fresh, 0.0, args, targets, location_label, live=True, conn=conn)

    conn.close()
    return 0 if not failed_stores else 1


def show_results(rows: list[dict], age: float | None, args, targets,
                 location_label: str = "", live: bool = True, conn=None) -> None:
    """Print the ranked answer, then build the HTML report."""
    rows = sorted(rows, key=lambda r: (
        str(r.get("sku")),
        -(r.get("api_stock") or 0),
        0 if r.get("available") else 1,
        r.get("distance_km") if r.get("distance_km") is not None else 9e9,
    ))
    have = [r for r in rows if r.get("available")]

    print("\n" + "-" * 74)
    if have:
        show_dist = any(r.get("distance_km") is not None for r in have)
        print(f"IN STOCK at {len(have)} of {len(rows)} store(s) checked:\n")
        hdr = f"  {'units':>5}  {'store':<26} {'city':<16}"
        if show_dist:
            hdr += f" {'km':>6}"
        print(hdr + f" {'price':>8} {'member':>8}")
        last = None
        for r in have:
            if r.get("sku") != last:
                print(f"\n  {r.get('title')} ({r.get('size')})  [SKU {r.get('sku')}]")
                last = r.get("sku")
            d = (f" {r['distance_km']:>6.1f}"
                 if show_dist and r.get("distance_km") is not None
                 else (" " * 7 if show_dist else ""))
            price = f"${r['price']:.2f}" if r.get("price") else "-"
            memb = f"${r['member_price']:.2f}" if r.get("member_price") else "-"
            print(f"  {r.get('api_stock') if r.get('api_stock') is not None else '?':>5}"
                  f"  {str(r.get('store_name'))[:26]:<26} {str(r.get('city'))[:16]:<16}"
                  f"{d} {price:>8} {memb:>8}")
    else:
        checked = len(rows)
        print(f"NOT IN STOCK at any of the {checked} store(s) checked."
              + ("" if checked > 5 else "  Try --top 25 to widen the search."))

    if not args.no_report and rows:
        q = " ".join(args.product) if args.product else "watchlist"
        path = report.write_and_open(rows, query=q, age_hours=age,
                                     location=location_label)
        print(f"\nReport: {path}")


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run with --resume <run_id> to continue.")
        sys.exit(130)
