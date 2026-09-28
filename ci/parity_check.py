"""Compare the old server (server.py) and the static site (site/static-api.js)
answering identical requests over the Saskatchewan history DB.

    # old server running:
    #   CANNACABANA_DATA=<tmp>/appdata python3 -m uvicorn server:app --port 8950
    # static site running:
    #   python3 -m http.server 8951   (in a dir holding site/index.html,
    #   site/static-api.js and data/<exports>)

    python3 ci/parity_check.py --old http://127.0.0.1:8950 \
        --static http://127.0.0.1:8951 [--headed]

Drives the static site's page with headless Chromium (Playwright) so
static-api.js's window.fetch wrapper answers exactly as it would for a real
visitor, then calls the same endpoint on the old server with urllib and
diffs the two JSON bodies field by field.

Ignored deliberately, per the plan:
  - age_hours-shaped fields: compared rounded to 0.1h (they are inherently
    time-dependent -- the two servers computed "now" a few seconds apart).
  - `rate` / `egress` on /api/index/status: server-process-only telemetry
    with no static equivalent.

stdlib + playwright only (playwright is a dev dependency of this check, not
of the site itself).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

# CHROME_PATH overrides which Chromium binary Playwright launches. Unset, we
# fall back to the sandbox's pre-fetched build if present, else Playwright's
# own bundled Chromium (no executable_path -- `playwright install` handles it).
_SANDBOX_CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
CHROME = os.environ.get("CHROME_PATH") or (
    _SANDBOX_CHROME if os.path.exists(_SANDBOX_CHROME) else None
)


def old_get(base: str, path: str) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, headers={"User-Agent": "parity-check"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # noqa: F821 (urllib.error imported below)
        return e.code, json.loads(e.read().decode())


import urllib.error  # noqa: E402  (after old_get's use, kept together with urllib imports)


ROUND_H_KEYS = {"age_hours", "index_age_hours"}


def normalize(d, path=""):
    """Round every *_hours-ish field to 0.1 so a few seconds' drift doesn't
    count as a mismatch, and drop keys that are inherently server-process-only
    telemetry with no static equivalent."""
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            if k in ("rate", "egress"):
                continue
            # Added to search results on the Pages site (approved
            # 2026-09-28, docs/plans/restore-original-page.md); the old
            # server has no equivalent to compare against.
            if path.endswith(".products") and k in ("stores", "price_from"):
                continue
            if k in ROUND_H_KEYS and isinstance(v, (int, float)):
                out[k] = round(v, 1)
            else:
                out[k] = normalize(v, f"{path}.{k}")
        return out
    if isinstance(d, list):
        return [normalize(v, path) for v in d]
    return d


def diff(a, b, path="$"):
    """Yield human-readable mismatches between two normalized JSON values."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                yield f"{path}.{k}: missing on old side"
            elif k not in b:
                yield f"{path}.{k}: missing on static side"
            else:
                yield from diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            yield f"{path}: length {len(a)} (old) vs {len(b)} (static)"
        for i, (x, y) in enumerate(zip(a, b)):
            yield from diff(x, y, f"{path}[{i}]")
    else:
        if a != b:
            yield f"{path}: {a!r} (old) vs {b!r} (static)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="http://127.0.0.1:8950")
    ap.add_argument("--static", default="http://127.0.0.1:8951")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            **({"executable_path": CHROME} if CHROME else {}), headless=not args.headed
        )
        page = browser.new_page()
        page.goto(args.static + "/index.html")
        page.wait_for_timeout(300)   # let static-api.js install its fetch wrapper

        def static_get(path: str):
            return page.evaluate(
                """async (path) => {
                    const r = await fetch(path);
                    let body;
                    try { body = await r.json(); } catch (e) { body = null; }
                    return {status: r.status, body};
                }""",
                path,
            )

        compared, mismatches = 0, []

        def check(label: str, path: str):
            nonlocal compared
            compared += 1
            os_, ob = old_get(args.old, path)
            sr = static_get(path)
            ss, sb = sr["status"], sr["body"]
            if os_ != ss:
                mismatches.append(f"[{label}] status: {os_} (old) vs {ss} (static)  {path}")
                return
            na, nb = normalize(ob), normalize(sb)
            ds = list(diff(na, nb))
            if ds:
                mismatches.append(f"[{label}] {path}\n    " + "\n    ".join(ds[:20])
                                   + (f"\n    ... and {len(ds) - 20} more" if len(ds) > 20 else ""))

        # /api/provinces
        check("provinces", "/api/provinces")

        # /api/categories
        for stocked in ("true", "false"):
            q = urllib.parse.urlencode({"province": "Saskatchewan", "stocked_only": stocked})
            check(f"categories stocked_only={stocked}", f"/api/categories?{q}")

        # /api/search
        terms = ["blue dream", "gummies", "vape", "pre-roll", "indica", "3.5 g",
                  "edibles", "cartridge", "sativa"]
        for term in terms:
            for stocked in ("true", "false"):
                q = urllib.parse.urlencode({"q": term, "province": "Saskatchewan",
                                            "stocked_only": stocked})
                check(f"search {term!r} stocked_only={stocked}", f"/api/search?{q}")
            q = urllib.parse.urlencode({"q": term, "province": "Saskatchewan",
                                        "category": "Flower"})
            check(f"search {term!r} category=Flower", f"/api/search?{q}")

        # /api/results
        skus = ["109473", "101614", "110085"]  # 109473, one ELITE, one mixed-stock
        near_values = [None, "saskatoon, sk", "52.13,-106.67"]
        for sku in skus:
            for top_kind in (("top", "10"), ("all_stores", "true")):
                for sort in ("distance", "stock"):
                    for near in near_values:
                        params = {"sku": sku, "province": "Saskatchewan", "sort": sort}
                        params[top_kind[0]] = top_kind[1]
                        if near:
                            params["near"] = near
                        q = urllib.parse.urlencode(params)
                        label = f"results sku={sku} {top_kind} sort={sort} near={near}"
                        check(label, f"/api/results?{q}")

        # /api/index/status
        check("index/status", "/api/index/status")

        browser.close()

    print(f"Compared {compared} requests.")
    if mismatches:
        print(f"\n{len(mismatches)} mismatch(es):\n")
        for m in mismatches:
            print(m, "\n")
        return 1
    print("No mismatches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
