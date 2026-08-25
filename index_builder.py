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
import ratelimit
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
            ratelimit.observe(r.headers)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            ratelimit.observe(e.headers, 429)
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


def _eta_min(t0: float, done: int, total: int) -> float | None:
    """Minutes left, from the rate achieved so far. None until one store lands."""
    if done <= 0:
        return None
    return (time.time() - t0) / done * (total - done) / 60


def build_index(province: str, *, limit: int | None = None,
                resume: str | None = None, db_path: str | None = None,
                on_progress=None, should_stop=None) -> dict:
    """Index every store in a province. The CLI and the web UI both call this.

    `on_progress(event)` receives a dict per milestone. `phase` is one of:
      begin   -- run_id and store count are known, nothing fetched yet
      store   -- about to fetch this store (it takes ~29s, so the UI needs
                 the name up front rather than after the fact)
      stored  -- this store landed; carries rows/instock/closed
      failed  -- this store raised; carries error

    On `rows` vs `instock`: the search endpoint returns in-stock products
    only, so every row this builds has available=1 and the two are equal.
    Reporting both as if they were separate facts is noise. They are kept
    separate here for one reason: if they ever diverge, the endpoint has
    stopped honouring that contract, and the CLI says so loudly instead of
    quietly indexing rows that are not actually in stock.

    `should_stop()` is polled once per store. A store's paged fetch is not
    interruptible mid-flight, so a cancel takes effect at the next store
    boundary -- up to ~29s. Say that in the UI rather than implying it is
    instant.

    Raises ValueError if the province matches no stores.
    """
    run_id = resume or (
        f"index-{province.replace(' ', '')}-"
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")

    store_list = S.get_stores(province=province, limit=limit)
    if not store_list:
        raise ValueError(f"No stores for province={province!r}")

    ratelimit.start_run()
    conn = db.connect(db_path)
    try:
        already = db.done_store_ids(conn, run_id) if resume else set()
        todo = [s for s in store_list if s["store_id"] not in already]

        def emit(**kw):
            if on_progress:
                on_progress({"run_id": run_id, "province": province,
                             "total": len(todo), "resumed": len(already), **kw})

        emit(phase="begin", done=0, store="", city="",
             eta_min=len(todo) * 25 * 60 / config.API_RATE_PER_MIN / 60)

        pacer = _Pacer(config.API_RATE_PER_MIN)
        t0 = time.time()
        rows_total = failed = completed = 0
        cancelled = False

        for i, st in enumerate(todo, 1):
            if should_stop and should_stop():
                cancelled = True
                break
            emit(phase="store", done=i - 1, store=st["name"],
                 city=st.get("city", ""), eta_min=_eta_min(t0, i - 1, len(todo)))
            try:
                rows = index_store(pacer, st, province)
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                closed = _close_out(conn, st["store_id"],
                                    {r["sku"] for r in rows}, now, st)
                db.write_rows(conn, run_id, rows + closed)
                rows_total += len(rows)
                completed += 1
                emit(phase="stored", done=i, store=st["name"],
                     city=st.get("city", ""), rows=len(rows),
                     instock=sum(1 for r in rows if r["available"]),
                     closed=len(closed), eta_min=_eta_min(t0, i, len(todo)))
            except Exception as e:                                # noqa: BLE001
                failed += 1
                emit(phase="failed", done=i, store=st["name"],
                     city=st.get("city", ""),
                     error=f"{type(e).__name__}: {str(e)[:60]}",
                     eta_min=_eta_min(t0, i, len(todo)))

        return {"run_id": run_id, "province": province, "rows": rows_total,
                "stores": len(todo), "completed": completed, "failed": failed,
                "cancelled": cancelled, "minutes": (time.time() - t0) / 60,
                "rate": ratelimit.summarize(),
                "rate_history": ratelimit.record("index")}
    finally:
        conn.close()


def _report_broken(db_path: str) -> int:
    """Which stores the scan endpoint keeps refusing.

    Read-only. The skip list should be maintained from this rather than from
    someone noticing a run felt slow -- that is how store 528 went unnoticed
    for weeks while costing 90s of backoff per run.
    """
    import fetchers

    conn = db.connect(db_path)
    try:
        broken = db.scan_failure_streaks(conn)
    finally:
        conn.close()

    already = fetchers.scan_skip_ids()
    print("=" * 74)
    print("Stores whose recent scan attempts all failed")
    print("=" * 74)
    if not broken:
        print("None. Every store has succeeded at least once recently.")
        return 0

    print(f"{'store':<7} {'name':<26} {'city':<16} {'fails':>5}  status")
    for b in broken:
        mark = "skipped" if b["store_id"] in already else "NOT SKIPPED"
        print(f"{b['store_id']:<7} {b['name'][:26]:<26} {b['city'][:16]:<16} "
              f"{b['failures']:>5}  {mark}")
        if b["error"]:
            print(f"        last error: {b['error'][:60]}")

    missing = [b for b in broken if b["store_id"] not in already]
    if missing:
        ids = ", ".join(f'"{b["store_id"]}"' for b in missing)
        print()
        print("To skip these on the live-check path, add to config.py:")
        print(f"    SCAN_SKIP_STORES = {{{ids}}}")
        print("or add them to scan_skip_stores in settings.json.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a province-wide stock index")
    ap.add_argument("--province", default=config.PROVINCE)
    ap.add_argument("--limit", type=int, default=None,
                    help="only index the first N stores (try 5 first)")
    ap.add_argument("--resume", metavar="RUN_ID", default=None)
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--broken-stores", action="store_true",
                    help="list stores whose recent scan attempts all failed, "
                         "then exit (skip-list candidates)")
    args = ap.parse_args(argv)

    if args.broken_stores:
        return _report_broken(args.db)

    def report(ev: dict) -> None:
        phase = ev["phase"]
        if phase == "begin":
            print("=" * 74)
            print(f"Stock index — {ev['province']}   run {ev['run_id']}")
            print("=" * 74)
            if ev["resumed"]:
                print(f"Resuming: {ev['resumed']} store(s) already indexed.")
            print(f"Stores: {ev['total']} to index   (~{ev['eta_min']:.0f} min)")
            print()
        elif phase == "stored":
            sold = (f", {ev['closed']:>4} sold out since last run"
                    if ev["closed"] else "")
            odd = ("  !! %d rows came back with no stock -- the search "
                   "endpoint no longer returns in-stock only"
                   % (ev["rows"] - ev["instock"])
                   if ev["rows"] != ev["instock"] else "")
            eta = ev["eta_min"]
            eta_s = f"   ETA {eta:.0f}m" if eta is not None else ""
            print(f"[{ev['done']}/{ev['total']}] {ev['store'][:26]:<26} "
                  f"{ev['city'][:14]:<14} {ev['instock']:>5} in stock"
                  f"{sold}{eta_s}{odd}")
        elif phase == "failed":
            print(f"[{ev['done']}/{ev['total']}] {ev['store'][:26]:<26} "
                  f"FAILED {ev['error']}")

    try:
        res = build_index(args.province, limit=args.limit, resume=args.resume,
                          db_path=args.db, on_progress=report)
    except ValueError as e:
        print(e)
        return 2

    print()
    print("=" * 74)
    print(f"Indexed {res['rows']} store-product rows in {res['minutes']:.1f} min"
          f"   ({res['failed']} store(s) failed)")
    print(f"DB: {args.db}   run_id: {res['run_id']}")
    if res["rate"]:
        print(res["rate"])
        hist = res["rate_history"]
        if hist and hist.get("runs", 0) > 1:
            print(f"Across {hist['runs']} runs the closest we have come is "
                  f"{hist['lowest_remaining']} left"
                  + (f" of {hist['limit']}" if hist.get("limit") else "")
                  + (f", {hist['throttled']} throttled in total"
                     if hist.get("throttled") else ", never throttled"))
    if res["failed"]:
        print(f"Retry just those:  python index_builder.py --resume {res['run_id']}")
    return 0 if not res["failed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted — re-run with --resume <run_id> to continue.")
        sys.exit(130)
