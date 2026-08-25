"""Internal-API backend -- the fast path.

The site prices every product by POSTing to

    /api/product/scan-multiple-items/<store_id>     {"skus":[{"<sku>": <variant_id>}]}

One call carries a whole watchlist, so cost scales with the number of STORES,
not stores x products. That is where it beats the browser: five products cost
exactly the same as one.

Two things verified by probe before this was written:

  * **No authentication is required.** The endpoint answers unauthenticated.
    The site's page mints a client-credentials token but never attaches it to
    this call. So we send no credentials at all -- see config.API_SEND_AUTH.

  * **The server rate-limits at 60/min** (`X-RateLimit-Limit: 60`) and 429s on
    short bursts. We pace under that and stay sequential; at ~0.7s latency the
    rate limit is the binding constraint, so concurrency would buy nothing.

`getEffectiveStoreId` -- the delivery-mode hub substitution that silently
collapsed 34 Calgary stores onto two IDs on the browser path -- does not exist
here. We pass the real store_id, so that failure mode is structurally absent.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import config
import ratelimit
import scrape          # reuse _parse_scan / _cannabinoids -- one decoder only


class _RateLimiter:
    """Minimum spacing between calls, shared across a run."""

    def __init__(self, per_min: int):
        self._interval = 60.0 / max(1, per_min)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._interval:
                await asyncio.sleep(self._interval - gap)
            self._last = time.monotonic()


class ApiFetcher:
    name = "api"
    # This backend enforces its own spacing (_RateLimiter), so the caller must
    # NOT add its own inter-store delay on top -- that would triple a sweep.
    paces_itself = True

    def __init__(self):
        self._limiter = _RateLimiter(config.API_RATE_PER_MIN)
        self._sem = asyncio.Semaphore(max(1, config.API_CONCURRENCY))
        self._token: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    # --- transport ---------------------------------------------------------

    def _post(self, url: str, payload: dict) -> tuple[int, object, dict]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": config.USER_AGENT,
        }
        if config.API_SEND_AUTH and self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=config.API_TIMEOUT_S) as r:
                return r.status, json.loads(r.read().decode()), dict(r.headers)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")[:200]
            return e.code, body, dict(e.headers)

    async def _call(self, store_id: str, skus: list[dict]) -> dict:
        """One scan call, paced and retried. Raises on give-up."""
        last = ""
        for attempt in range(1, config.API_MAX_RETRIES + 1):
            async with self._sem:
                await self._limiter.wait()
                status, body, headers = await asyncio.to_thread(
                    self._post,
                    config.API_SCAN.format(store_id=store_id),
                    {"skus": skus},
                )
            # Free telemetry: the budget headers ride on every response.
            ratelimit.observe(headers, status)

            if status == 200 and isinstance(body, dict) and body.get("success"):
                return body

            if status == 429:
                # Respect the server's own backoff hint when it gives one.
                delay = float(headers.get("Retry-After") or 0) or 15.0 * attempt
                last = f"429 rate limited (waited {delay:.0f}s)"
                await asyncio.sleep(delay)
                continue

            if status and 500 <= status < 600:
                last = f"HTTP {status}"
                await asyncio.sleep(2.0 * attempt)
                continue

            # A 4xx that isn't 429, or a success:false -- retrying won't help.
            snippet = body if isinstance(body, str) else json.dumps(body)[:150]
            raise RuntimeError(f"HTTP {status}: {snippet}")

        raise RuntimeError(f"gave up after {config.API_MAX_RETRIES} attempts: {last}")

    # --- fetching ----------------------------------------------------------

    async def fetch(self, store: dict, variants: list[dict],
                    verbose: bool = True) -> list[dict]:
        """One row per variant, matching scrape.scrape_variant()'s shape."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sid = str(store["store_id"])

        # {sku: variant_id} pairs; skip variants with no SKU to map back by.
        payload = [{str(v["sku"]): v["variant_id"]}
                   for v in variants if v.get("sku")]

        items: dict = {}
        elite: dict = {}
        missing: set = set()
        err = ""

        if payload:
            try:
                body = await self._call(sid, payload)
                data = body.get("data") or {}
                items = data.get("scanned-items") or {}
                elite = data.get("elitePrices") or {}
                missing = {str(m) for m in (data.get("missingItems") or [])}
            except Exception as e:                       # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:300]

        rows = []
        for v in variants:
            rows.append(self._row(store, v, now, items, elite, missing, err))
            if verbose:
                r = rows[-1]
                flag = "OK " if r["status"] == "ok" else "ERR"
                price = f"${r['price']:.2f}" if r["price"] else "-"
                memb = f"${r['member_price']:.2f}" if r["member_price"] else "-"
                qty = "" if r["api_stock"] is None else f" x{r['api_stock']}"
                print(f"    [{flag}] {v.get('sku') or '-':>8}  {price:>9} / {memb:>9}"
                      f"  {(r['stock_text'] or '-') + qty:<16} {v['title'][:34]}")
        return rows

    def _row(self, store, v, now, items, elite, missing, err) -> dict:
        sku = str(v.get("sku") or "")
        thc, cbd = scrape._cannabinoids(v)
        sid = str(store["store_id"])

        row = {
            "scraped_at": now,
            "store_id": sid,
            "store_name": store.get("name", ""),
            "city": store.get("city", ""),
            "province": store.get("province", ""),
            "sku": sku,
            "handle": v.get("handle", ""),
            "title": v.get("title", ""),
            "brand": v.get("brand", ""),
            "category": v.get("category", ""),
            "size": v.get("size", ""),
            "url": f"{config.BASE}/products/{v.get('handle','')}?sID={sid}",
            "price": None,
            "member_price": None,
            "default_price": v.get("default_price"),
            "available": None,
            "carried": None,
            "stock_text": "",
            "thc": thc,
            "cbd": cbd,
            "store_label": "",
            # We choose the store_id ourselves here, so no hub substitution is
            # possible. Recorded as matching to keep downstream filters simple.
            "api_store_id": sid,
            "store_id_match": 1,
            "api_stock": None,
            "api_member_price": None,
            "api_price": None,
            "api_elite_price": None,
            "api_equiv_g": None,
            "api_raw": "",
            "is_elite": None,
            "image": v.get("image", ""),
            "status": "ok",
            "error": "",
        }

        if err:
            row["status"] = "error"
            row["error"] = err
            return row

        blob = items.get(sku)
        if blob is None:
            # Genuinely absent from this store's catalogue.
            row["carried"] = 0
            row["available"] = 0
            row["stock_text"] = "Not carried"
            if sku not in missing:
                row["error"] = "sku absent from response"
            return row

        row.update(scrape._parse_scan(blob))

        try:
            ep = float(elite.get(sku) or 0) or None
        except (TypeError, ValueError):
            ep = None
        row["api_elite_price"] = ep
        # The scan endpoint has no is_elite flag, but the tiers are
        # mutually exclusive, so it is recoverable: an ELITE price with
        # no member price means an ELITE-tier product.
        row["is_elite"] = 1 if (ep and not row.get("api_member_price")) else 0

        retail = row.get("api_price")
        carried = bool(retail and retail > 0)
        row["carried"] = 1 if carried else 0

        if carried:
            row["price"] = retail
            row["member_price"] = row.get("api_member_price")
            qty = row.get("api_stock") or 0
            row["available"] = 1 if qty > 0 else 0
            row["stock_text"] = "In Stock" if qty > 0 else "Sold Out"
        else:
            row["available"] = 0
            row["stock_text"] = "Not carried"

        return row
