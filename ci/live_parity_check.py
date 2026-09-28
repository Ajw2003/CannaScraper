"""Compare the live check ("Check live now") in the old app and the static site.

ci/parity_check.py covers everything the index answers. This covers the one
path that talks to cannacabana.com at request time: POST /api/refresh, polled
through /api/job/<id>, then /api/results showing the fresh numbers.

Neither side may reach the real site here, so both are pointed at the same
deterministic stub of app.cannacabana.com's scan endpoint:

  - old app: server.py started in a subprocess with config.API_SCAN pointed at
    the stub and the admin check overridden (live checks are admin-only there);
  - static site: headless Chromium with every request to app.cannacabana.com
    answered by the same stub function (page.route), so static-api.js runs its
    real code path, including the cross-site POST.

The stub answers in the shape the real endpoint uses (see
fetchers/api_fetcher.py:104-118 and BUILD_INSTRUCTIONS.md, "Read the response
body too"): positional CSV per SKU in `scanned-items`, `elitePrices`, and
`missingItems` with message "Bag Changed" when a SKU isn't carried.

    python3 ci/live_parity_check.py --data <dir holding history.db etc.> \\
        --static http://127.0.0.1:8951

The old app is started on --old-port (default 8952) against a COPY of the
history DB, because a live check writes rows into it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

# CHROME_PATH overrides which Chromium binary Playwright launches. Unset, we
# fall back to the sandbox's pre-fetched build if present, else Playwright's
# own bundled Chromium (no executable_path -- `playwright install` handles it).
_SANDBOX_CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
CHROME = os.environ.get("CHROME_PATH") or (
    _SANDBOX_CHROME if os.path.exists(_SANDBOX_CHROME) else None
)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from parity_check import diff, normalize  # noqa: E402  (same field-by-field comparison)

# Error paths, exercised on both sides identically: one store answers 500
# twice and then succeeds (api_fetcher._call retries 5xx), one always answers
# 500 (the check gives up, the store keeps its previous data, the job reports
# an error). Counters are kept per side so both see the same sequence.
FAIL_TWICE = {"3163"}      # Tisdale
FAIL_ALWAYS = {"3152"}     # Swift Current
_calls: dict = {}


def stub_status(side: str, store_id: str, body: dict) -> int:
    key = (side, store_id, json.dumps(body, sort_keys=True))
    _calls[key] = _calls.get(key, 0) + 1
    if store_id in FAIL_ALWAYS:
        return 500
    if store_id in FAIL_TWICE and _calls[key] <= 2:
        return 500
    return 200


SKUS = ["109473", "101614", "110085", "100689"]   # plain, ELITE-tier, mixed stock, THC from catalogue text
PROVINCE = "Saskatchewan"


def stub_answer(store_id: str, body: dict) -> dict:
    """Deterministic scan answer for one store: same input, same output."""
    scanned, elite, missing = {}, {}, []
    for item in body.get("skus", []):
        for sku, vid in item.items():
            h = int(hashlib.sha256(f"{store_id}:{sku}".encode()).hexdigest(), 16)
            if h % 4 == 0:
                missing.append(vid)
                continue
            retail = 10 + h % 50 + 0.99
            member = 0.0 if h % 3 == 0 else round(retail * 0.8, 2)
            scanned[sku] = f"{h % 20},{member:.2f},{retail:.2f},each,98.10,0.60,3.50,588:{sku}"
            if h % 5 == 0:
                elite[sku] = f"{retail * 0.85:.2f}"
    return {"data": {"scanned-items": scanned or [], "elitePrices": elite or [],
                     "payload": {}, "missingItems": missing},
            "message": "Bag Changed" if missing else "Ok",
            "success": not missing}


def start_stub(port: int):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            store = self.path.rstrip("/").split("/")[-1]
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            status = stub_status("old", store, body)
            out = json.dumps(stub_answer(store, body) if status == 200 else {"error": "stub"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_old_app(data_dir: Path, port: int, stub_port: int) -> subprocess.Popen:
    code = f"""
import config
config.API_SCAN = "http://127.0.0.1:{stub_port}/api/product/scan-multiple-items/{{store_id}}"
import auth, jobs, server, uvicorn
server.app.dependency_overrides[auth.require_admin] = lambda: True
jobs.start()
uvicorn.run(server.app, host="127.0.0.1", port={port}, log_level="warning")
"""
    env = {"CANNACABANA_DATA": str(data_dir), "PATH": "/usr/bin:/bin:/usr/local/bin",
           "PYTHONPATH": str(REPO)}
    p = subprocess.Popen([sys.executable, "-c", code], cwd=REPO, env=env)
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/provinces", timeout=2)
            return p
        except OSError:
            time.sleep(0.5)
    p.kill()
    raise SystemExit("old app did not start")


def old_live(base: str, sku: str) -> dict:
    q = urllib.parse.urlencode({"sku": sku, "province": PROVINCE, "all_stores": "true",
                                "fetcher": "api"})
    req = urllib.request.Request(f"{base}/api/refresh?{q}", method="POST", data=b"")
    job = json.load(urllib.request.urlopen(req, timeout=30))["job"]
    for _ in range(600):
        s = json.load(urllib.request.urlopen(f"{base}/api/job/{job}", timeout=30))
        if s.get("finished"):
            break
        time.sleep(0.5)
    q = urllib.parse.urlencode({"sku": sku, "province": PROVINCE, "all_stores": "true"})
    return {"job": s, "results": json.load(urllib.request.urlopen(f"{base}/api/results?{q}", timeout=30))}


STATIC_JS = """
async (sku) => {
  const q = new URLSearchParams({sku, province: 'Saskatchewan', all_stores: 'true', fetcher: 'api'});
  const {job} = await (await fetch('/api/refresh?' + q, {method: 'POST'})).json();
  let s;
  for (let i = 0; i < 600; i++) {
    s = await (await fetch('/api/job/' + job)).json();
    if (s.finished) break;
    await new Promise(r => setTimeout(r, 500));
  }
  const r = new URLSearchParams({sku, province: 'Saskatchewan', all_stores: 'true'});
  return {job: s, results: await (await fetch('/api/results?' + r)).json()};
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="dir with history.db, catalog.json, stores.json, geocode.json")
    ap.add_argument("--static", required=True)
    ap.add_argument("--old-port", type=int, default=8952)
    ap.add_argument("--stub-port", type=int, default=8960)
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="live-parity-"))
    for f in Path(args.data).iterdir():
        if f.is_file():
            shutil.copy(f, work / f.name)

    stub = start_stub(args.stub_port)
    old = start_old_app(work, args.old_port, args.stub_port)
    mismatches = 0
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(**({"executable_path": CHROME} if CHROME else {}))
            pg = b.new_page()
            seen = []

            def route(r):
                req = r.request
                if req.method == "OPTIONS":
                    return r.fulfill(status=204, headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "POST",
                        "Access-Control-Allow-Headers": "content-type"})
                store = req.url.rstrip("/").split("/")[-1]
                seen.append(store)
                payload = json.loads(req.post_data or "{}")
                status = stub_status("static", store, payload)
                body = stub_answer(store, payload) if status == 200 else {"error": "stub"}
                r.fulfill(status=status, body=json.dumps(body), headers={
                    "Content-Type": "application/json", "Access-Control-Allow-Origin": "*"})

            pg.route("https://app.cannacabana.com/**", route)
            pg.goto(args.static)
            pg.wait_for_timeout(1500)
            for sku in SKUS:
                o = old_live(f"http://127.0.0.1:{args.old_port}", sku)
                n = pg.evaluate(STATIC_JS, sku)
                # Approved change (2026-09-28): the static page reports a
                # failed store on the job, where the original ended "Done.".
                # So the static side must report an error exactly when a store
                # in scope always fails; the old side reports none.
                expect_err = bool(FAIL_ALWAYS)
                if bool(n["job"].get("error")) != expect_err or o["job"].get("error"):
                    mismatches += 1
                    print("  job error not as expected (static should report the failing store, old should not)")
                print(f"sku {sku}: old job done={o['job'].get('done')}/{o['job'].get('total')} "
                      f"error={o['job'].get('error')!r} | static job done={n['job'].get('done')}/"
                      f"{n['job'].get('total')} error={n['job'].get('error')!r}")
                d = list(diff(normalize(o["results"]), normalize(n["results"])))
                if d:
                    mismatches += 1
                    print(f"  {len(d)} field difference(s) in /api/results:")
                    for line in d[:12]:
                        print("   ", line)
                else:
                    print("  /api/results identical after the live check")
            print(f"static page sent {len(seen)} scan request(s) to app.cannacabana.com (stubbed)")
            b.close()
    finally:
        old.kill()
        stub.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print(f"{len(SKUS)} live checks compared, {mismatches} with differences.")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
