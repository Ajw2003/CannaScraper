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

from playwright.async_api import async_playwright

import browser as B
import catalog
import config
import db
import scrape
import stores as S


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Canna Cabana cross-location scraper")
    p.add_argument("--province", default=config.PROVINCE,
                   help=f"province to sweep (default: {config.PROVINCE})")
    p.add_argument("--limit", type=int, default=None,
                   help="only scrape the first N stores")
    p.add_argument("--store", action="append", default=None,
                   help="scrape a specific store_id (repeatable)")
    p.add_argument("--refresh-stores", action="store_true",
                   help="re-fetch the store registry")
    p.add_argument("--refresh-catalog", action="store_true",
                   help="re-fetch the product catalog")
    p.add_argument("--headed", action="store_true",
                   help="show the browser window")
    p.add_argument("--resume", metavar="RUN_ID", default=None,
                   help="continue a previous run, skipping finished stores")
    p.add_argument("--csv", default=config.CSV_PATH)
    p.add_argument("--db", default=config.DB_PATH)
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    started = time.time()
    run_id = args.resume or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print("=" * 74)
    print(f"Canna Cabana scraper — run {run_id}")
    print("=" * 74)

    # --- targets -----------------------------------------------------------
    cat = catalog.get_catalog(refresh=args.refresh_catalog)
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

    conn = db.connect(args.db)

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

    async with async_playwright() as pw:
        brwsr = await B.launch(pw, headless=not args.headed)
        try:
            for i, st in enumerate(todo, 1):
                head = f"[{i}/{len(todo)}] {st['name']} — {st['city']} (id={st['store_id']})"
                print(head)
                try:
                    rows = await scrape.scrape_store(brwsr, st, targets)
                    db.write_rows(conn, run_id, rows)     # persist per store
                    ok_rows += sum(1 for r in rows if r["status"] == "ok")
                    err_rows += sum(1 for r in rows if r["status"] != "ok")
                except Exception as e:                     # noqa: BLE001
                    # One bad store must never kill a 92-store sweep.
                    print(f"    !! store failed: {type(e).__name__}: {e}")
                    failed_stores.append((st["store_id"], str(e)[:120]))

                if i < len(todo):
                    await asyncio.sleep(random.uniform(*config.DELAY_RANGE))
        finally:
            await brwsr.close()

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

    summary = db.price_summary(conn, run_id)
    if summary:
        print("\nPrice spread across stores:")
        print(f"  {'SKU':>8}  {'stores':>6} {'carried':>7} {'stock':>5}"
              f"  {'market':>15}  {'member':>15}  title")
        for sku, title, nst, carried, instock, mn, mx, mmn, mmx in summary:
            rng = f"${mn:.2f}-${mx:.2f}" if mn is not None else "-"
            mrng = f"${mmn:.2f}-${mmx:.2f}" if mmn is not None else "-"
            print(f"  {sku or '-':>8}  {nst:>6} {carried or 0:>7} {instock or 0:>5}"
                  f"  {rng:>15}  {mrng:>15}  {title[:32]}")

    conn.close()
    return 0 if not failed_stores else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run with --resume <run_id> to continue.")
        sys.exit(130)
