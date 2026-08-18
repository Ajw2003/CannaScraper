"""Build a province-wide stock index.

`/api/product/search?title=<term>&storeId=<id>` returns, for one store, every
product it currently has IN STOCK -- 50 per page, with retail/member/elite
price, exact quantity, gram equivalence, and THC/CBD levels. One store is
~1,000-1,250 products, so ~25 paged calls.

That makes a whole province practical: ~2,300 calls, well under an hour, versus
~30 hours for the per-SKU scan endpoint.

Verified before building (see notes in README):
  * Exhaustive -- 11 different search terms surfaced nothing beyond `title='a'`,
    and 148 catalogue variants absent from the result were independently
    confirmed not-in-stock via scan-multiple-items.
  * In-stock ONLY -- carried-but-sold-out products are simply absent. That is
    why _close_out() exists; read its comment before changing anything here.

Usage:
    python index_builder.py                     # Alberta
    python index_builder.py --province Ontario
    python index_builder.py --limit 5           # try a few stores first
    python index_builder.py --resume index-Alberta-20260817
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config
import db
import stores as S

SEARCH = config.API_BASE + "/product/search"

# Any common term returns the store's whole in-stock set; the parameter is
# required but does not meaningfully filter. 'a' is simply a safe default.
ENUM_TERM = "a"
PAGE_SIZE = 50          # server rejects anything larger

# Not physical inventory. They appear at all 92 stores with six-figure "stock",
# which wrecks any "most widely available" or total-units aggregate.
SKIP_TITLE_PATTERNS = ("membership", "renewal", "gift card")


class _Pacer:
    """Keep us under the advertised 60/min."""

    def __init__(self, per_min: int):
        self._interval = 60.0 / max(1, per_min)
        self._last = 0.0

    def wait(self) -> None:
        gap = time.time() - self._last
        if gap < self._interval:
            time.sleep(self._interval - gap)
        self._last = time.time()


def _get(pacer: _Pacer, term: str, store_id: str, page: int,
         province: str) -> dict | None:
    pacer.wait()
    q = urllib.parse.urlencode({"title": term, "storeId": store_id,
                                "limit": PAGE_SIZE, "page": page,
                                "province": province})
    req = urllib.request.Request(
        f"{SEARCH}?{q}",
        headers={"User-Agent": config.USER_AGENT,
                 "Content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=config.API_TIMEOUT_S * 3) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(20)
            return _get(pacer, term, store_id, page, province)
        return None
    except Exception:
        return None
    # A blank/odd term makes the server serve its login page instead of JSON.
    if body.lstrip().startswith("<"):
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _row(store: dict, prod: dict, var: dict, now: str) -> dict:
    p = var.get("pricing") or {}
    # The search response already carries CDN urls -- no extra request.
    imgs = prod.get("images") or []
    image = imgs[0] if imgs else ""
    qty = p.get("qty_available")
    # A handful of accessories carry stock but no price in their system.
    # Recording that as "$0.00" would read as free; None means "unknown".
    retail = p.get("retail_price") or None
    member = p.get("member_price") or None
    sid = str(store["store_id"])

    return {
        "scraped_at": now,
        "store_id": sid,
        "store_name": store.get("name", ""),
        "city": store.get("city", ""),
        "province": store.get("province", ""),
        "sku": str(var.get("sku") or ""),
        "handle": prod.get("handle", ""),
        "title": prod.get("title", ""),
        "brand": prod.get("vendor", ""),
        "category": prod.get("productType", ""),
        "size": var.get("title", ""),
        "url": f"{config.BASE}/products/{prod.get('handle','')}?sID={sid}",
        "price": retail,
        "member_price": member,
        "default_price": None,
        "available": 1 if (qty or 0) > 0 else 0,
        "carried": 1,
        "stock_text": "In Stock" if (qty or 0) > 0 else "Sold Out",
        # THC/CBD straight from the source beats our regex over body_html.
        "thc": p.get("thc_level") or "",
        "cbd": p.get("cbd_level") or "",
        "store_label": "",
        "api_store_id": sid,
        "store_id_match": 1,
        "api_stock": qty,
        "api_member_price": member,
        "api_price": retail,
        "api_elite_price": p.get("elite_price") or None,
        # Tiers are mutually exclusive: is_elite products carry an ELITE
        # price and no member price, and vice versa.
        "is_elite": 1 if p.get("is_elite") else 0,
        "image": image,
        "api_equiv_g": p.get("equivalent_g"),
        # p["storeId"] is their internal id (e.g. 622 for our 3412); keep it
        # for cross-referencing without confusing it with our registry id.
        "api_raw": f"internal_store={p.get('storeId')};sale={p.get('sale_price')}",
        "status": "ok",
        "error": "",
    }


def index_store(pacer: _Pacer, store: dict, province: str) -> list[dict]:
    """Every in-stock product at one store."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: dict[str, dict] = {}
    page = 1
    while True:
        d = _get(pacer, ENUM_TERM, str(store["store_id"]), page, province)
        if not d:
            break
        prods = (d.get("products") or {})
        for prod in prods.get("data") or []:
            title = (prod.get("title") or "").lower()
            if any(pat in title for pat in SKIP_TITLE_PATTERNS):
                continue
            for var in prod.get("variants") or []:
                r = _row(store, prod, var, now)
                if r["sku"]:
                    rows[r["sku"]] = r
        if not (prods.get("pagination") or {}).get("hasNextPage"):
            break
        page += 1
    return list(rows.values())


def _close_out(conn, store_id: str, seen_skus: set, now: str,
               store: dict) -> list[dict]:
    """Zero-out products that were in stock last time but are absent now.

    The search endpoint returns in-stock items ONLY, so a sold-out product just
    disappears. Without this, `db.latest_observations()` would keep serving
    yesterday's in-stock row as the freshest fact and confidently send someone
    to a store that has none -- the exact failure this tool exists to prevent.
    """
    prev = db.latest_observations(conn, store_ids=[store_id])
    out = []
    for r in prev:
        if r["sku"] in seen_skus or not r.get("available"):
            continue
        out.append({**r, "scraped_at": now, "api_stock": 0, "available": 0,
                    "stock_text": "Sold Out", "status": "ok", "error": ""})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a province-wide stock index")
    ap.add_argument("--province", default=config.PROVINCE)
    ap.add_argument("--limit", type=int, default=None,
                    help="only index the first N stores (try 5 first)")
    ap.add_argument("--resume", metavar="RUN_ID", default=None)
    ap.add_argument("--db", default=config.DB_PATH)
    args = ap.parse_args(argv)

    run_id = args.resume or (
        f"index-{args.province.replace(' ', '')}-"
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")

    store_list = S.get_stores(province=args.province, limit=args.limit)
    if not store_list:
        print(f"No stores for province={args.province!r}")
        return 2

    conn = db.connect(args.db)
    done = db.done_store_ids(conn, run_id) if args.resume else set()
    todo = [s for s in store_list if s["store_id"] not in done]

    print("=" * 74)
    print(f"Stock index — {args.province}   run {run_id}")
    print("=" * 74)
    if done:
        print(f"Resuming: {len(done)} store(s) already indexed.")
    print(f"Stores: {len(todo)} to index"
          f"   (~{len(todo) * 25 * 60 / config.API_RATE_PER_MIN / 60:.0f} min)\n")

    pacer = _Pacer(config.API_RATE_PER_MIN)
    t0 = time.time()
    total = failed = 0

    for i, st in enumerate(todo, 1):
        try:
            rows = index_store(pacer, st, args.province)
            closed = _close_out(conn, st["store_id"],
                                {r["sku"] for r in rows},
                                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                st)
            db.write_rows(conn, run_id, rows + closed)
            total += len(rows)
            instock = sum(1 for r in rows if r["available"])
            done_n, left = i, len(todo) - i
            eta = (time.time() - t0) / done_n * left / 60
            print(f"[{i}/{len(todo)}] {st['name'][:26]:<26} {st['city'][:14]:<14} "
                  f"{len(rows):>5} products, {instock:>5} in stock"
                  f"{f', {len(closed)} closed out' if closed else ''}"
                  f"   ETA {eta:.0f}m")
        except Exception as e:                                # noqa: BLE001
            failed += 1
            print(f"[{i}/{len(todo)}] {st['name'][:26]:<26} FAILED "
                  f"{type(e).__name__}: {str(e)[:60]}")

    mins = (time.time() - t0) / 60
    print("\n" + "=" * 74)
    print(f"Indexed {total} store-product rows in {mins:.1f} min"
          f"   ({failed} store(s) failed)")
    print(f"DB: {args.db}   run_id: {run_id}")
    if failed:
        print(f"Retry just those:  python index_builder.py --resume {run_id}")
    conn.close()
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted — re-run with --resume <run_id> to continue.")
        sys.exit(130)
