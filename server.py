"""Local web UI.

    .venv\\Scripts\\python server.py
    -> http://localhost:8000        (and http://<your-lan-ip>:8000 from a phone)

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
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

import catalog
import config
import db
import fetchers
import main as cli          # reuse fill_missing_stores / tier_price
import stores as S

app = FastAPI(title="Canna Cabana stock")
HERE = Path(__file__).parent
JOBS: dict[str, dict] = {}


# --- helpers ---------------------------------------------------------------

def thumb(url: str | None, w: int = 200) -> str:
    """Shopify resizes on demand; an 800px original is ~22x the bytes."""
    if not url:
        return ""
    return f"{url}{'&' if '?' in url else '?'}width={w}"


def _targets(sku: str, cat) -> list[dict]:
    return [v for v in cat if str(v.get("sku")) == str(sku)]


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
    return FileResponse(HERE / "web" / "index.html")


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
def api_search(q: str = "", limit: int = 50, offset: int = 0):
    """Product search -- catalogue only, no network calls, instant.

    Returns the total so the UI can page rather than silently truncating:
    "vape" matches over a thousand products, and showing 24 of them with no
    indication looks like the search is broken.
    """
    if not q.strip():
        return {"products": [], "total": 0, "offset": 0}

    # Dedupe by SKU first, then page -- otherwise the total is wrong and
    # paging skips items.
    seen, uniq = set(), []
    for v in catalog.search(q, limit=None):
        if v["sku"] in seen:
            continue
        seen.add(v["sku"])
        uniq.append(v)

    page = uniq[offset: offset + limit]
    return {
        "total": len(uniq),
        "offset": offset,
        "products": [{
            "sku": v["sku"], "title": v["title"], "brand": v["brand"],
            "size": v["size"], "category": v["category"],
            "image": thumb(v.get("image"), 160),
        } for v in page],
    }


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
                    "size": v["size"], "image": thumb(v.get("image"), 320)},
        "scope": scope, "checked": len(store_list),
        "in_stock": sum(1 for r in packed if r["available"]),
        "age_hours": age,
        "indexed_stores": indexed,
        "index_age_hours": index_age,
        "results": packed,
    }


@app.post("/api/refresh")
async def api_refresh(sku: str, lat: float | None = None, lng: float | None = None,
                      near: str | None = None, top: int = config.DEFAULT_TOP,
                      province: str | None = None, all_stores: bool = False,
                      fetcher: str | None = None):
    """Kick off a live re-check. Returns a job id to poll."""
    cat = catalog.get_catalog(verbose=False)
    targets = _targets(sku, cat)
    if not targets:
        return JSONResponse({"error": f"unknown sku {sku}"}, status_code=404)

    store_list, _ = _scope(lat, lng, near, top, province, all_stores)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"done": 0, "total": len(store_list), "store": "",
                    "finished": False, "error": None, "started": time.time()}
    asyncio.create_task(_run_refresh(job_id, store_list, targets, fetcher))
    return {"job": job_id, "total": len(store_list)}


async def _run_refresh(job_id: str, store_list: list[dict],
                       targets: list[dict], fetcher: str | None = None):
    job = JOBS[job_id]
    run_id = f"web-{job_id}"
    conn = db.connect()
    try:
        async with fetchers.get_fetcher(fetcher) as f:
            for i, st in enumerate(store_list, 1):
                job["store"] = f"{st['name']}, {st['city']}"
                try:
                    rows = await f.fetch(st, targets, verbose=False)
                    db.write_rows(conn, run_id, rows)
                except Exception as e:                      # noqa: BLE001
                    job["error"] = f"{st['name']}: {type(e).__name__}"
                job["done"] = i
    except Exception as e:                                  # noqa: BLE001
        job["error"] = f"{type(e).__name__}: {e}"[:200]
    finally:
        conn.close()
        job["finished"] = True


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return job


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
    import uvicorn

    port = 8000
    print("=" * 60)
    print("  Canna Cabana stock — web UI")
    print("=" * 60)
    print(f"  This computer :  http://localhost:{port}")
    print(f"  Phone / LAN   :  http://{_lan_ip()}:{port}")
    print("\n  Ctrl-C to stop.\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
