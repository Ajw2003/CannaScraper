"""Web UI routes.

    .venv\\Scripts\\python server.py
    -> http://127.0.0.1:8000        (and http://<your-lan-ip>:8000 from a phone)

That entry point is local and LAN only. `app.py` is the full application --
same routes, plus the public tunnel and the console banner -- and is what the
packaged exe runs.

Thin layer over what already exists: catalog.search for products,
stores.nearest for geography, db.latest_observations for instant answers, and
the api fetcher for a live re-check. It adds no scraping logic of its own.

A live re-check takes 1-2s per store, so it runs as a background job with
progress rather than blocking the request.
"""

from __future__ import annotations

import asyncio
import socket
import time

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import auth
import catalog
import config
import db
import fetchers
import jobs
import main as cli          # reuse fill_missing_stores / tier_price
import paths
import ratelimit
import stores as S

app = FastAPI(title="Canna Cabana stock")
WEB = paths.APP_DIR / "web"


# --- helpers ---------------------------------------------------------------

def thumb(url: str | None, w: int = 200) -> str:
    """Shopify resizes on demand; an 800px original is ~22x the bytes."""
    if not url:
        return ""
    return f"{url}{'&' if '?' in url else '?'}width={w}"


def _targets(sku: str, cat) -> list[dict]:
    return [v for v in cat if str(v.get("sku")) == str(sku)]


# Per-SKU facts for a province: in stock anywhere, category, THC/CBD. Costs a
# ~250ms scan over 90k rows, and search runs on every debounced keystroke, so
# cache it briefly. It only changes when an index or live check writes.
_FACTS: dict[str, tuple[float, dict]] = {}
_FACTS_TTL = 120.0


def province_facts(province: str) -> dict[str, dict]:
    hit = _FACTS.get(province)
    if hit and time.time() - hit[0] < _FACTS_TTL:
        return hit[1]
    ids = [s["store_id"] for s in S.get_stores(province=province)]
    conn = db.connect()
    try:
        facts = db.province_facts(conn, ids)
    finally:
        conn.close()
    _FACTS[province] = (time.time(), facts)
    return facts


def invalidate_facts(province: str | None, result=None) -> None:
    """Forget the cached facts for a province after something wrote rows.

    Without this a finished index build is invisible for up to two minutes,
    which reads as "the refresh did nothing".
    """
    if province:
        _FACTS.pop(province, None)
    else:
        _FACTS.clear()


jobs.index_finished_hook = invalidate_facts


# THC/CBD arrive as a bare number whose unit depends on the product: a
# percentage for flower/vape/concentrate, milligrams for edibles. Values above
# 100 are always mg, because a percentage cannot exceed 100 -- that is what
# catches infused pre-rolls listed at e.g. 615.
_MG_CATEGORIES = {
    "edibles", "gummies", "beverages", "chocolates", "oils & capsules",
    "capsules", "oils", "topicals", "soft chews", "mints", "baked goods",
}


def potency(value, category: str) -> str:
    """Format THC/CBD with the unit its magnitude and category imply."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if (category or "").strip().lower() in _MG_CATEGORIES or n > 100:
        return f"{n:g} mg"
    return f"{n:g}%"


def _scope(lat=None, lng=None, near=None, top=None, province=None,
           all_stores=False):
    """The stores this query covers, plus a human label for them.

    Location can come from the browser (lat/lng), a typed place ("Calgary, AB",
    a postal code), or config.HOME -- matching what the CLI accepts.
    """
    prov = province or config.PROVINCE
    store_list = S.get_stores(province=prov)

    # Resolve location FIRST, independently of scope. "Whole province" only
    # removes the count limit -- it must not throw away where you are, or the
    # closest-first sort silently stops working on exactly the search where
    # ranking matters most.
    if lat is not None and lng is not None:
        loc, where = (float(lat), float(lng)), "your location"
    else:
        loc = S.resolve_location(near) if near else S.resolve_location(None)
        where = near or str(config.HOME)

    if not loc:
        if all_stores:
            return store_list, f"all {len(store_list)} stores in {prov}"
        return store_list[: top or config.DEFAULT_TOP], f"{prov} (unlocated)"

    # nearest() with no limit = every store, still carrying distance_km.
    limit = None if all_stores else (top or config.DEFAULT_TOP)
    found = S.nearest(store_list, loc[0], loc[1], limit)
    label = (f"all {len(found)} stores in {prov}, nearest first"
             if all_stores else f"{len(found)} nearest")
    return found, f"{label} to {where}"


def _pack(rows: list[dict], store_list: list[dict],
          sort: str = "distance") -> list[dict]:
    """Shape rows for the browser.

    Default sort is nearest-first: 13 units 2 km away beats 17 units 15 km
    away, because the trip is the cost, not the shelf depth. Stock only breaks
    ties between similarly-close stores. `sort="stock"` restores depth-first
    for when you want the store least likely to have sold out.

    Out-of-stock rows always sink to the bottom either way.
    """
    by_id = {s["store_id"]: s for s in store_list}
    out = []
    for r in rows:
        st = by_id.get(r["store_id"], {})
        label, deal = cli.tier_price(r)
        market = r.get("price")
        save = None
        if market and deal and deal < market:
            save = {"amount": round(market - deal, 2),
                    "pct": round(100 * (market - deal) / market)}
        out.append({
            "store": r.get("store_name"), "city": r.get("city"),
            "store_id": r.get("store_id"),
            "distance_km": r.get("distance_km", st.get("distance_km")),
            "qty": r.get("api_stock"),
            "available": bool(r.get("available")),
            "carried": r.get("carried"),
            "stock_text": r.get("stock_text"),
            "market": market, "tier": label, "tier_price": deal, "save": save,
            "is_elite": bool(r.get("is_elite")),
            "url": r.get("url"),
        })
    far = 9e9
    if sort == "stock":
        out.sort(key=lambda r: (not r["available"], -(r["qty"] or 0),
                                r["distance_km"] if r["distance_km"] is not None else far))
    else:
        # Nearest first. With no location (whole-province, no geocode) every
        # distance is None, so this quietly degrades to stock-depth order.
        out.sort(key=lambda r: (not r["available"],
                                r["distance_km"] if r["distance_km"] is not None else far,
                                -(r["qty"] or 0)))
    return out


# --- routes ----------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/provinces")
def api_provinces():
    """Provinces with store counts, straight from the registry."""
    everything = S.get_stores(province="")
    counts: dict[str, int] = {}
    for st in everything:
        if st.get("province"):
            counts[st["province"]] = counts.get(st["province"], 0) + 1
    return {"provinces": [{"name": k, "stores": v}
                          for k, v in sorted(counts.items(), key=lambda kv: -kv[1])],
            "default": config.PROVINCE, "home": config.HOME}


@app.get("/api/search")
def api_search(q: str = "", limit: int = 50, offset: int = 0,
               province: str | None = None, stocked_only: bool = True,
               category: str | None = None):
    """Product search -- catalogue only, no network calls, instant.

    Returns the total so the UI can page rather than silently truncating:
    "vape" matches over a thousand products, and showing 24 of them with no
    indication looks like the search is broken.

    `stocked_only` hides products the whole province is out of. Most of the
    catalogue is not stocked in any given province, so unfiltered results are
    mostly things you cannot buy. `hidden` is always returned so the UI can
    say what was filtered rather than quietly dropping it.
    """
    if not q.strip():
        return {"products": [], "total": 0, "offset": 0, "hidden": 0,
                "filtered": False}

    # Dedupe by SKU first, then page -- otherwise the total is wrong and
    # paging skips items.
    seen, uniq = set(), []
    for v in catalog.search(q, limit=None):
        if v["sku"] in seen:
            continue
        seen.add(v["sku"])
        uniq.append(v)

    prov = province or config.PROVINCE
    facts = province_facts(prov)
    # With no index for this province we know nothing, so filtering would hide
    # everything. Fall back to showing all and say the filter is inactive.
    can_filter = bool(facts)
    hidden = 0

    if category:
        want = category.strip().lower()
        uniq = [v for v in uniq
                if (facts.get(v["sku"], {}).get("category")
                    or v.get("category", "")).lower() == want]

    if stocked_only and can_filter:
        before = len(uniq)
        uniq = [v for v in uniq if facts.get(v["sku"], {}).get("available")]
        hidden = before - len(uniq)

    page = uniq[offset: offset + limit]
    return {
        "total": len(uniq),
        "offset": offset,
        "hidden": hidden,
        "filtered": bool(stocked_only and can_filter),
        "can_filter": can_filter,
        "province": prov,
        "products": [{
            "sku": v["sku"], "title": v["title"], "brand": v["brand"],
            "size": v["size"],
            "category": facts.get(v["sku"], {}).get("category") or v["category"],
            "in_stock": bool(facts.get(v["sku"], {}).get("available")),
            "thc": potency(facts.get(v["sku"], {}).get("thc"),
                           facts.get(v["sku"], {}).get("category", "")),
            "cbd": potency(facts.get(v["sku"], {}).get("cbd"),
                           facts.get(v["sku"], {}).get("category", "")),
            "image": thumb(v.get("image"), 160),
        } for v in page],
    }


@app.get("/api/categories")
def api_categories(province: str | None = None, stocked_only: bool = True):
    """Categories present in this province's index, biggest first."""
    facts = province_facts(province or config.PROVINCE)
    counts: dict[str, int] = {}
    for f in facts.values():
        if stocked_only and not f["available"]:
            continue
        cat = (f.get("category") or "").strip()
        if cat:
            counts[cat] = counts.get(cat, 0) + 1
    return {"categories": [{"name": k, "skus": v}
                           for k, v in sorted(counts.items(),
                                              key=lambda kv: -kv[1])]}


@app.get("/api/results")
def api_results(sku: str, lat: float | None = None, lng: float | None = None,
                near: str | None = None, top: int = config.DEFAULT_TOP,
                province: str | None = None, all_stores: bool = False,
                sort: str = "distance"):
    """Instant answer from the index/cache. No scraping."""
    cat = catalog.get_catalog(verbose=False)
    targets = _targets(sku, cat)
    if not targets:
        return JSONResponse({"error": f"unknown sku {sku}"}, status_code=404)

    store_list, scope = _scope(lat, lng, near, top, province, all_stores)
    store_ids = [s["store_id"] for s in store_list]
    conn = db.connect()
    try:
        rows = db.latest_observations(conn, [sku], store_ids)
        age = db.cache_age_hours(rows)
        # The index holds in-stock items only, so zero rows is ambiguous:
        # either we never looked, or we looked and it isn't stocked. Coverage
        # tells them apart, and the UI must not report the second as "no data".
        indexed, index_age = db.index_coverage(conn, store_ids)
        rows = cli.fill_missing_stores(rows, store_list, targets, conn)
    finally:
        conn.close()

    v = targets[0]
    packed = _pack(rows, store_list, sort)
    return {
        "product": {"sku": sku, "title": v["title"], "brand": v["brand"],
                    "size": v["size"], "image": thumb(v.get("image"), 320),
                    "category": next((r.get("category") for r in rows
                                      if r.get("category")), v.get("category", "")),
                    "thc": potency(next((r.get("thc") for r in rows if r.get("thc")), ""),
                                   next((r.get("category") for r in rows
                                         if r.get("category")), "")),
                    "cbd": potency(next((r.get("cbd") for r in rows if r.get("cbd")), ""),
                                   next((r.get("category") for r in rows
                                         if r.get("category")), ""))},
        "scope": scope, "checked": len(store_list),
        "in_stock": sum(1 for r in packed if r["available"]),
        "age_hours": age,
        "indexed_stores": indexed,
        "index_age_hours": index_age,
        "results": packed,
    }


@app.post("/api/refresh")
def api_refresh(sku: str, lat: float | None = None, lng: float | None = None,
                near: str | None = None, top: int = config.DEFAULT_TOP,
                province: str | None = None, all_stores: bool = False,
                fetcher: str | None = None,
                _admin: bool = Depends(auth.require_admin)):
    """Queue a live re-check. Returns a job id to poll.

    Password-gated: it spends the shared rate-limit budget and sends traffic
    to the site from whichever machine is hosting this.
    """
    cat = catalog.get_catalog(verbose=False)
    targets = _targets(sku, cat)
    if not targets:
        return JSONResponse({"error": f"unknown sku {sku}"}, status_code=404)
    try:
        fetchers.check_available(fetcher)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    store_list, _scope_label = _scope(lat, lng, near, top, province, all_stores)
    prov = province or config.PROVINCE
    title = targets[0]["title"][:40]

    def make(job):
        return _run_refresh(job, store_list, targets, prov, fetcher)

    job = jobs.submit_live(f"Live check: {title} ({len(store_list)} stores)",
                           len(store_list), make, sku=sku, province=prov)
    return {"job": job["id"], "total": len(store_list)}


async def _run_refresh(job: dict, store_list: list[dict], targets: list[dict],
                       province: str, fetcher: str | None = None) -> None:
    """Re-check one product across stores. Runs on the job worker's own loop.

    Stores go out together rather than one after another. The fetcher's own
    semaphore (config.API_CONCURRENCY) is what bounds how many are in flight,
    and it can only do that if it is handed more than one at a time -- awaiting
    each store in turn left that semaphore permanently at one, which is what
    made a whole-province check cost ten seconds per store.

    Per-store failures are recorded and the sweep continues: one store timing
    out should not cost the other ninety-one.
    """
    run_id = f"web-{job['id']}"
    conn = db.connect()
    lock = asyncio.Lock()
    started = time.time()
    title = targets[0].get("title", "")[:40]

    ratelimit.start_run()
    jobs.echo(f"  [live] {title}: {len(store_list)} stores")
    try:
        async with fetchers.get_fetcher(fetcher) as f:
            async def one(st: dict) -> None:
                if job["cancel_requested"]:
                    return
                try:
                    rows = await f.fetch(st, targets, verbose=False)
                except Exception as e:                      # noqa: BLE001
                    async with lock:
                        job["failed_stores"] += 1
                        job["done"] += 1
                        job["error"] = f"{st['name']}: {type(e).__name__}"
                        jobs.echo(f"  [live] [{job['done']}/{job['total']}] "
                                  f"{st['name'][:26]:<26} FAILED "
                                  f"{type(e).__name__}")
                    return

                async with lock:
                    # One connection, one thread, so the writes need ordering
                    # but not a second connection.
                    db.write_rows(conn, run_id, rows)
                    job["rows"] += len(rows)
                    job["done"] += 1
                    job["store"] = job["current"] = f"{st['name']}, {st['city']}"
                    if job["done"]:
                        per = (time.time() - started) / job["done"]
                        job["eta_min"] = per * (job["total"] - job["done"]) / 60
                    jobs.echo(f"  [live] [{job['done']}/{job['total']}] "
                              f"{st['name'][:26]:<26} {_stock_note(rows)}")

            await asyncio.gather(*(one(st) for st in store_list))
    finally:
        conn.close()
        invalidate_facts(province)
        jobs.echo(f"  [live] {title}: done, {job['done']}/{job['total']} "
                  f"stores in {(time.time() - started) / 60:.1f} min"
                  + (f", {job['failed_stores']} failed"
                     if job["failed_stores"] else ""))
        rate = ratelimit.summarize()
        ratelimit.record("live")
        if rate:
            jobs.echo(f"  [live] {rate}")


def _stock_note(rows: list[dict]) -> str:
    """What this store said, in a few words, for the console line."""
    for r in rows:
        if r.get("status") != "ok":
            return "error"
        if r.get("available"):
            qty = r.get("api_stock")
            return f"in stock{'' if qty is None else f' x{qty}'}"
        if not r.get("carried"):
            return "not carried"
        return "sold out"
    return "no data"


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return job


@app.get("/api/jobs")
def api_jobs():
    """Everything active plus recent history, for the index panel."""
    return {"jobs": [_brief(j) for j in jobs.all_jobs()[:20]]}


def _brief(job: dict) -> dict:
    return {k: job.get(k) for k in
            ("id", "kind", "label", "state", "province", "sku", "run_id",
             "total", "done", "current", "eta_min", "rows", "failed_stores",
             "resumed", "error", "started", "finished")}


# --- the index panel -------------------------------------------------------

@app.get("/api/index/status")
def api_index_status():
    """Per-province coverage and whatever is running. Open -- it is read-only."""
    by_prov: dict[str, list] = {}
    for st in S.get_stores(province=""):
        if st.get("province"):
            by_prov.setdefault(st["province"], []).append(st)

    conn = db.connect()
    try:
        out = []
        for prov, sts in sorted(by_prov.items(), key=lambda kv: -len(kv[1])):
            ids = [s["store_id"] for s in sts]
            indexed, age = db.index_coverage(conn, ids)
            runs = db.index_runs(conn, prov, limit=1)
            last = runs[0] if runs else None
            job = jobs.active_for_province(prov)
            out.append({
                "province": prov,
                "stores": len(sts),
                "indexed": indexed,
                "age_hours": age,
                "last_run": last["run_id"] if last else None,
                "last_run_stores": last["stores"] if last else 0,
                # A run that never reached every store can be resumed rather
                # than restarted, which on Ontario is the difference between
                # 10 minutes and 49.
                "incomplete": bool(last and last["stores"] < len(sts)),
                "estimate_min": round(len(sts) * 25 * 60
                                      / config.API_RATE_PER_MIN / 60),
                "job": _brief(job) if job else None,
            })
    finally:
        conn.close()

    # Everything in the queue, not just index jobs. One worker runs one job at
    # a time, so a live check can hold a province build up for minutes -- and
    # a province row showing "queued" with nothing saying what it is queued
    # behind is exactly the state that looks like the app has stalled.
    active = [_brief(j) for j in jobs.active()]
    running = next((j for j in active if j["state"] == "running"), None)
    return {"provinces": out,
            "busy": bool(active),
            "active": active,
            "running": running,
            # Measured headroom against the server's advertised budget, built
            # up from headers we already receive. See ratelimit.py.
            "rate": ratelimit.history()}


@app.post("/api/index/{province}")
def api_index_start(province: str, resume: str | None = None,
                    limit: int | None = None,
                    _admin: bool = Depends(auth.require_admin)):
    """Queue a province rebuild. `resume=auto` picks up an interrupted run."""
    store_list = S.get_stores(province=province)
    if not store_list:
        return JSONResponse({"error": f"unknown province {province!r}"},
                            status_code=404)

    if resume == "auto":
        conn = db.connect()
        try:
            runs = db.index_runs(conn, province, limit=1)
        finally:
            conn.close()
        resume = (runs[0]["run_id"]
                  if runs and runs[0]["stores"] < len(store_list) else None)

    try:
        job = jobs.submit_index(province, resume=resume, limit=limit)
    except jobs.Busy as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return {"job": job["id"], "province": province, "resume": resume}


@app.post("/api/index/{province}/cancel")
def api_index_cancel(province: str,
                     _admin: bool = Depends(auth.require_admin)):
    job = jobs.active_for_province(province)
    if not job:
        return JSONResponse({"error": "nothing running for that province"},
                            status_code=404)
    jobs.cancel(job["id"])
    return {"ok": True, "job": job["id"]}


# --- capabilities and login ------------------------------------------------

@app.get("/api/capabilities")
def api_capabilities(request: Request):
    return {"playwright": fetchers.have_browser(),
            "sources": fetchers.available(),
            "live_seconds_per_store": config.LIVE_SECONDS_PER_STORE,
            "admin": auth.verify_token(request.cookies.get(auth.COOKIE, ""))}


class Login(BaseModel):
    password: str


@app.post("/api/login")
def api_login(body: Login, request: Request, response: Response):
    key = auth.client_key(request)
    wait = auth.throttled(key)
    if wait:
        return JSONResponse(
            {"error": f"too many attempts — wait {wait:.0f}s"}, status_code=429)

    if not auth.check_password(body.password):
        auth.note_failure(key)
        return JSONResponse({"error": "wrong password"}, status_code=401)

    auth.note_success(key)
    # Behind the tunnel the hop to us is plain http, so trust the forwarded
    # scheme to decide whether the cookie may be marked Secure.
    https = (request.url.scheme == "https"
             or request.headers.get("x-forwarded-proto") == "https")
    response.set_cookie(auth.COOKIE, auth.make_token(), httponly=True,
                        samesite="lax", secure=https,
                        max_age=auth.SESSION_HOURS * 3600)
    return {"ok": True}


@app.post("/api/logout")
def api_logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


def _lan_ip() -> str:
    """Best-guess LAN address, so the phone URL can be printed."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


if __name__ == "__main__":
    # Local + LAN only, no tunnel. `python app.py` is the full launcher and is
    # what the packaged exe runs; this stays as the plain dev entry point.
    import uvicorn

    settings = auth.load()
    port = int(settings["port"])
    generated = auth.ensure_configured()

    print("=" * 60)
    print("  Canna Cabana stock — web UI (dev)")
    print("=" * 60)
    print(f"  This computer :  http://127.0.0.1:{port}")
    print(f"  Phone / LAN   :  http://{_lan_ip()}:{port}")
    if generated:
        print(f"  Admin password:  {generated}   <- write this down")
    else:
        print("  Admin password:  already set")
    print(f"  Data          :  {paths.DATA_DIR}")
    print("\n  Ctrl-C to stop.\n")
    jobs.start()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
