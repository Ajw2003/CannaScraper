"""Check that the scan backend reads a "Bag Changed" response correctly.

Two coupled bugs lived here, and both are invisible unless you look at a batch
where nothing is carried:

  1. `_call()` keyed success on `body["success"]`. The server sets that to
     false, with message "Bag Changed", whenever ANY requested SKU is not
     stocked at that store. A batch where none are stocked -- a perfectly
     ordinary answer -- was treated as a hard failure, burned all three
     retries, and produced error rows instead of "not carried" rows.

  2. `missingItems` holds VARIANT IDS while `scanned-items` is keyed by SKU.
     The not-carried branch compared a SKU against that set, so the test could
     never pass and every legitimately uncarried product was ALSO tagged with
     the error "sku absent from response".

The second bug hid the first: both produced noise on uncarried products, so
neither looked like the other's cause.

Three cases, against the live endpoint:

  A  every SKU carried        -- must price them all, no errors
  B  no SKU carried           -- must return "Not carried", no errors  <-- bug 1
  C  mixed                    -- must do both in one response          <-- bug 2

Read-only: it asks for prices and writes nothing. ~3 requests.

    python verify_scan_fix.py
    python verify_scan_fix.py --store 3154
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys

import config
import db
from fetchers.api_fetcher import ApiFetcher

N = 3          # SKUs per case; small, since one call is one case


def _catalog() -> dict[str, dict]:
    with open(config.CATALOG_CACHE, encoding="utf-8") as fh:
        variants = json.load(fh).get("variants") or []
    return {str(v["sku"]): v for v in variants
            if v.get("sku") and v.get("variant_id")}


def _pick(conn: sqlite3.Connection, store_id: str, cat: dict) -> tuple[list, list]:
    """(carried, uncarried) catalogue variants for this store, from history."""
    carried_skus = [r[0] for r in conn.execute(
        "SELECT DISTINCT sku FROM observations "
        "WHERE store_id=? AND available=1", (store_id,))]
    carried = [cat[s] for s in carried_skus if s in cat][:N]

    seen = set(carried_skus)
    uncarried = [v for s, v in cat.items() if s not in seen][:N]
    return carried, uncarried


def _check(name: str, rows: list[dict], expect_carried: bool) -> bool:
    """Every row must be status ok, error-free, and on the expected side."""
    bad = []
    for r in rows:
        if r["status"] != "ok":
            bad.append(f"{r['sku']}: status={r['status']} {r['error'][:60]}")
        elif r["error"]:
            bad.append(f"{r['sku']}: error={r['error'][:60]}")
        elif expect_carried and not r["carried"]:
            bad.append(f"{r['sku']}: expected carried, got '{r['stock_text']}'")
        elif not expect_carried and r["carried"]:
            bad.append(f"{r['sku']}: expected not-carried, got carried")

    if bad:
        print(f"  FAIL  {name}")
        for b in bad[:6]:
            print(f"          {b}")
        return False
    print(f"  PASS  {name} — {len(rows)} row(s), no errors")
    return True


async def run(store: dict, carried: list, uncarried: list) -> bool:
    ok = True
    async with ApiFetcher() as f:
        print("\nCASE A  every SKU carried at this store")
        if carried:
            ok &= _check("all carried", await f.fetch(store, carried, verbose=False), True)
        else:
            print("  SKIP  no known-carried SKUs for this store")

        print("\nCASE B  no SKU carried  (this is the one that used to raise)")
        if uncarried:
            rows = await f.fetch(store, uncarried, verbose=False)
            ok &= _check("none carried", rows, False)
            if rows and all(r["stock_text"] == "Not carried" for r in rows):
                print("          all reported 'Not carried', which is the "
                      "correct answer, not an error")
        else:
            print("  SKIP  every catalogue SKU is stocked here")

        print("\nCASE C  mixed batch")
        if carried and uncarried:
            rows = await f.fetch(store, carried + uncarried, verbose=False)
            by_sku = {r["sku"]: r for r in rows}
            ok &= _check("mixed / carried half",
                         [by_sku[str(v["sku"])] for v in carried], True)
            ok &= _check("mixed / uncarried half",
                         [by_sku[str(v["sku"])] for v in uncarried], False)
        else:
            print("  SKIP  need both kinds for this case")
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--store", default=None)
    ap.add_argument("--province", default=config.PROVINCE)
    args = ap.parse_args(argv)

    print("=" * 74)
    print("scan backend — 'Bag Changed' handling")
    print("=" * 74)

    import stores as S
    cat = _catalog()
    conn = db.connect()
    try:
        sid = str(args.store) if args.store else None
        if not sid:
            skip = set(config.SCAN_SKIP_STORES)
            for r, _n in conn.execute(
                    "SELECT store_id, COUNT(*) n FROM observations "
                    "WHERE available=1 AND province=? GROUP BY store_id "
                    "ORDER BY n DESC", (args.province,)):
                if str(r) not in skip:
                    sid = str(r)
                    break
        if not sid:
            print("\n  FAIL  no usable store found in the database.")
            return 2
        carried, uncarried = _pick(conn, sid, cat)
    finally:
        conn.close()

    store = next((s for s in S.get_stores(province=args.province)
                  if str(s["store_id"]) == sid), {"store_id": sid, "name": sid})
    print(f"\n  store {sid} — {store.get('name','')}, {store.get('city','')}")
    print(f"  {len(carried)} known-carried, {len(uncarried)} known-uncarried "
          f"SKUs selected")

    ok = asyncio.run(run(store, carried, uncarried))

    print("\n" + "=" * 74)
    print("RESULT: " + ("all cases pass — 'Bag Changed' is read as data, "
                        "not as failure." if ok else
                        "FAILURES above; the fix is incomplete."))
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
