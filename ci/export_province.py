"""Export one province's latest stock to a single static JSON file.

Built for the scheduled scrape (.github/workflows/scrape-one.yml): after
a run finishes, this reads the history DB it wrote to and produces
`data/<slug>.json` -- the file the static site (site/index.html, via
site/static-api.js) fetches at runtime. It never talks to the network
itself; everything comes from the history DB and the committed stores.json
(via stores.get_stores), same as the CLI and the web server.

Sold-out rows are kept (not just in-stock ones): the static site's port of
db.province_facts (static-api.js) needs the newest row per (sku, store)
INCLUDING sold-out rows to compute "in stock anywhere", category and
THC/CBD spans the same way server.py does, and the product page needs them
to tell "not in stock" apart from "not checked". Each stock row therefore
carries `available` (0/1) and `stock_text` appended after the original 7
fields, so an older export (from before this changed) still has its first 7
fields at the same positions.

    python ci/export_province.py --province Saskatchewan --db PATH --out DIR \
        [--run-summary run.json]

Runs anywhere with Python 3.11 and no third-party packages.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402  (needs the repo root on sys.path first)
import stores as S  # noqa: E402
import workqueue  # noqa: E402


def tier_price(row: dict) -> tuple[str, float | None]:
    """Which discount tier applies, and what it costs.

    Copied from main.py:326 (tier_price) rather than imported: main.py builds
    an argparse parser and runs asyncio scraping code at import time via its
    module-level imports of fetchers/report, which is unwanted weight (and
    risk) in an export step that only needs this one small, pure function.
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


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export one province's latest stock to JSON")
    p.add_argument("--province", required=True)
    p.add_argument("--db", required=True, help="path to the history sqlite DB")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--run-summary", default=None,
                    help="optional JSON file whose contents are embedded as `run`")
    return p.parse_args(argv)


def slugify(province: str) -> str:
    return province.strip().lower().replace(" ", "-")


def main(argv=None) -> int:
    args = parse_args(argv)

    # get_stores(province=None) falls back to config.PROVINCE, not "no
    # filter" -- pass "" (falsy but not None) to get every province.
    all_provinces = {s["province"] for s in S.get_stores(province="")}
    if args.province not in all_provinces:
        print(f"error: {args.province!r} is not a province in stores.json "
              f"(known: {sorted(all_provinces)})", file=sys.stderr)
        return 2

    store_list = S.get_stores(province=args.province)
    store_ids = [s["store_id"] for s in store_list]

    run_summary = None
    if args.run_summary:
        run_summary = json.loads(Path(args.run_summary).read_text(encoding="utf-8"))

    conn = db.connect(args.db)
    try:
        rows = db.latest_observations(conn, store_ids=store_ids)
        # The page names the stores that failed on the last run, so a reader
        # knows whose numbers are older. The count alone doesn't say which.
        if run_summary and run_summary.get("run_id"):
            workqueue.ensure(conn)
            run_summary["failed_stores"] = [
                f["name"] or f["store_id"]
                for f in workqueue.failures(conn, run_summary["run_id"])]
    finally:
        conn.close()

    if not rows:
        print(f"error: no rows for {args.province} in {args.db}", file=sys.stderr)
        return 1

    # Per-store newest scraped_at, from rows only (a store with zero rows
    # simply has no timestamp -- it was not reached, or nothing was in stock).
    newest_by_store: dict[str, str] = {}
    for r in rows:
        sid = str(r["store_id"])
        ts = r.get("scraped_at")
        if ts and (sid not in newest_by_store or ts > newest_by_store[sid]):
            newest_by_store[sid] = ts

    stores_out = [
        {
            "id": s["store_id"],
            "name": s["name"],
            "city": s["city"],
            "lat": s.get("latitude"),
            "lng": s.get("longitude"),
            "scraped_at": newest_by_store.get(str(s["store_id"])),
        }
        for s in store_list
    ]
    stores_with_data = sum(1 for s in stores_out if s["scraped_at"])

    products: dict[str, dict] = {}
    stock: dict[str, list] = {}
    # Each row's own scrape time, stored once in `times` and referenced by
    # index: a store's rows are not all written at the same instant, and the
    # page's "this product last checked" (db.cache_age_hours) reads the row's
    # time, not the store's. A shared table keeps the file small.
    times: list[str] = []
    time_index: dict[str, int] = {}
    for r in rows:
        sku = str(r["sku"])
        if sku not in products:
            products[sku] = {
                "title": r.get("title"),
                "brand": r.get("brand"),
                "category": r.get("category"),
                "size": r.get("size"),
                "image": r.get("image"),
                "handle": r.get("handle"),
            }
        tier_label, tier_amt = tier_price(r)
        price = r.get("api_price") or r.get("price")
        # Appended, not inserted: the first 7 fields keep their old positions
        # so a page that still expects the pre-sold-out-rows shape can treat
        # a missing `available` as available=1 (see static-api.js).
        stock.setdefault(sku, []).append([
            r["store_id"], r.get("api_stock"), price,
            tier_label, tier_amt, r.get("thc"), r.get("cbd"),
            1 if r.get("available") else 0, r.get("stock_text"),
            time_index.setdefault(r.get("scraped_at") or "", len(time_index)),
            r.get("carried"),
        ])
        if len(times) < len(time_index):
            times.append(r.get("scraped_at") or "")

    out = {
        "province": args.province,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run": run_summary,
        "stores": stores_out,
        "products": products,
        "stock": stock,
        "times": times,
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slugify(args.province)}.json"
    text = json.dumps(out, separators=(",", ":"), ensure_ascii=False)
    out_path.write_text(text, encoding="utf-8")

    n_stock_rows = sum(len(v) for v in stock.values())
    print(f"{args.province}: {stores_with_data}/{len(stores_out)} stores with data, "
          f"{len(products)} products, {n_stock_rows} stock rows, "
          f"{len(text.encode('utf-8'))} bytes -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
