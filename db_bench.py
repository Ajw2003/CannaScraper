"""Time the read queries that matter, so a schema change can be judged.

Normalizing `observations` into obs + products + store_meta puts two joins
behind every read. That should be cheap -- both lookup tables are tiny and
primary-keyed, and the narrower row means less I/O per scan -- but "should be"
is not a measurement. Run this before a schema change and after it, and compare.

Every query here is one a user actually waits on: the search page, a product
lookup, the index freshness banner.

    python db_bench.py                        # time the current schema
    python db_bench.py --db history.db.pre-normalize --label before
    python db_bench.py --repeat 5

Read-only. Safe to run against a live database.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

import config
import db
import stores as S


def _time(fn, repeat: int) -> tuple[float, float, object]:
    """Best-of-N wall time in ms, plus the last result for a sanity check.

    Best-of rather than mean: we are comparing schemas, and the fastest run is
    the one least polluted by whatever else the machine was doing.
    """
    times, out = [], None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t0) * 1000)
    return min(times), statistics.median(times), out


def _size(x) -> int:
    try:
        return len(x)
    except TypeError:
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--province", default=config.PROVINCE)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--label", default=None, help="tag for the saved json")
    ap.add_argument("--save", default=None, help="write results to this json")
    args = ap.parse_args(argv)

    print("=" * 74)
    print(f"db read benchmark — {args.db}")
    print("=" * 74)

    conn = db.connect(args.db)
    try:
        # --- STEP 1 --------------------------------------------------------
        print("\nSTEP 1  Gather realistic query inputs")
        store_list = S.get_stores(province=args.province)
        ids = [s["store_id"] for s in store_list]
        run = db.latest_run_id(conn)
        skus = [r[0] for r in conn.execute(
            "SELECT DISTINCT sku FROM observations LIMIT 25")]
        near = ids[:10]                       # a "10 nearest stores" lookup
        if not (ids and run and skus):
            print("  FAIL  database has no usable rows to benchmark.")
            return 2
        print(f"  PASS  {len(ids)} stores, run {run}, {len(skus)} sample SKUs")

        # --- STEP 2 --------------------------------------------------------
        print("\nSTEP 2  Time each query")
        cases = [
            ("province_facts (search page)",
             lambda: db.province_facts(conn, ids)),
            ("available_skus (search filter)",
             lambda: db.available_skus(conn, ids)),
            ("latest_observations (1 sku, 10 stores)",
             lambda: db.latest_observations(conn, [skus[0]], near)),
            ("latest_observations (25 skus, all stores)",
             lambda: db.latest_observations(conn, skus, ids)),
            ("latest_observations (whole store)",
             lambda: db.latest_observations(conn, store_ids=[ids[0]])),
            ("index_coverage (freshness banner)",
             lambda: db.index_coverage(conn, ids)),
            ("indexed_store_ids",
             lambda: db.indexed_store_ids(conn, ids)),
            ("index_runs",
             lambda: db.index_runs(conn, args.province)),
            ("in_stock (one run)",
             lambda: db.in_stock(conn, run)),
            ("price_summary (one run)",
             lambda: db.price_summary(conn, run)),
            ("last_scan_attempt",
             lambda: db.last_scan_attempt(conn, ids)),
            ("scan_failure_streaks",
             lambda: db.scan_failure_streaks(conn)),
        ]

        print(f"\n        {'query':<42} {'best ms':>9} {'median':>9}  rows")
        results = {}
        for name, fn in cases:
            try:
                best, med, out = _time(fn, args.repeat)
            except Exception as e:                            # noqa: BLE001
                print(f"        {name:<42} {'ERROR':>9}  {type(e).__name__}: {e}")
                results[name] = None
                continue
            results[name] = {"best_ms": round(best, 2),
                             "median_ms": round(med, 2), "rows": _size(out)}
            print(f"        {name:<42} {best:>9.1f} {med:>9.1f}  {_size(out):>6,}")

        total = sum(r["best_ms"] for r in results.values() if r)
        print(f"\n        {'TOTAL':<42} {total:>9.1f}")

        # --- STEP 3 --------------------------------------------------------
        print("\nSTEP 3  Record the shape of the data behind those numbers")
        n_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        import os
        try:
            fsize = os.path.getsize(args.db)
        except OSError:
            fsize = 0
        print(f"  PASS  {n_obs:,} observation rows, file {fsize / 1048576:,.1f} MB")

        if args.save:
            with open(args.save, "w", encoding="utf-8") as fh:
                json.dump({"label": args.label or args.db, "db": args.db,
                           "rows": n_obs, "file_mb": round(fsize / 1048576, 1),
                           "total_ms": round(total, 2), "queries": results},
                          fh, indent=2)
            print(f"        saved to {args.save}")
    finally:
        conn.close()

    print("\n" + "=" * 74)
    print("Compare a before/after pair by running this twice with --save and")
    print("diffing the two json files, or just read the TOTAL line.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
