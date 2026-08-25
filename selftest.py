"""Prove the app works, without touching your real data or the network.

    .venv\\Scripts\\python selftest.py
    CannaCabana.exe --selftest

Everything runs against a throwaway data directory seeded from the bundled
catalogue, so this is also exactly the fresh-install state a new machine sees:
products searchable, no index yet. Nothing here calls cannacabana.com -- the
index and live-check paths are exercised with stand-in jobs, because the real
ones cost 3 to 49 minutes and a slice of the rate limit.

It drives a real uvicorn on a loopback port with plain urllib rather than an
in-process test client. That needs no extra dependency in the packaged app,
and it exercises the actual server rather than a shim standing in for it.

Each step prints PASS or FAIL on its own line, and the exit code is the number
of failures, so a build script can gate on it.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

# Must happen before anything imports paths/config, or the test would run
# against the real history.db and settings.json.
_SANDBOX = os.environ.get("CANNACABANA_SELFTEST_DIR")
if not _SANDBOX:
    _SANDBOX = tempfile.mkdtemp(prefix="cannacabana-selftest-")
    os.environ["CANNACABANA_SELFTEST_DIR"] = _SANDBOX
os.environ["CANNACABANA_DATA"] = _SANDBOX

_results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        ok, note = fn()
    except Exception as e:                                        # noqa: BLE001
        ok, note = False, f"{type(e).__name__}: {e}"
    _results.append((name, bool(ok), note or ""))
    print(f"{len(_results):>3}. {name:<48} {'PASS' if ok else 'FAIL'}"
          + (f"   {note}" if note else ""), flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client:
    """Just enough HTTP: JSON in, JSON out, cookies remembered."""

    def __init__(self, base: str):
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body=None):
        data, headers = None, {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, (json.loads(raw) if raw else None)
            except json.JSONDecodeError:
                return e.code, None

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body=None):
        return self.call("POST", path, body)

    def raw(self, path) -> tuple[int, int]:
        """Status and byte count, for things that are not JSON."""
        with self.opener.open(self.base + path, timeout=30) as r:
            return r.status, len(r.read())


def main() -> int:
    print("=" * 74)
    print("  CannaScraper self-test")
    print("=" * 74)
    print(f"  sandbox: {_SANDBOX}")
    print()

    import auth
    import catalog
    import config
    import db
    import jobs
    import paths
    import server
    import stores as S
    import uvicorn

    port = free_port()
    conf = uvicorn.Config(server.app, host="127.0.0.1", port=port,
                          log_level="error")
    srv = uvicorn.Server(conf)
    web = threading.Thread(target=srv.run, daemon=True)
    web.start()
    deadline = time.time() + 20
    while time.time() < deadline and not srv.started and web.is_alive():
        time.sleep(0.05)
    c = Client(f"http://127.0.0.1:{port}")

    def t_server():
        return srv.started, f"listening on 127.0.0.1:{port}"

    def t_paths():
        ok = (os.path.isabs(config.DB_PATH)
              and str(paths.DATA_DIR) == _SANDBOX
              and config.DB_PATH.startswith(_SANDBOX))
        return ok, "writes land in the data dir, not the launch dir"

    def t_seed():
        ok = (os.path.exists(config.CATALOG_CACHE)
              and os.path.exists(config.STORES_CACHE))
        mb = os.path.getsize(config.CATALOG_CACHE) // 1024 // 1024 if ok else 0
        return ok, f"catalogue seeded ({mb} MB)"

    def t_catalog():
        cat = catalog.get_catalog(verbose=False)
        return len(cat) > 1000, f"{len(cat)} variants"

    def t_stores():
        allst = S.get_stores(province="")
        provs = {s["province"] for s in allst if s.get("province")}
        return len(allst) > 100 and len(provs) >= 5, \
            f"{len(allst)} stores, {len(provs)} provinces"

    def t_db():
        conn = db.connect()
        try:
            n = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        finally:
            conn.close()
        return n == 0, f"fresh database, {n} rows"

    def t_page():
        status, size = c.raw("/")
        return status == 200 and size > 5000, f"index.html served, {size} bytes"

    def t_status():
        s, d = c.get("/api/index/status")
        return (s == 200 and len(d["provinces"]) >= 5
                and all(p["indexed"] == 0 for p in d["provinces"])), \
            f"{len(d['provinces'])} provinces, none indexed yet"

    def t_open_read():
        s1, d1 = c.get("/api/search?q=grape&stocked_only=false")
        s2, _ = c.get("/api/provinces")
        return s1 == 200 and s2 == 200 and d1["total"] > 0, \
            f"{d1['total']} search matches with no login"

    def t_write_locked():
        a, _ = c.post("/api/index/Alberta")
        b, _ = c.post("/api/refresh?sku=12345")
        return a == 401 and b == 401, "index and refresh both 401"

    def t_bad_password():
        s, _ = c.post("/api/login", {"password": "definitely-wrong"})
        return s == 401, "rejected"

    def t_good_password():
        pw = auth.ensure_configured() or "?"
        s, _ = c.post("/api/login", {"password": pw})
        _, caps = c.get("/api/capabilities")
        return s == 200 and caps["admin"], "session cookie accepted"

    def t_write_unlocked():
        # An unknown province proves the request got past auth without
        # starting a real 45-minute build.
        s, _ = c.post("/api/index/Atlantis")
        return s == 404, "reached the handler, 404 not 401"

    def t_throttle():
        for _ in range(6):
            c.post("/api/login", {"password": "no"})
        s, _ = c.post("/api/login", {"password": "no"})
        auth._fails.clear()
        auth._global_fails[0], auth._global_fails[1] = 0, 0.0
        return s == 429, "locks out after repeated failures"

    def t_serialized():
        order = []
        jobs.echo = lambda *a, **k: None

        def body(name, secs):
            def run(job):
                order.append("start " + name)
                time.sleep(secs)
                job["done"] = job["total"] = 1
                order.append("end " + name)
            return run

        a = jobs._new("index", "A", 1, province="ZZ-one")
        jobs._enqueue(a, body("A", 0.30))
        b = jobs._new("index", "B", 1, province="ZZ-two")
        jobs._enqueue(b, body("B", 0.10))
        end = time.time() + 15
        while time.time() < end and not (jobs.get(a["id"])["finished"]
                                         and jobs.get(b["id"])["finished"]):
            time.sleep(0.05)
        return order == ["start A", "end A", "start B", "end B"], \
            "one at a time, in order"

    def t_busy_guard():
        j = jobs._new("index", "C", 1, province="ZZ-three")
        j["state"] = "running"
        try:
            jobs.submit_index("ZZ-three")
            return False, "allowed a duplicate"
        except jobs.Busy:
            return True, "second request refused"
        finally:
            j.update(state="done", finished=True, finished_at=time.time())

    def t_cancel():
        seen = {"stopped": False}

        def run(job):
            for _ in range(200):
                if job["cancel_requested"]:
                    seen["stopped"] = True
                    return
                time.sleep(0.02)

        j = jobs._new("index", "D", 1, province="ZZ-four")
        jobs._enqueue(j, run)
        end = time.time() + 8
        while time.time() < end and jobs.get(j["id"])["state"] != "running":
            time.sleep(0.02)
        jobs.cancel(j["id"])
        while time.time() < end and not jobs.get(j["id"])["finished"]:
            time.sleep(0.02)
        return seen["stopped"] and jobs.get(j["id"])["state"] == "cancelled", \
            "job saw the flag and stopped"

    def t_backend_honesty():
        _, caps = c.get("/api/capabilities")
        ok = ("browser" in caps["sources"]) == caps["playwright"]
        if caps["playwright"]:
            return ok, "playwright present, browser source offered"
        # Ask for the browser source on a SKU that really exists, so the
        # refusal comes from the missing backend and not from an unknown SKU.
        # It never reaches the network: the handler rejects it first.
        _, found = c.get("/api/search?q=grape&stocked_only=false&limit=1")
        sku = found["products"][0]["sku"]
        s, _ = c.post(f"/api/refresh?sku={sku}&fetcher=browser")
        return ok and s == 400, \
            f"playwright absent, browser source refused with {s}"

    def t_tunnel_binary():
        import tunnel
        found = tunnel.find_binary()
        # Not a failure: without it the app still serves local and LAN.
        return True, (f"cloudflared at {os.path.basename(found)}" if found
                      else "no cloudflared - local/LAN only, no public URL")

    try:
        check("http server starts", t_server)
        check("data paths are absolute and sandboxed", t_paths)
        check("bundled catalogue seeds into the data dir", t_seed)
        check("catalogue loads", t_catalog)
        check("store registry loads", t_stores)
        check("database opens clean on a fresh install", t_db)
        check("the page itself is served", t_page)
        check("index status lists every province", t_status)
        check("reads work with no password", t_open_read)
        check("writes are refused with no password", t_write_locked)
        check("wrong password is rejected", t_bad_password)
        check("correct password unlocks writes", t_good_password)
        check("unlocked write reaches the handler", t_write_unlocked)
        check("repeated bad passwords get throttled", t_throttle)
        check("job queue runs one job at a time", t_serialized)
        check("a second build per province is refused", t_busy_guard)
        check("cancel stops a running job", t_cancel)
        check("browser backend is reported honestly", t_backend_honesty)
        check("tunnel binary", t_tunnel_binary)
    finally:
        srv.should_exit = True
        web.join(timeout=5)

    failed = [r for r in _results if not r[1]]
    print()
    print("-" * 74)
    print(f"  {len(_results) - len(failed)} passed, {len(failed)} failed")
    for name, _, note in failed:
        print(f"    FAILED: {name}  {note}")
    print("-" * 74)
    return len(failed)


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        if os.environ.get("CANNACABANA_SELFTEST_KEEP") != "1":
            shutil.rmtree(_SANDBOX, ignore_errors=True)
    sys.exit(code)
