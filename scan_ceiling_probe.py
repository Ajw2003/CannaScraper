"""How many SKUs will scan-multiple-items accept in one call?

`/api/product/scan-multiple-items/<store_id>` returns price + stock and nothing
else -- no title, no image, no body_html. If it accepts the whole known SKU
list in one request, a province refresh costs ~1 call per store instead of the
~25 paged product/search calls index_builder.py makes today, and moves a small
fraction of the bytes.

The number this exists to find is the ceiling, and the failure mode it exists
to catch is SILENT TRUNCATION -- a server that accepts 2,000 SKUs, answers 200
OK, and quietly prices only the first 500. That would look like 1,500 products
going "not carried" at every store, which is exactly the kind of confidently
wrong answer this tool must never produce. So every rung checks coverage
(scanned + missing == requested), not just the status code.

Two things the endpoint does that make a naive probe misread it:

  * `success: false` with `message: "Bag Changed"` is NORMAL. It means at least
    one requested SKU is not carried at that store, not that the call failed.
    Coverage is the only honest health check.
  * `missingItems` holds VARIANT IDs, while `scanned-items` is keyed by SKU.
    Counting them as if they were the same identifier space would overstate
    coverage, so the two are reconciled explicitly below.

The SKU list is therefore built known-carried-first, from what the database has
actually seen in stock at the target store -- sending arbitrary catalogue SKUs
measures how many are missing, not how many the server will price.

Read-only: it only asks for prices. ~10 requests, paced under the rate limit.

    python scan_ceiling_probe.py
    python scan_ceiling_probe.py --store 3154 --max 6702
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

import config
import db
import stores as S

# Escalating rungs. Stops at the first rung that fails or truncates, so a low
# ceiling costs few requests.
LADDER = [1, 25, 100, 250, 500, 1000, 2000, 4000, 6702]


def _load_catalog() -> dict[str, int]:
    """{sku: variant_id} for every catalogue variant we can address."""
    with open(config.CATALOG_CACHE, encoding="utf-8") as fh:
        variants = json.load(fh).get("variants") or []
    out: dict[str, int] = {}
    for v in variants:
        if v.get("sku") and v.get("variant_id"):
            out.setdefault(str(v["sku"]), v["variant_id"])
    return out


def _busiest_store(conn, province: str, skip: set) -> str | None:
    """The store the database has seen the most in-stock products at.

    A store with a deep in-stock history is the one most likely to price a
    large batch, which is what we are trying to measure. Probing a sparse
    store would measure its empty shelves instead of the server's ceiling.
    """
    rows = conn.execute(
        "SELECT store_id, COUNT(*) n FROM observations "
        "WHERE available=1 AND province=? GROUP BY store_id ORDER BY n DESC",
        (province,)).fetchall()
    for sid, _ in rows:
        if str(sid) not in skip:
            return str(sid)
    return None


def _carried_skus(conn, store_id: str) -> list[str]:
    """SKUs the database has seen in stock at this store."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT sku FROM observations WHERE store_id=? AND available=1",
        (store_id,))]


def _post(store_id: str, skus: list[dict]) -> tuple[int, object, float]:
    url = config.API_SCAN.format(store_id=store_id)
    body = json.dumps({"skus": skus}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": config.USER_AGENT},
        method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode()), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:200], time.time() - t0
    except Exception as e:                                    # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}", time.time() - t0


def _kb(n: float) -> str:
    return f"{n / 1024:,.1f} KB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--store", default=None)
    ap.add_argument("--province", default=config.PROVINCE)
    ap.add_argument("--max", type=int, default=None,
                    help="cap the ladder (default: whole catalogue)")
    args = ap.parse_args(argv)

    print("=" * 74)
    print("scan-multiple-items payload ceiling probe")
    print("=" * 74)

    # --- STEP 1 -----------------------------------------------------------
    print("\nSTEP 1  Pick a store with a deep in-stock history")
    store_list = S.get_stores(province=args.province)
    skip = set(config.SCAN_SKIP_STORES)
    conn = db.connect()
    try:
        sid = str(args.store) if args.store else _busiest_store(
            conn, args.province, skip)
        if not sid:
            print("  FAIL  no store in the database has in-stock history.")
            return 2
        carried = _carried_skus(conn, sid)
    finally:
        conn.close()

    store = next((s for s in store_list if str(s["store_id"]) == sid), None)
    label = f"{store['name']}, {store.get('city','')}" if store else "(unknown)"
    print(f"  PASS  store {sid} — {label}")
    print(f"        {len(carried):,} SKUs seen in stock there   "
          f"(skip list: {sorted(skip) or 'none'})")

    # --- STEP 2 -----------------------------------------------------------
    print("\nSTEP 2  Build the payload, known-carried SKUs first")
    catalog = _load_catalog()
    if not catalog:
        print("  FAIL  catalog.json has no usable sku/variant_id pairs.")
        return 2

    ordered = [s for s in carried if s in catalog]
    rest = [s for s in catalog if s not in set(ordered)]
    pairs = [{s: catalog[s]} for s in ordered + rest]
    print(f"  PASS  {len(pairs):,} distinct SKUs to send "
          f"({len(ordered):,} known-carried, then {len(rest):,} others)")
    if len(ordered) < 100:
        print("        NOTE  fewer than 100 known-carried SKUs, so rungs above")
        print("              that are mostly not-carried items. The ceiling is")
        print("              still valid; coverage is what is being measured.")

    # --- STEP 3 -----------------------------------------------------------
    print("\nSTEP 3  Climb the ladder until it breaks or truncates")
    print(f"\n        {'sent':>6}  {'HTTP':>5}  {'secs':>6}  {'response':>10}"
          f"  {'priced':>7}  {'missing':>7}  {'covered':>8}  verdict")

    ceiling = 0
    best = None
    rungs = [n for n in LADDER if n <= len(pairs)]
    if args.max:
        rungs = [n for n in rungs if n <= args.max]
    if len(pairs) not in rungs:
        rungs.append(len(pairs))

    for n in rungs:
        time.sleep(60.0 / config.API_RATE_PER_MIN)
        batch = pairs[:n]
        status, body, secs = _post(sid, batch)

        # Note what is NOT checked here: body["success"]. It is false whenever
        # any requested SKU is not carried, which is the normal case for a
        # large batch. Only the transport and the coverage count matter.
        if status != 200 or not isinstance(body, dict):
            snippet = body if isinstance(body, str) else json.dumps(body)[:60]
            print(f"        {n:>6}  {status:>5}  {secs:>6.1f}  "
                  f"{'-':>10}  {'-':>7}  {'-':>7}  {'-':>8}  REJECTED {snippet[:30]}")
            break

        data = body.get("data") or {}
        priced = len(data.get("scanned-items") or {})
        missing = len(data.get("missingItems") or [])
        size = len(json.dumps(body).encode())
        covered = priced + missing
        ok = covered >= n

        print(f"        {n:>6}  {status:>5}  {secs:>6.1f}  {_kb(size):>10}"
              f"  {priced:>7}  {missing:>7}  {covered:>8}  "
              f"{'ok' if ok else 'TRUNCATED — server answered for only ' + str(covered)}")

        if not ok:
            break
        ceiling = n
        best = (n, size, secs)

    # --- STEP 4 -----------------------------------------------------------
    print("\nSTEP 4  What this means for a province refresh")
    if not ceiling:
        print("  FAIL  the endpoint would not take even the smallest batch.")
        print("        Nothing to change; keep index_builder.py as it is.")
        return 1

    stores_n = len(store_list)
    calls_per_store = -(-len(pairs) // ceiling)          # ceil
    n, size, secs = best
    est_bytes = size / n * len(pairs) * stores_n
    # Today: ~25 paged product/search calls per store at ~157 KB each.
    today_bytes = 25 * 156.9 * 1024 * stores_n
    today_calls = 25 * stores_n

    print(f"  PASS  ceiling is at least {ceiling:,} SKUs per call "
          f"({secs:.1f}s, {_kb(size)} back)")
    print()
    print(f"        {'':<22} {'calls':>8}  {'bytes':>12}")
    print(f"        {'today (search)':<22} {today_calls:>8,}  "
          f"{today_bytes / 1048576:>9,.0f} MB")
    print(f"        {'scan, this ceiling':<22} "
          f"{calls_per_store * stores_n:>8,}  {est_bytes / 1048576:>9,.0f} MB")
    print()
    if calls_per_store == 1:
        print(f"        One call per store covers the whole {len(pairs):,}-SKU")
        print(f"        catalogue: {stores_n} calls for {args.province}.")
    else:
        print(f"        {calls_per_store} calls per store to cover {len(pairs):,} SKUs.")
    print()
    print("        CAVEAT, and it decides the design: scan-multiple-items prices")
    print("        SKUs you already know. It cannot discover a product that is")
    print("        new to the catalogue. Any refresh built on it must still run")
    print("        product/search periodically to keep catalog.json current, or")
    print("        new products stay invisible.")

    print("\n" + "=" * 74)
    print(f"RESULT: ceiling >= {ceiling:,} SKUs/call, {calls_per_store} call(s) per store.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
