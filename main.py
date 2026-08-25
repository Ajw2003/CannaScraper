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
    p.add_argument("--compare", action="store_true",
                   help="run BOTH backends over the same stores and diff them; "
                        "writes nothing to the database")
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

    if args.compare:
        return await compare_backends(store_list, targets)

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
        from_index = any(str(r.get("run_id", "")).startswith("index-")
                         for r in cached)
        src = "STOCK INDEX" if from_index else "CACHE"
        print(f"\n[source: {src} — {age:.1f}h old]")
        cached = fill_missing_stores(cached, store_list, targets, conn)
        show_results(cached, age, args, targets, location_label,
                     live=False, conn=conn, checked=len(store_list))
        if not args.cached:
            print(f"\n(Reused data {age:.1f}h old. Use --refresh to check now.)")
        return 0

    if args.cached:
        # The index stores in-stock items only, so "no rows" can mean either
        # "never looked" or "looked, and it isn't in stock". Say which.
        covered, idx_age = db.index_coverage(conn, store_ids)
        if covered:
            # "Nowhere near you has this" is a real answer, so show it the same
            # way as any other -- listing every store checked, and opening the
            # report. Returning early here left the page unwritten.
            print(f"\n[source: STOCK INDEX — {idx_age:.1f}h old]")
            rows = fill_missing_stores([], store_list, targets, conn)
            show_results(rows, idx_age, args, targets, location_label,
                         live=False, conn=conn, checked=len(store_list))
            return 0
        print("\nNo index or cached data covers those stores yet.")
        print("Run:  python index_builder.py     (builds the province index)")
        print("or drop --cached to check them live now.")
        return 1

    skip: set[str] = set()
    if args.resume:
        skip = db.done_store_ids(conn, run_id)
        print(f"\nResuming: {len(skip)} store(s) already complete, skipping them.")

    todo = [s for s in store_list if s["store_id"] not in skip]

    # Stores the scan endpoint will not serve. Skipped here rather than failed
    # slowly: store 528 alone costs 90s of retry backoff per run.
    if (args.fetcher or config.FETCHER) == "api":
        todo, unscannable = fetchers.partition_scannable(
            todo, db.last_scan_attempt(conn, [s["store_id"] for s in todo]))
        for s in unscannable:
            print(f"Skipping   {s['name']} ({s['store_id']}) — known bad on "
                  f"the scan endpoint; retried every "
                  f"{config.SCAN_SKIP_RETRY_DAYS} days")

    print(f"\nProvince : {args.province}")
    print(f"Stores   : {len(todo)} to scrape ({len(store_list)} in scope)")
    print(f"Products : {len(targets)} variant(s)")
    print(f"Requests : ~{len(todo) * len(targets)} product page loads\n")

    ok_rows = err_rows = 0
    failed_stores: list[tuple[str, str]] = []

    fetcher = fetchers.get_fetcher(args.fetcher)
    if args.headed and hasattr(fetcher, "_headless"):
        fetcher._headless = False
    print(f"[source: LIVE via {fetcher.name.upper()}"
          f"{' (real browser)' if fetcher.name == 'browser' else ''}]\n")

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

            # Backends that pace themselves (the rate-limited API) must not get
            # an extra delay stacked on top -- that alone would triple a sweep.
            if i < len(todo) and not getattr(f, "paces_itself", False):
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
    fresh = fill_missing_stores(fresh, store_list, targets, conn)
    show_results(fresh, 0.0, args, targets, location_label, live=True, conn=conn,
                 checked=len(store_list))

    conn.close()
    return 0 if not failed_stores else 1


def fill_missing_stores(rows: list[dict], store_list: list[dict],
                        targets: list[dict], conn) -> list[dict]:
    """Add a row for every store we checked that returned nothing.

    The stock index holds in-stock items only, so a store with no row simply
    isn't stocking the product. Dropping it makes "1 of 1 checked" out of a
    six-store search, which reads as though we barely looked. Every store we
    checked should appear, with an explicit answer.
    """
    indexed = db.indexed_store_ids(conn, [s["store_id"] for s in store_list])
    have = {(r["store_id"], str(r.get("sku"))) for r in rows}
    out = list(rows)

    for v in targets:
        sku = str(v.get("sku") or "")
        if not sku:
            continue
        for s in store_list:
            if (s["store_id"], sku) in have:
                continue
            known = s["store_id"] in indexed
            out.append({
                "scraped_at": None,
                "store_id": s["store_id"],
                "store_name": s.get("name", ""),
                "city": s.get("city", ""),
                "province": s.get("province", ""),
                "distance_km": s.get("distance_km"),
                "sku": sku,
                "handle": v.get("handle", ""),
                "title": v.get("title", ""),
                "brand": v.get("brand", ""),
                "category": v.get("category", ""),
                "size": v.get("size", ""),
                "price": None, "member_price": None,
                "api_stock": 0 if known else None,
                "api_elite_price": None, "api_member_price": None,
                "is_elite": None,
                "available": 0,
                "carried": 0 if known else None,
                "stock_text": "Not in stock" if known else "Not checked",
                "status": "ok",
            })
    return out


def tier_price(row: dict) -> tuple[str, float | None]:
    """Which discount tier applies, and what it costs.

    A product is either ELITE-tier or Member-tier, never both: the site shows
    the member price only when is_elite is false, and the ELITE price only when
    it is true. So there is no elite-vs-member delta per product -- the useful
    comparison is tier price vs market price.
    """
    elite = row.get("api_elite_price")
    member = row.get("member_price") or row.get("api_member_price")
    if row.get("is_elite") and elite:
        return "ELITE", elite
    if member:
        return "member", member
    if elite:                      # is_elite unknown (older rows)
        return "ELITE", elite
    return "-", None


def tier_note(row: dict) -> str:
    """A one-line flag for ELITE-only products."""
    if row.get("is_elite"):
        return "   *** ELITE members only — no Cabana Club price ***"
    return ""


async def compare_backends(store_list, targets) -> int:
    """Run both backends over the same stores and diff the answers.

    The API returning another store's numbers, or stale stock, would look
    completely normal in the output -- this is the only thing that would catch
    it. Writes nothing to the database.
    """
    FIELDS = ["price", "member_price", "api_stock", "carried", "available"]

    print(f"\nComparing backends over {len(store_list)} store(s), "
          f"{len(targets)} product(s). Nothing will be saved.\n")

    results: dict[str, dict] = {}
    for backend in ("browser", "api"):
        print(f"--- {backend} ---")
        t0 = time.time()
        async with fetchers.get_fetcher(backend) as f:
            for i, st in enumerate(store_list, 1):
                try:
                    for r in await f.fetch(st, targets, verbose=False):
                        results.setdefault((r["store_id"], r["sku"]), {})[backend] = r
                except Exception as e:                      # noqa: BLE001
                    print(f"    {st['name']}: FAILED {type(e).__name__}: {e}")
                if i < len(store_list) and not getattr(f, "paces_itself", False):
                    await asyncio.sleep(random.uniform(*config.DELAY_RANGE))
        print(f"    {time.time() - t0:.1f}s\n")

    diffs = compared = 0
    skipped: list[str] = []

    for (sid, sku), got in sorted(results.items()):
        a, b = got.get("browser"), got.get("api")
        if not a or not b:
            skipped.append(f"{sid}/{sku}: only {'browser' if a else 'api'} returned")
            continue

        # A row that errored holds no data to compare -- counting it as a
        # disagreement would blame the wrong backend. Report separately.
        failed = [n for n, r in (("browser", a), ("api", b))
                  if r.get("status") != "ok"]
        if failed:
            skipped.append(f"{a['store_name']} ({sid}) sku {sku}: "
                           f"{'+'.join(failed)} errored "
                           f"({(a if 'browser' in failed else b).get('error','')[:48]})")
            continue

        compared += 1
        bad = [f for f in FIELDS if (a.get(f) or 0) != (b.get(f) or 0)]
        if bad:
            diffs += 1
            print(f"  {a['store_name']} ({sid}) sku {sku}:")
            for f in bad:
                print(f"      {f:<14} browser={a.get(f)!r:<12} api={b.get(f)!r}")

    if skipped:
        print(f"\n  {len(skipped)} row(s) not comparable (a backend failed):")
        for s in skipped:
            print(f"      {s}")

    print("\n" + "=" * 74)
    if diffs:
        print(f"{diffs} disagreement(s) across {compared} comparable row(s). "
              f"Do NOT switch the default until these are understood.")
    else:
        print(f"No disagreements across {compared} comparable row(s). "
              f"The API backend matches the browser exactly.")
    return 1 if diffs else 0


def show_results(rows: list[dict], age: float | None, args, targets,
                 location_label: str = "", live: bool = True, conn=None,
                 checked: int | None = None) -> None:
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
        # Count stores we looked at, not rows we got back -- the index omits
        # stores where an item is out of stock, so len(rows) understates it.
        n_checked = checked if checked is not None else len(rows)
        print(f"IN STOCK at {len(have)} of {n_checked} store(s) checked:\n")
        hdr = f"  {'units':>5}  {'store':<26} {'city':<16}"
        if show_dist:
            hdr += f" {'km':>6}"
        print(hdr + f" {'market':>8} {'tier':>6} {'you pay':>8} {'save':>13}")
        last = None
        for r in have:
            if r.get("sku") != last:
                print(f"\n  {r.get('title')} ({r.get('size')})  [SKU {r.get('sku')}]"
                      + tier_note(r))
                last = r.get("sku")
            d = (f" {r['distance_km']:>6.1f}"
                 if show_dist and r.get("distance_km") is not None
                 else (" " * 7 if show_dist else ""))
            market = r.get("price")
            label, deal = tier_price(r)
            saving = ""
            if market and deal and deal < market:
                saving = f"-${market - deal:.2f} ({100*(market-deal)/market:.0f}%)"
            print(f"  {r.get('api_stock') if r.get('api_stock') is not None else '?':>5}"
                  f"  {str(r.get('store_name'))[:26]:<26} {str(r.get('city'))[:16]:<16}"
                  f"{d} {('$%.2f' % market) if market else '-':>8}"
                  f" {label:>6} {('$%.2f' % deal) if deal else '-':>8} {saving:>13}")
    else:
        n_checked = checked if checked is not None else len(rows)
        print(f"NOT IN STOCK at any of the {n_checked} store(s) checked."
              + ("" if n_checked > 5 else "  Try --top 25 to widen the search."))

    if not args.no_report:
        q = " ".join(args.product) if args.product else "watchlist"
        path = report.write_and_open(rows, query=q, age_hours=age,
                                     location=location_label,
                                     checked=checked)
        print(f"\nReport: {path}")


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run with --resume <run_id> to continue.")
        sys.exit(130)
