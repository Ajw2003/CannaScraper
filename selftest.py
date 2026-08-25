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

    # --- the index fan-out ------------------------------------------------
    # Nothing below touches the network. The queue tests are pure SQLite, and
    # the end-to-end run swaps index_builder._get for a synthetic page server,
    # so a real 46-minute province build is never needed to prove the
    # machinery works.

    def _wq_conn():
        import workqueue
        conn = db.connect()
        workqueue.ensure(conn)
        return conn

    def _fake_stores(n, first=9000):
        return [{"store_id": str(first + i), "name": f"Store {i}",
                 "city": "Testville"} for i in range(n)]

    def t_queue_claim_once():
        """N workers racing over one queue must not fetch a store twice."""
        import workqueue
        run = "wq-race"
        conn = _wq_conn()
        try:
            workqueue.enqueue(conn, run, "ZZ", _fake_stores(40))
        finally:
            conn.close()

        claimed, errors = [], []
        grab = threading.Lock()

        def racer(name):
            c = db.connect()
            try:
                while True:
                    item = workqueue.claim(c, run, name)
                    if item is None:
                        return
                    with grab:
                        claimed.append(item["store_id"])
                    workqueue.complete(c, run, item["store_id"])
            except Exception as e:                                # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
            finally:
                c.close()

        ts = [threading.Thread(target=racer, args=(f"w{i}",)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)

        dupes = len(claimed) - len(set(claimed))
        return (not errors and len(claimed) == 40 and dupes == 0), \
            f"6 workers, 40 stores, {len(claimed)} claims, {dupes} duplicates"

    def t_queue_redelivery():
        """A claim whose lease expires goes back to another worker."""
        import workqueue
        run = "wq-lease"
        conn = _wq_conn()
        lease = config.WORK_LEASE_S
        try:
            workqueue.enqueue(conn, run, "ZZ", _fake_stores(1))
            first = workqueue.claim(conn, run, "worker-that-dies")
            # Still held: nobody else may take it.
            blocked = workqueue.claim(conn, run, "worker-b") is None
            # Now pretend the lease ran out.
            config.WORK_LEASE_S = -1
            second = workqueue.claim(conn, run, "worker-b")
        finally:
            config.WORK_LEASE_S = lease
            conn.close()
        ok = (first and blocked and second
              and second["store_id"] == first["store_id"])
        return ok, "held claim is exclusive, expired claim is redelivered"

    def t_queue_resume():
        """Re-enqueueing a run leaves finished stores finished."""
        import workqueue
        run = "wq-resume"
        conn = _wq_conn()
        try:
            stores = _fake_stores(10)
            workqueue.enqueue(conn, run, "ZZ", stores)
            for s in stores[:4]:
                workqueue.complete(conn, run, s["store_id"])
            # Exactly what build_index(resume=...) does on a second run.
            workqueue.enqueue(conn, run, "ZZ", stores)
            c = workqueue.counts(conn, run)
        finally:
            conn.close()
        return (c["done"] == 4 and c["pending"] == 6 and c["total"] == 10), \
            f"{c['done']} kept done, {c['pending']} still to do"

    def t_queue_retry_then_give_up():
        """A store is retried, then given up on, then revived by --resume."""
        import workqueue
        run = "wq-retry"
        conn = _wq_conn()
        try:
            workqueue.enqueue(conn, run, "ZZ", _fake_stores(1))
            sid = "9000"
            verdicts = []
            for _ in range(config.WORK_MAX_ATTEMPTS):
                workqueue.claim(conn, run, "w")
                verdicts.append(workqueue.fail(conn, run, sid, "boom"))
            gave_up = workqueue.counts(conn, run)["failed"] == 1
            revived = workqueue.requeue_failed(conn, run) == 1
            back = workqueue.counts(conn, run)["pending"] == 1
        finally:
            conn.close()
        ok = (verdicts[:-1] == [True] * (config.WORK_MAX_ATTEMPTS - 1)
              and verdicts[-1] is False and gave_up and revived and back)
        return ok, (f"retried {config.WORK_MAX_ATTEMPTS - 1}x, failed, "
                    f"then requeued by resume")

    def t_egress_pool():
        """The pool is built from config, and always has at least one route."""
        import egress
        was = config.EGRESS_PROXIES
        try:
            config.EGRESS_PROXIES = ["http://a.example:8080",
                                     "http://b.example:8080",
                                     "http://a.example:8080"]   # duplicate
            pool = egress.build_pool()
            names = [e.name for e in pool]
            direct_first = pool[0].proxy is None
        finally:
            config.EGRESS_PROXIES = was
        bare = egress.build_pool()
        return (len(pool) == 3 and direct_first and len(bare) == 1), \
            f"direct + 2 unique proxies = {names}, bare pool = 1 route"

    def t_egress_no_credential_leak():
        """A proxy URL's user:pass must never reach the console or the API."""
        import egress
        eg = egress.Egress("p1", "http://joe:hunter2@proxy.example:3128",
                           50, 0.0)
        shown = eg.describe() + json.dumps(eg.snapshot())
        return ("hunter2" not in shown and "joe" not in shown
                and "proxy.example" in shown), \
            f"describe() = {eg.describe()}"

    def t_egress_cooldown():
        """A failing route parks itself, and comes back when it expires."""
        import egress
        eg = egress.Egress("p1", None, 50, 0.0)
        for _ in range(config.EGRESS_FAIL_LIMIT):
            secs = eg.note_failure()
        parked = not eg.healthy() and secs > 0
        eg.cooldown_until = time.time() - 1        # pretend it expired
        recovered = eg.healthy()
        eg.note_ok()
        return (parked and recovered and eg.failures == 0), \
            f"parked for {secs:.0f}s after {config.EGRESS_FAIL_LIMIT} failures"

    def t_pacer_ceiling():
        """Jitter must never let a route exceed its configured rate."""
        import egress
        # 600/min = 100ms apart. Ten calls therefore cannot finish in under
        # 0.9s no matter which way the jitter lands, because jitter is
        # one-sided and only ever makes the gap longer.
        p = egress.Pacer(600, jitter=0.5)
        t0 = time.monotonic()
        for _ in range(10):
            p.wait()
        elapsed = time.monotonic() - t0
        return elapsed >= 0.9, f"10 calls at 600/min took {elapsed:.2f}s (>=0.90)"

    def t_backoff_jitter():
        """Retry waits are spread, not identical, and stay within bounds."""
        import egress
        draws = [egress.backoff(3, 2.0) for _ in range(50)]
        spread = len(set(round(d, 4) for d in draws)) > 40
        bounded = all(2.0 <= d <= 8.0 for d in draws)
        return (spread and bounded), \
            f"50 draws in [{min(draws):.2f}, {max(draws):.2f}], all distinct"

    def t_index_fanout():
        """End to end: 3 routes drain the queue, and every store lands.

        index_builder._get is replaced, so this exercises the real queue, the
        real workers, the real _close_out and the real database write --
        everything except the one function that would touch the network.
        """
        import egress
        import index_builder as IB
        import workqueue

        province = "ZZ-fanout"
        n_stores = 9
        fake = _fake_stores(n_stores, first=9500)
        for s in fake:
            s["province"] = province

        # Real pages take ~0.74s. Without some latency here the first worker
        # drains all nine items before the other two threads are scheduled,
        # and the test would "pass" on a serial run -- so PAGE_MS stands in
        # for the network and makes the speedup measurable.
        PAGE_MS = 0.03

        def fake_get(eg, term, store_id, page, prov):
            # Two pages per store, three products each, so pagination and the
            # per-store dedupe both get exercised.
            time.sleep(PAGE_MS)
            data = []
            for i in range(3):
                sku = f"{store_id}-{page}-{i}"
                data.append({
                    "title": f"Product {sku}", "handle": f"p-{sku}",
                    "vendor": "TestCo", "productType": "Flower", "images": [],
                    "variants": [{"sku": sku, "title": "1g",
                                  "pricing": {"retail_price": 10.0,
                                              "member_price": 9.0,
                                              "qty_available": 5}}],
                })
            return {"products": {"data": data,
                                 "pagination": {"hasNextPage": page < 2}}}

        real_get, real_stores = IB._get, IB.S.get_stores
        seen_workers = set()
        lock = threading.Lock()

        def watched_get(eg, *a, **kw):
            with lock:
                seen_workers.add(eg.name)
            return fake_get(eg, *a, **kw)

        try:
            IB._get = watched_get
            IB.S.get_stores = lambda province="", limit=None: (
                fake[:limit] if limit else fake)
            # Three routes, paced fast enough that the test is not a wait.
            pool = [egress.Egress(f"r{i}", None, 6000, 0.0) for i in range(3)]
            clock = time.monotonic()
            res = IB.build_index(province, pool=pool, workers=3)
            elapsed = time.monotonic() - clock
        finally:
            IB._get = real_get
            IB.S.get_stores = real_stores

        conn = db.connect()
        try:
            counts = workqueue.counts(conn, res["run_id"])
            rows = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE run_id=?",
                (res["run_id"],)).fetchone()[0]
            stores_seen = conn.execute(
                "SELECT COUNT(DISTINCT store_id) FROM observations "
                "WHERE run_id=?", (res["run_id"],)).fetchone()[0]
        finally:
            conn.close()

        # What one route would have taken: every page, back to back.
        serial = n_stores * 2 * PAGE_MS
        # Three routes should land near a third of that. 0.6 leaves room for
        # thread scheduling without being loose enough to pass a serial run.
        parallel = elapsed < serial * 0.6

        ok = (res["completed"] == n_stores and res["failed"] == 0
              and counts["done"] == n_stores and counts["pending"] == 0
              and stores_seen == n_stores and rows == n_stores * 6
              and len(seen_workers) == 3 and parallel)
        return ok, (f"{n_stores} stores, {rows} rows, "
                    f"{len(seen_workers)}/3 routes worked, "
                    f"{elapsed:.2f}s vs {serial:.2f}s serial "
                    f"({serial / elapsed:.1f}x)")

    def t_index_partial_is_not_success():
        """A mid-pagination failure must fail the store, not truncate it.

        This is the regression that matters most: the old _get returned None
        on error and the caller read that as "no more pages", so a store would
        be recorded with only the products fetched before the hiccup -- and
        _close_out would then mark everything it never reached as sold out.
        """
        import egress
        import index_builder as IB

        def flaky_get(eg, term, store_id, page, prov):
            if page == 2:
                raise IB.FetchError("simulated mid-pagination failure")
            return {"products": {
                "data": [{"title": "P", "handle": "p", "vendor": "v",
                          "productType": "t", "images": [],
                          "variants": [{"sku": "S1", "title": "1g",
                                        "pricing": {"retail_price": 1.0,
                                                    "qty_available": 1}}]}],
                "pagination": {"hasNextPage": True}}}

        real = IB._get
        try:
            IB._get = flaky_get
            eg = egress.Egress("r0", None, 6000, 0.0)
            try:
                IB.index_store(eg, {"store_id": "9999", "name": "X",
                                    "city": "Y", "province": "ZZ"}, "ZZ")
                return False, "returned a partial store instead of raising"
            except IB.FetchError:
                return True, "raises, so the store is retried not truncated"
        finally:
            IB._get = real

    # --- the egress probe's verdict ---------------------------------------
    # The probe is the only thing standing between "we added proxies" and
    # "the proxies actually do something", so its verdict has to be right in
    # both directions. These run against stub routes with known budgets --
    # no request leaves this machine.

    def t_verdict_table():
        """verdict_for() on readings whose right answer is known."""
        import egress
        cases = [
            # (baseline, drawn, other, expected, why)
            (59, 48, 47, "shared",
             "other continued from the drawn-down level"),
            (59, 48, 58, "independent",
             "other still near its own full budget"),
            # The regression this replaced: two INDEPENDENT routes both answer
            # near the top of their window, so the old spread test called them
            # shared and would have talked you out of a working pool.
            (59, 48, 59, "independent", "both near full, but route 0 was drawn"),
            # Route 0 barely moved: the window almost certainly reset, and any
            # comparison against it is meaningless.
            (59, 58, 59, "unknown", "no draw-down to compare against"),
            (59, 59, 12, "unknown", "no draw-down, however low the other reads"),
            (None, 48, 47, "unknown", "a missing header is not a verdict"),
            (59, 48, None, "unknown", "a missing header is not a verdict"),
            # Right at the midpoint, ties go to shared -- understating the
            # pool is the safe direction to be wrong in.
            (60, 50, 55, "shared", "exactly halfway counts as shared"),
        ]
        bad = []
        for baseline, drawn, other, want, why in cases:
            got = egress.verdict_for(baseline, drawn, other, 10)
            if got != want:
                bad.append(f"({baseline},{drawn},{other}) -> {got}, want {want}")
        return not bad, (f"{len(cases)} readings classified correctly"
                         if not bad else "; ".join(bad))

    class _Budget:
        """A rate-limit counter. Routes that share one share an allowance."""

        def __init__(self, start=60):
            self.left = start

        def take(self):
            self.left -= 1
            return self.left

    class _FakeResp:
        def __init__(self, remaining):
            self.status = 200
            self.headers = {"X-RateLimit-Limit": "60",
                            "X-RateLimit-Remaining": str(remaining)}

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeEgress:
        """Stands in for an Egress with a known budget. No sockets."""

        def __init__(self, name, budget):
            self.name, self._budget = name, budget

        def describe(self):
            return "stub"

        def open_unpaced(self, req, timeout):
            return _FakeResp(self._budget.take())

    def _probe_stub(pool):
        """Run the real probe over stub routes, with the spacing removed."""
        import egress
        was = egress.PROBE_SPACING_S
        try:
            egress.PROBE_SPACING_S = 0.0
            return egress.probe(pool, "0000", "ZZ", echo=lambda *a, **k: None)
        finally:
            egress.PROBE_SPACING_S = was

    def t_probe_detects_shared():
        """Three routes behind one counter must be reported as shared."""
        import egress
        one = _Budget()
        pool = [_FakeEgress(f"r{i}", one) for i in range(3)]
        res = _probe_stub(pool)
        return (res["verdict"] == "shared" and not res["independent"]
                and res["speedup"] == 1), \
            f"verdict={res['verdict']}, speedup={res['speedup']}x, " \
            f"{egress.summarize_probe(res)[:44]}..."

    def t_probe_detects_independent():
        """Three routes with their own counters must be reported independent.

        This is the case the old one-request-per-route probe got wrong.
        """
        import egress
        pool = [_FakeEgress(f"r{i}", _Budget()) for i in range(3)]
        res = _probe_stub(pool)
        return (res["verdict"] == "independent" and res["independent"]
                and res["speedup"] == 3), \
            f"verdict={res['verdict']}, speedup={res['speedup']}x"

    def t_probe_detects_mixed():
        """One good proxy behind a shared route must not read as a clean win."""
        shared = _Budget()
        pool = [_FakeEgress("r0", shared), _FakeEgress("r1", shared),
                _FakeEgress("r2", _Budget())]
        res = _probe_stub(pool)
        return (res["verdict"] == "mixed" and res["speedup"] == 2), \
            f"verdict={res['verdict']}, speedup={res['speedup']}x (not 3)"

    def t_probe_single_route_is_honest():
        """One route cannot answer the question, and must not pretend to."""
        import egress
        res = _probe_stub([_FakeEgress("r0", _Budget())])
        text = egress.summarize_probe(res)
        return (res["verdict"] == "unknown" and not res["independent"]
                and "nothing to compare" in text), \
            "reports unknown rather than guessing"

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
        # The index fan-out. Offline: no request leaves this machine.
        check("queue claims each store exactly once", t_queue_claim_once)
        check("a dead worker's claim is redelivered", t_queue_redelivery)
        check("resume keeps finished stores finished", t_queue_resume)
        check("a store retries, then is given up on", t_queue_retry_then_give_up)
        check("egress pool is built from config", t_egress_pool)
        check("proxy credentials never get printed", t_egress_no_credential_leak)
        check("a failing route parks and recovers", t_egress_cooldown)
        check("jitter never exceeds the rate ceiling", t_pacer_ceiling)
        check("retry backoff is spread, not lockstep", t_backoff_jitter)
        check("3 routes drain the queue end to end", t_index_fanout)
        check("a partial store fails instead of truncating",
              t_index_partial_is_not_success)
        check("probe verdict is right on known readings", t_verdict_table)
        check("probe detects a shared budget", t_probe_detects_shared)
        check("probe detects independent budgets", t_probe_detects_independent)
        check("probe detects a mixed pool", t_probe_detects_mixed)
        check("probe admits when one route can't tell",
              t_probe_single_route_is_honest)
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
