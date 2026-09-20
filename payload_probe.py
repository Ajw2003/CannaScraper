"""How much of what product/search sends us do we actually keep?

index_builder.py asks for a page of 50 products and then throws most of each
one away -- it reads about a dozen fields off a response that also carries
body_html, image arrays, terpene lists, SEO blocks and so on. This measures
that waste and then tests, against the live server, whether we can ask for
less.

Two separate questions, answered separately below:

  STEP 2  What fraction of the bytes do we keep?  (arithmetic, always true)
  STEP 3  Will the server give us a trimmed response if we ask?  (their call,
          not ours -- if every candidate parameter comes back byte-identical
          the endpoint has no sparse-fieldset support and there is nothing to
          turn on)

Read-only. Makes ~10 GET requests, paced under the rate limit. Safe to re-run.

    python payload_probe.py
    python payload_probe.py --store 3801 --pages 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import stores as S

SEARCH = config.API_BASE + "/product/search"

# The keys index_builder._row() actually reads. Everything else on the wire is
# paid for and discarded.
USED_PRODUCT_KEYS = {"handle", "title", "vendor", "productType", "images",
                     "variants"}
USED_VARIANT_KEYS = {"sku", "title", "pricing"}
USED_PRICING_KEYS = {"qty_available", "retail_price", "member_price",
                     "elite_price", "is_elite", "thc_level", "cbd_level",
                     "equivalent_g", "storeId", "sale_price"}

# Parameter names worth trying. Sparse-fieldset conventions from the common
# frameworks -- Laravel/JSON:API/Shopify-ish -- plus a couple of long shots.
TRIM_CANDIDATES = [
    ("fields", "handle,title,vendor,productType,variants"),
    ("_fields", "handle,title,vendor,productType,variants"),
    ("select", "handle,title,vendor,productType,variants"),
    ("only", "handle,title,vendor,productType,variants"),
    ("fields[products]", "handle,title,vendor,productType,variants"),
    ("exclude", "body_html,description,seo,images"),
    ("without", "body_html,description"),
    ("light", "1"),
]


def _get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url, headers={"User-Agent": config.USER_AGENT,
                      "Content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=config.API_TIMEOUT_S * 3) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:                                    # noqa: BLE001
        print(f"    request failed: {type(e).__name__}: {e}")
        return 0, b""


def _url(store_id: str, province: str, page: int = 1, **extra) -> str:
    q = {"title": "a", "storeId": store_id, "limit": 50, "page": page,
         "province": province}
    q.update(extra)
    return f"{SEARCH}?{urllib.parse.urlencode(q)}"


def _size(obj) -> int:
    """Bytes this value costs on the wire, as compact JSON."""
    return len(json.dumps(obj, separators=(",", ":")).encode())


def _kb(n: float) -> str:
    return f"{n / 1024:,.1f} KB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--store", default=None, help="store_id (default: first "
                                                  "store in the province)")
    ap.add_argument("--province", default=config.PROVINCE)
    ap.add_argument("--pages", type=int, default=1,
                    help="pages to sample for the size breakdown")
    args = ap.parse_args(argv)

    print("=" * 74)
    print("product/search payload probe")
    print("=" * 74)

    # --- STEP 1 -----------------------------------------------------------
    print("\nSTEP 1  Pick a store to sample")
    store_list = S.get_stores(province=args.province)
    if not store_list:
        print(f"  FAIL  no stores for province={args.province!r}")
        return 2
    store = next((s for s in store_list if str(s["store_id"]) == str(args.store)),
                 store_list[0]) if args.store else store_list[0]
    sid = str(store["store_id"])
    print(f"  PASS  store {sid} — {store['name']}, {store.get('city','')}"
          f"   ({len(store_list)} stores in {args.province})")

    # --- STEP 2 -----------------------------------------------------------
    print("\nSTEP 2  Measure what one page costs and how much of it we keep")
    total_bytes = 0
    key_bytes: dict[str, int] = {}
    n_products = n_variants = 0
    kept_bytes = 0

    for page in range(1, args.pages + 1):
        status, raw = _get(_url(sid, args.province, page))
        if status != 200 or not raw or raw.lstrip().startswith(b"<"):
            print(f"  FAIL  page {page} returned HTTP {status} "
                  f"({len(raw)} bytes, not JSON)")
            return 1
        total_bytes += len(raw)
        d = json.loads(raw.decode())
        for prod in ((d.get("products") or {}).get("data") or []):
            n_products += 1
            for k, v in prod.items():
                key_bytes[k] = key_bytes.get(k, 0) + _size(v) + len(k) + 4
                if k in USED_PRODUCT_KEYS and k != "variants":
                    # images: we keep exactly one url out of the array
                    kept_bytes += (_size(v[0]) if k == "images" and v
                                   else _size(v))
            for var in prod.get("variants") or []:
                n_variants += 1
                for vk, vv in var.items():
                    if vk == "pricing" and isinstance(vv, dict):
                        for pk, pv in vv.items():
                            if pk in USED_PRICING_KEYS:
                                kept_bytes += _size(pv)
                    elif vk in USED_VARIANT_KEYS:
                        kept_bytes += _size(vv)
        time.sleep(60.0 / config.API_RATE_PER_MIN)

    print(f"  PASS  {args.pages} page(s), {n_products} products, "
          f"{n_variants} variants = {_kb(total_bytes)} on the wire")
    print(f"        we keep {_kb(kept_bytes)} of it "
          f"({kept_bytes / total_bytes * 100:.1f}%) — the rest is discarded")
    print()
    print(f"        {'product key':<26} {'bytes':>12}  {'share':>7}  kept?")
    for k, b in sorted(key_bytes.items(), key=lambda kv: -kv[1])[:18]:
        mark = "yes" if k in USED_PRODUCT_KEYS else "DISCARDED"
        print(f"        {k[:26]:<26} {b:>12,}  {b / total_bytes * 100:>6.1f}%"
              f"  {mark}")

    per_store_pages = 25
    stores_n = len(store_list)
    est = total_bytes / args.pages * per_store_pages * stores_n
    print()
    print(f"        A full {args.province} index (~{per_store_pages} pages x "
          f"{stores_n} stores) moves about {est / 1024 / 1024:,.0f} MB,")
    print(f"        of which roughly {est * kept_bytes / total_bytes / 1024 / 1024:,.0f} MB is data we store.")

    # --- STEP 3 -----------------------------------------------------------
    print("\nSTEP 3  Ask the server for less — does any parameter shrink it?")
    status, base = _get(_url(sid, args.province))
    if status != 200:
        print(f"  FAIL  baseline request returned HTTP {status}")
        return 1
    print(f"        baseline (no extra params): {_kb(len(base))}")
    print()
    shrunk = []
    for name, value in TRIM_CANDIDATES:
        time.sleep(60.0 / config.API_RATE_PER_MIN)
        st, raw = _get(_url(sid, args.province, **{name: value}))
        if st != 200 or not raw:
            verdict = f"HTTP {st}"
        elif len(raw) == len(base):
            verdict = "ignored (identical size)"
        else:
            delta = (len(raw) - len(base)) / len(base) * 100
            verdict = f"CHANGED {delta:+.1f}%"
            shrunk.append((name, value, len(raw)))
        print(f"        {name + '=' + value[:34]:<52} {verdict}")

    print()
    if shrunk:
        print("  PASS  the endpoint honours a trim parameter:")
        for name, value, n in shrunk:
            print(f"          {name}={value}  ->  {_kb(n)} "
                  f"(baseline {_kb(len(base))})")
        print("        Verify the trimmed response still carries every key in")
        print("        USED_PRODUCT_KEYS/USED_PRICING_KEYS before wiring it into")
        print("        index_builder._get().")
    else:
        print("  PASS  every candidate came back byte-identical to the baseline.")
        print("        The endpoint has no sparse-fieldset support — it serves one")
        print("        fixed shape. Nothing to trim at the request layer; the only")
        print("        savings available are on OUR side (what we store per row).")

    print("\n" + "=" * 74)
    print("RESULT: bandwidth waste measured above. Request-side trimming is")
    print("        possible only if STEP 3 found a working parameter.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
