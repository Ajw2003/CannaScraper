"""Does cannacabana.com answer requests from this machine's IP address?

Built for GitHub Actions (.github/workflows/runner-ip-test.yml): runners sit on
Microsoft data-centre addresses, which some sites refuse. Before building a
scheduled scrape on Actions, this checks each endpoint the scraper depends on,
using the scraper's own User-Agent so the answer applies to the real code.

Prints one line per endpoint and exits 1 if a required one is not a clean 200.
The store locator is reported but not required: a scrape reads the committed
stores.json and only fetches the locator to refresh the store list.
Runs anywhere with Python 3.11 and no third-party packages:

    python ci/runner_ip_probe.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402  (needs the repo root on sys.path first)

# Headers worth seeing when the answer is not a 200: who answered (a CDN or
# bot wall rather than the app), and how much rate-limit budget is left.
REVEALING_HEADERS = ("server", "cf-ray", "cf-mitigated", "x-ratelimit-limit",
                     "x-ratelimit-remaining", "retry-after", "content-type")

TIMEOUT_S = 30


def public_ip() -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=TIMEOUT_S) as r:
            return r.read().decode().strip()
    except (urllib.error.URLError, OSError) as e:
        return f"unknown ({e})"


def first_store_id(province: str) -> str:
    stores = json.loads(Path(config.STORES_CACHE).read_text(encoding="utf-8"))
    for store in stores:
        if store["province"] == province:
            return str(store["store_id"])
    raise SystemExit(f"No {province} store in {config.STORES_CACHE}")


def probe(name: str, url: str) -> bool:
    """GET `url` and report what came back. True only for a 200 with a body."""
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            status, headers, body = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, headers, body = e.code, e.headers, e.read()
    except (urllib.error.URLError, OSError) as e:
        print(f"FAIL  {name:<12} no response: {e}")
        return False

    seen = {h: headers.get(h) for h in REVEALING_HEADERS if headers.get(h)}
    is_ok = status == 200 and len(body) > 0
    print(f"{'OK  ' if is_ok else 'FAIL'}  {name:<12} HTTP {status}, {len(body)} bytes  {seen}")
    if not is_ok:
        print(f"      first 300 bytes: {body[:300]!r}")
    return is_ok


def main() -> int:
    province = os.environ.get("PROBE_PROVINCE", "Saskatchewan")
    store_id = first_store_id(province)
    search = urllib.parse.urlencode({"title": "a", "storeId": store_id, "limit": 5,
                                     "page": 1, "province": province})

    print(f"Public IP: {public_ip()}")
    print(f"Probing as User-Agent: {config.USER_AGENT}")
    required = [
        probe("catalog", f"{config.PRODUCTS_JSON}?limit=1&page=1"),
        probe("stock-api", f"{config.API_BASE}/product/search?{search}"),
    ]
    if not probe("locator", config.LOCATOR_URL):
        print("WARN  locator is optional: scrapes use the committed stores.json, "
              "but refreshing the store list from this runner would fail.")
    passed = sum(required)
    print(f"{passed}/{len(required)} required endpoints answered normally.")
    return 0 if passed == len(required) else 1


if __name__ == "__main__":
    sys.exit(main())
