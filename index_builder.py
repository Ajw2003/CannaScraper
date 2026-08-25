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

How a run is structured
-----------------------
Every store is a durable work item in the `work_queue` table (workqueue.py),
claimed atomically by N workers. Each worker owns one route out of the egress
pool (egress.py), and each route carries its own request budget -- which is the
only thing that actually moves the wall clock, since a single route is
rate-limit-bound at ~46 minutes per province no matter how many threads push it.

With no proxies configured the pool is one route and this behaves as the old
sequential loop did, plus crash-durability and per-store retries.

Usage:
    python index_builder.py                     # Alberta
    python index_builder.py --province Ontario
    python index_builder.py --limit 5           # try a few stores first
    python index_builder.py --resume index-Alberta-20260817
    python index_builder.py --probe-egress      # is the pool worth having?
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config
import db
import egress
import ratelimit
import stores as S
import workqueue

SEARCH = config.API_BASE + "/product/search"

# Any common term returns the store's whole in-stock set; the parameter is
# required but does not meaningfully filter. 'a' is simply a safe default.
ENUM_TERM = "a"
PAGE_SIZE = 50          # server rejects anything larger

# A store is ~25 pages. This exists only so a `hasNextPage` that never goes
# false cannot spin a worker forever against someone else's bug.
MAX_PAGES = 200

# Not physical inventory. They appear at all 92 stores with six-figure "stock",
# which wrecks any "most widely available" or total-units aggregate.
SKIP_TITLE_PATTERNS = ("membership", "renewal", "gift card")


class FetchError(RuntimeError):
    """A page could not be retrieved, so the store is left for a retry."""


def _get(eg, term: str, store_id: str, page: int, province: str) -> dict:
    """One search page, paced on `eg` and retried with jittered backoff.

    Raises FetchError rather than returning None. The old version returned None
    on any failure and the caller treated that as "no more pages", so a single
    transient 500 midway through a store's pagination produced a *partial*
    store that then went through _close_out() -- marking every product it never
    reached as sold out. A failed store is recoverable; a store confidently
    recorded as half empty is not.
    """
    q = urllib.parse.urlencode({"title": term, "storeId": store_id,
                                "limit": PAGE_SIZE, "page": page,
                                "province": province})
    req = urllib.request.Request(
        f"{SEARCH}?{q}",
        headers={"User-Agent": config.USER_AGENT,
                 "Content-type": "application/json"})

    last = ""
    for attempt in range(1, config.API_MAX_RETRIES + 1):
        try:
            with eg.open(req, config.API_TIMEOUT_S * 3) as r:
                body = r.read().decode()
                ratelimit.observe(r.headers)
        except urllib.error.HTTPError as e:
            ratelimit.observe(e.headers, e.code, store_id)
            if e.code == 429:
                # The server's own hint first; jittered backoff otherwise, so
                # workers that hit the same wall do not retry in lockstep.
                delay = (float(e.headers.get("Retry-After") or 0)
                         or egress.backoff(attempt, 15.0))
                last = f"429 rate limited (waited {delay:.0f}s)"
                time.sleep(delay)
                continue
            if 500 <= e.code < 600:
                last = f"HTTP {e.code}"
                time.sleep(egress.backoff(attempt, 2.0))
                continue
            raise FetchError(f"HTTP {e.code}")
        except Exception as e:                                    # noqa: BLE001
            # Connection reset, DNS, timeout, a proxy that dropped us.
            last = f"{type(e).__name__}: {e}"[:80]
            time.sleep(egress.backoff(attempt, 2.0))
            continue

        # A blank/odd term makes the server serve its login page instead of
        # JSON. With ENUM_TERM that should never happen, so treat it as a
        # failure worth surfacing rather than as an empty result.
        if body.lstrip().startswith("<"):
            raise FetchError("server returned HTML, not JSON")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise FetchError("malformed JSON in response")

    raise FetchError(f"gave up after {config.API_MAX_RETRIES} attempts: {last}")


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


def index_store(eg, store: dict, province: str) -> list[dict]:
    """Every in-stock product at one store. Raises FetchError on any bad page."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: dict[str, dict] = {}
    page = 1
    while True:
        d = _get(eg, ENUM_TERM, str(store["store_id"]), page, province)
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
        if page > MAX_PAGES:
            raise FetchError(
                f"pagination did not terminate after {MAX_PAGES} pages")
    return list(rows.values())


def _close_out(conn, store_id: str, seen_skus: set, now: str,
               store: dict) -> list[dict]:
    """Zero-out products that were in stock last time but are absent now.

    The search endpoint returns in-stock items ONLY, so a sold-out product just
    disappears. Without this, `db.latest_observations()` would keep serving
    yesterday's in-stock row as the freshest fact and confidently send someone
    to a store that has none -- the exact failure this tool exists to prevent.

    This is also why index_store() must raise rather than return a short list:
    everything it failed to fetch would be closed out here as sold.
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
    """Minutes left, from the rate achieved so far. None until one store lands.

    Wall-clock based, so it accounts for however many workers are running
    without needing to be told how many there are.
    """
    if done <= 0:
        return None
    return (time.time() - t0) / done * (total - done) / 60


def build_index(province: str, *, limit: int | None = None,
                resume: str | None = None, db_path: str | None = None,
                on_progress=None, should_stop=None,
                workers: int | None = None,
                pool: list | None = None) -> dict:
    """Index every store in a province. The CLI and the web UI both call this.

    `on_progress(event)` receives a dict per milestone. `phase` is one of:
      begin   -- run_id, store count and the route list are known
      store   -- a worker claimed this store and is about to fetch it
      stored  -- this store landed; carries rows/instock/closed
      retry   -- this store failed but has attempts left; back in the queue
      failed  -- this store is out of attempts and the run gives up on it

    Every event after `begin` carries `worker` and `route`, because with more
    than one worker the lines interleave and are otherwise unreadable.

    On `rows` vs `instock`: the search endpoint returns in-stock products
    only, so every row this builds has available=1 and the two are equal.
    Reporting both as if they were separate facts is noise. They are kept
    separate here for one reason: if they ever diverge, the endpoint has
    stopped honouring that contract, and the CLI says so loudly instead of
    quietly indexing rows that are not actually in stock.

    `should_stop()` is polled before each claim. A store's paged fetch is not
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
    pool = pool if pool is not None else egress.build_pool()

    # --- seed the queue ----------------------------------------------------
    conn = db.connect(db_path)
    try:
        workqueue.ensure(conn)
        workqueue.enqueue(conn, run_id, province, store_list)
        if resume:
            # db.done_store_ids() is the authority on what actually landed --
            # it reads observations, not the queue -- so a run resumed from a
            # database that predates this queue still skips finished stores.
            workqueue.mark_done_from_history(
                conn, run_id, db.done_store_ids(conn, run_id))
            # Resuming is a deliberate second go, so stores that exhausted
            # their attempts get a fresh budget rather than staying failed.
            workqueue.requeue_failed(conn, run_id)
        # Only one index job runs at a time (jobs.py enforces it), so any
        # 'claimed' row here belongs to a process that is gone. Reclaiming now
        # beats waiting out WORK_LEASE_S for something we know is dead.
        workqueue.reset_stuck(conn, run_id)
        stats = workqueue.counts(conn, run_id)
        todo = stats[workqueue.PENDING]
        resumed = stats[workqueue.DONE]
        workqueue.prune(conn, config.WORK_KEEP_RUNS)
    finally:
        conn.close()

    n_workers = workers or config.INDEX_WORKERS or len(egress.healthy(pool))
    n_workers = max(1, min(int(n_workers), len(pool)))

    state = {"done": 0, "rows": 0, "completed": 0, "failed": 0,
             "retries": 0, "cancelled": False}
    lock = threading.Lock()
    out_lock = threading.Lock()
    t0 = time.time()

    def emit(**kw):
        if not on_progress:
            return
        # Serialized so two workers cannot interleave halves of one line.
        with out_lock:
            on_progress({"run_id": run_id, "province": province,
                         "total": todo, "resumed": resumed,
                         "workers": n_workers, **kw})

    emit(phase="begin", done=0, store="", city="", worker="", route="",
         routes=[e.snapshot() for e in pool],
         eta_min=(todo * 25 * 60 / config.EGRESS_RATE_PER_MIN
                  / max(1, n_workers) / 60))

    # --- the workers -------------------------------------------------------
    def work(eg) -> None:
        conn = db.connect(db_path)
        try:
            while True:
                if should_stop and should_stop():
                    with lock:
                        state["cancelled"] = True
                    return

                if not eg.healthy():
                    # Parked. Wait it out rather than claiming work this route
                    # cannot currently do -- another worker will take it.
                    time.sleep(min(2.0,
                                   max(0.1, eg.cooldown_until - time.time())))
                    continue

                item = workqueue.claim(conn, run_id, eg.name)
                if item is None:
                    return                      # queue drained

                store = {"store_id": item["store_id"], "name": item["name"],
                         "city": item["city"], "province": province}
                with lock:
                    done = state["done"]
                emit(phase="store", done=done, store=store["name"],
                     city=store["city"], worker=eg.name, route=eg.describe(),
                     attempt=item["attempts"],
                     eta_min=_eta_min(t0, done, todo))

                try:
                    rows = index_store(eg, store, province)
                    now = datetime.now(timezone.utc).isoformat(
                        timespec="seconds")
                    closed = _close_out(conn, item["store_id"],
                                        {r["sku"] for r in rows}, now, store)
                    db.write_rows(conn, run_id, rows + closed)
                    workqueue.complete(conn, run_id, item["store_id"])
                    eg.note_ok()
                    with lock:
                        state["rows"] += len(rows)
                        state["completed"] += 1
                        state["done"] += 1
                        done = state["done"]
                    emit(phase="stored", done=done, store=store["name"],
                         city=store["city"], worker=eg.name,
                         route=eg.describe(), rows=len(rows),
                         instock=sum(1 for r in rows if r["available"]),
                         closed=len(closed), eta_min=_eta_min(t0, done, todo))
                except Exception as e:                            # noqa: BLE001
                    msg = f"{type(e).__name__}: {str(e)[:60]}"
                    cooled = eg.note_failure()
                    retry = workqueue.fail(conn, run_id, item["store_id"], msg)
                    with lock:
                        if retry:
                            state["retries"] += 1
                        else:
                            state["failed"] += 1
                            state["done"] += 1
                        done = state["done"]
                    emit(phase="retry" if retry else "failed", done=done,
                         store=store["name"], city=store["city"],
                         worker=eg.name, route=eg.describe(), error=msg,
                         attempt=item["attempts"], cooldown_s=cooled,
                         eta_min=_eta_min(t0, done, todo))
        finally:
            conn.close()

    threads = [threading.Thread(target=work, args=(eg,), daemon=True,
                                name=f"index-{eg.name}")
               for eg in pool[:n_workers]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # --- how it went -------------------------------------------------------
    conn = db.connect(db_path)
    try:
        final = workqueue.counts(conn, run_id)
        gave_up = workqueue.failures(conn, run_id)
    finally:
        conn.close()

    return {"run_id": run_id, "province": province, "rows": state["rows"],
            "stores": todo, "completed": state["completed"],
            "failed": state["failed"], "retries": state["retries"],
            "cancelled": state["cancelled"],
            "minutes": (time.time() - t0) / 60,
            "workers": n_workers,
            "routes": [e.snapshot() for e in pool],
            "queue": final, "gave_up": gave_up,
            "rate": ratelimit.summarize(),
            "rate_history": ratelimit.record("index")}


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


def _probe_egress(province: str) -> int:
    """Do the configured routes actually hold separate rate-limit budgets?

    The entire case for the pool rests on the answer, and it costs one request
    per route to find out -- so find out, rather than inferring it from a run
    that felt faster than the last one.
    """
    pool = egress.build_pool()
    store_list = S.get_stores(province=province, limit=1)
    if not store_list:
        print(f"No stores for province={province!r}")
        return 2

    print("=" * 74)
    print(f"Egress probe -- {len(pool)} route(s), "
          f"drawing route 0 down by {egress.PROBE_DRAWDOWN} to see who feels it")
    print("=" * 74)
    res = egress.probe(pool, str(store_list[0]["store_id"]), province)
    print()
    print(egress.summarize_probe(res))
    if len(pool) == 1:
        print()
        print("Only the direct route is configured. Add proxies to "
              "EGRESS_PROXIES in config.py, or 'egress_proxies' in "
              "settings.json, then run this again.")
    return 0 if res["working"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a province-wide stock index")
    ap.add_argument("--province", default=config.PROVINCE)
    ap.add_argument("--limit", type=int, default=None,
                    help="only index the first N stores (try 5 first)")
    ap.add_argument("--resume", metavar="RUN_ID", default=None)
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--workers", type=int, default=None,
                    help="parallel workers; default is one per healthy route")
    ap.add_argument("--probe-egress", action="store_true",
                    help="send one request down each route and report whether "
                         "their rate-limit budgets are independent, then exit")
    ap.add_argument("--broken-stores", action="store_true",
                    help="list stores whose recent scan attempts all failed, "
                         "then exit (skip-list candidates)")
    args = ap.parse_args(argv)

    if args.broken_stores:
        return _report_broken(args.db)
    if args.probe_egress:
        return _probe_egress(args.province)

    multi = {"on": False}

    def report(ev: dict) -> None:
        phase = ev["phase"]
        # With one worker the route column is noise; with several it is the
        # only way to read interleaved lines.
        tag = f"{ev.get('worker', ''):<7} " if multi["on"] else ""
        if phase == "begin":
            multi["on"] = ev["workers"] > 1
            print("=" * 74)
            print(f"Stock index - {ev['province']}   run {ev['run_id']}")
            print("=" * 74)
            for r in ev["routes"]:
                print(f"  route {r['name']:<8} {r['route']:<30} "
                      f"{r['per_min']}/min")
            print(f"  {ev['workers']} worker(s), combined ceiling "
                  f"{ev['workers'] * config.EGRESS_RATE_PER_MIN} req/min")
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
            print(f"[{ev['done']}/{ev['total']}] {tag}{ev['store'][:26]:<26} "
                  f"{ev['city'][:14]:<14} {ev['instock']:>5} in stock"
                  f"{sold}{eta_s}{odd}")
        elif phase == "retry":
            cool = (f", route parked {ev['cooldown_s']:.0f}s"
                    if ev.get("cooldown_s") else "")
            print(f"[{ev['done']}/{ev['total']}] {tag}{ev['store'][:26]:<26} "
                  f"retry {ev['attempt']}/{config.WORK_MAX_ATTEMPTS} "
                  f"{ev['error']}{cool}")
        elif phase == "failed":
            print(f"[{ev['done']}/{ev['total']}] {tag}{ev['store'][:26]:<26} "
                  f"FAILED {ev['error']}")

    try:
        res = build_index(args.province, limit=args.limit, resume=args.resume,
                          db_path=args.db, on_progress=report,
                          workers=args.workers)
    except ValueError as e:
        print(e)
        return 2

    print()
    print("=" * 74)
    print(f"Indexed {res['rows']} store-product rows in {res['minutes']:.1f} min"
          f"   ({res['failed']} store(s) failed, {res['retries']} retried)")
    print(f"DB: {args.db}   run_id: {res['run_id']}")

    if res["workers"] > 1:
        print()
        print(f"{'route':<10} {'via':<30} {'requests':>9} {'failures':>9}  state")
        for r in res["routes"]:
            how = "ok" if r["healthy"] else f"cooling {r['cooldown_s']:.0f}s"
            print(f"{r['name']:<10} {r['route'][:30]:<30} {r['requests']:>9} "
                  f"{r['failures']:>9}  {how}")

    if res["rate"]:
        print(res["rate"])
        hist = res["rate_history"]
        if hist and hist.get("runs", 0) > 1:
            print(f"Across {hist['runs']} runs the closest we have come is "
                  f"{hist['lowest_remaining']} left"
                  + (f" of {hist['limit']}" if hist.get("limit") else "")
                  + (f", {hist['throttled']} throttled in total"
                     if hist.get("throttled") else ", never throttled"))

    if res["gave_up"]:
        print()
        print("Stores the run gave up on:")
        for g in res["gave_up"]:
            print(f"  {g['store_id']:<7} {g['name'][:26]:<26} "
                  f"{g['attempts']} attempts  {g['error'][:40]}")
    if res["failed"] or res["cancelled"]:
        print(f"Retry just those:  "
              f"python index_builder.py --resume {res['run_id']}")
    return 0 if not res["failed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted - re-run with --resume <run_id> to continue.")
        sys.exit(130)
