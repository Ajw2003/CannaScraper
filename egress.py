"""The egress pool: N independent routes to the site, each with its own budget.

Why this exists
---------------
An index run is rate-limit-bound, not latency-bound. `product/search` answers in
~0.74 s, well under the 1.2 s pacer interval, so the wall clock for a province
is set entirely by how many requests per minute we are allowed:

    92 stores x ~25 pages = ~2,300 requests
    at 50/min  ->  46 min        at the advertised 60/min ceiling  ->  38 min

That 38 minutes is a floor, and no amount of threading moves it, because the
budget is counted once for the whole client. `ratelimit_probe_summary.json`
pins down exactly how:

    A_unit_cost  median drop 1.0 per request      -- one request, one unit
    B_window     49 -> 59 at t=50s and t=105s     -- FIXED window, not rolling
    D_sharing    a search call dropped the SCAN counter 59 -> 46
                                                  -- ONE budget, both endpoints

So the only way to raise throughput is to hold more than one budget, and a
budget is (we believe) counted per source address. Each Egress below is one
such route: its own proxy, its own Pacer, its own health state. Give the pool
four working routes and the province takes ~12 minutes instead of ~46.

**"We believe" is doing real work in that sentence.** Nothing here proves the
counter is keyed on IP -- it could just as easily key on a fingerprint we also
send from every route, in which case the pool buys nothing and every worker
just burns the same 60/min faster. `probe()` settles it: it draws route 0's
budget down, then reads the others inside the same 60-second window, so a
shared counter is unmistakable. See `index_builder.py --probe-egress`. Run it
before trusting a multi-egress config, and read `verdict` rather than assuming.

On jitter
---------
Two kinds here, both for correctness rather than for looking human:

  * **Pacer jitter** spreads request starts so N workers coming out of a
    shared stall do not re-align on the same instant. It only ever makes the
    gap LONGER (`1 + uniform(0, jitter)`), never shorter, so the configured
    rate stays a hard ceiling -- costing ~6% throughput at the default 0.12.

  * **Retry jitter** is decorrelated backoff. Deterministic `15 * attempt`
    sleeps make every worker that hit the same 429 retry in lockstep and hit
    it again together; randomising the wait is what breaks that cycle.

What this module deliberately does not do: rotate user agents, vary header
order, or otherwise shape traffic to read as human. The pool is here to hold
more budget honestly, not to disguise what it is.
"""

from __future__ import annotations

import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config


class Pacer:
    """Minimum spacing between requests on ONE egress.

    Thread-safe, and the lock is held across the sleep on purpose: one egress
    is one serialized stream of requests, so a second caller must wait its
    turn rather than overlap.
    """

    def __init__(self, per_min: int, jitter: float = 0.0):
        self._interval = 60.0 / max(1, per_min)
        # Clamped: a jitter of 1.0 would double the interval and halve the
        # throughput, which is never what anyone means by "a bit of jitter".
        self._jitter = max(0.0, min(0.5, float(jitter or 0.0)))
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            interval = self._interval
            if self._jitter:
                # One-sided: never faster than the configured spacing, so the
                # rate ceiling holds no matter what the random draw is.
                interval *= 1.0 + random.uniform(0.0, self._jitter)
            gap = time.monotonic() - self._last
            if gap < interval:
                time.sleep(interval - gap)
            self._last = time.monotonic()


def backoff(attempt: int, base: float, cap: float = 90.0) -> float:
    """Decorrelated jittered backoff, in seconds.

    `base * 2**(attempt-1)` is the deterministic schedule this replaces. Two
    workers that hit the same 429 on that schedule wake at the same instant
    and collide again; drawing uniformly from [base, deterministic] spreads
    them out while keeping the same worst case.
    """
    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(base, max(base, ceiling))


class Egress:
    """One route to the site, with its own request budget and health.

    `name` is what shows up in progress lines, so keep it short.
    """

    def __init__(self, name: str, proxy: str | None, per_min: int,
                 jitter: float):
        self.name = name
        self.proxy = proxy or None
        self.pacer = Pacer(per_min, jitter)
        self.per_min = per_min

        # Health. A route that has started refusing is parked rather than
        # removed, so a flaky proxy recovers on its own instead of needing a
        # restart -- the same shape as SCAN_SKIP_RETRY_DAYS for stores.
        self.failures = 0          # consecutive
        self.total_failures = 0
        self.requests = 0
        self.cooldown_until = 0.0

        if self.proxy:
            handler = urllib.request.ProxyHandler(
                {"http": self.proxy, "https": self.proxy})
        else:
            # An EMPTY ProxyHandler, not the default one. The default reads
            # HTTP_PROXY/HTTPS_PROXY from the environment, which would quietly
            # route "direct" through whatever the machine happens to be
            # configured with -- and then two pool members share one address
            # while the console claims they are independent.
            handler = urllib.request.ProxyHandler({})
        self._opener = urllib.request.build_opener(handler)

    # --- health ------------------------------------------------------------

    def healthy(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.cooldown_until

    def note_ok(self) -> None:
        self.failures = 0
        self.requests += 1

    def note_failure(self) -> float:
        """Record a failure. Returns seconds of cooldown imposed (0 = none)."""
        self.failures += 1
        self.total_failures += 1
        self.requests += 1
        if self.failures < config.EGRESS_FAIL_LIMIT:
            return 0.0
        # Escalating, so a route that is properly dead stops being retried
        # every two minutes for the rest of a 45-minute run.
        rounds = self.failures - config.EGRESS_FAIL_LIMIT + 1
        secs = min(config.EGRESS_COOLDOWN_MAX_S,
                   config.EGRESS_COOLDOWN_S * rounds)
        self.cooldown_until = time.time() + secs
        return secs

    # --- transport ---------------------------------------------------------

    def open(self, req, timeout: float):
        self.pacer.wait()
        return self._opener.open(req, timeout=timeout)

    def open_unpaced(self, req, timeout: float):
        """Skip the pacer. Only probe() uses this -- see PROBE_SPACING_S.

        The probe has to fit every reading inside one 60-second window, and at
        50/min the pacer alone would stretch it past two. It sends ~15
        requests, so the allowance is in no danger.
        """
        return self._opener.open(req, timeout=timeout)

    def describe(self) -> str:
        if not self.proxy:
            return "direct"
        # Never print credentials: a proxy URL routinely carries user:pass and
        # this string goes to the console and to the web UI.
        try:
            p = urllib.parse.urlsplit(self.proxy)
            return f"{p.scheme}://{p.hostname}:{p.port or '?'}"
        except ValueError:
            return "proxy"

    def snapshot(self) -> dict:
        now = time.time()
        return {"name": self.name, "route": self.describe(),
                "per_min": self.per_min, "requests": self.requests,
                "failures": self.total_failures,
                "healthy": self.healthy(now),
                "cooldown_s": max(0.0, round(self.cooldown_until - now, 1))}


# --- building the pool -----------------------------------------------------

def _configured_proxies() -> list[str]:
    """Proxy URLs from config, plus any settings.json adds.

    settings.json is the packaged app's only way to gain a pool without a
    rebuild. Merged rather than replaced, matching how scan_skip_stores works.
    """
    out = [str(p).strip() for p in getattr(config, "EGRESS_PROXIES", []) or []]
    try:
        import auth
        extra = auth.load().get("egress_proxies") or []
        out += [str(p).strip() for p in extra]
    except Exception:                                             # noqa: BLE001
        # Settings are optional, and a malformed file must not stop an index
        # run that would have been perfectly fine going direct.
        pass

    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def build_pool(per_min: int | None = None,
               jitter: float | None = None) -> list[Egress]:
    """The routes an index run may use, direct first.

    Always returns at least one member: with no proxies configured this is a
    one-element pool holding the direct route, and every caller downstream
    takes the same code path it would with twelve.
    """
    per_min = int(per_min if per_min is not None else config.EGRESS_RATE_PER_MIN)
    jitter = float(jitter if jitter is not None else config.EGRESS_JITTER)

    pool: list[Egress] = []
    if config.EGRESS_INCLUDE_DIRECT:
        pool.append(Egress("direct", None, per_min, jitter))
    for i, proxy in enumerate(_configured_proxies(), 1):
        pool.append(Egress(f"p{i}", proxy, per_min, jitter))

    if not pool:
        # EGRESS_INCLUDE_DIRECT off with no proxies configured is a
        # misconfiguration, not a request to do nothing at all.
        pool.append(Egress("direct", None, per_min, jitter))
    return pool


def healthy(pool: list[Egress]) -> list[Egress]:
    now = time.time()
    return [e for e in pool if e.healthy(now)]


# --- proving the pool is worth having --------------------------------------

#: Requests spent drawing route 0's budget down before the other routes are
#: read. Large enough that a shared counter is unmistakable against the noise
#: in the per-request cost, small enough that the whole probe fits inside one
#: 60-second window and spends well under the 60-request allowance.
PROBE_DRAWDOWN = 10

#: The probe paces itself instead of using each route's Pacer, because every
#: reading MUST land inside ONE fixed 60-second window. A window reset midway
#: refills the counter, and the refilled reading looks exactly like an
#: independent budget -- so a slow probe would report success no matter what.
#: At 0.4s spacing, 12-15 requests take about six seconds.
PROBE_SPACING_S = 0.4


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def verdict_for(baseline, drawn, other, drawdown=PROBE_DRAWDOWN):
    """Did `other` see route 0's spending? Pure, so it can be tested offline.

    All three arguments are `X-RateLimit-Remaining` readings:

        baseline   route 0, before the draw-down
        drawn      route 0, after spending `drawdown` requests
        other      the route under test, read immediately afterwards

    A SHARED counter means `other` continues from `drawn` -- route 0's spending
    came out of its allowance too. An INDEPENDENT one means `other` is still up
    near `baseline`, untouched by anything route 0 did.

    This replaces a comparison that could not actually tell the two apart. The
    old version sent one request per route and called them shared when the
    readings were within (routes - 1) of each other -- but two *independent*
    budgets both answer near the top of their own window, so they land within
    1 of each other and were reported as shared. It gave the right answer for
    the shared case and quietly failed the case it existed to detect.

    Returns "shared", "independent", or "unknown".
    """
    if baseline is None or drawn is None or other is None:
        return "unknown"
    if baseline - drawn < drawdown * 0.5:
        # Route 0's own counter barely moved, so there is no draw-down to
        # compare against: either the window reset under us or the server
        # stopped counting the way the probe assumes. Refuse to guess.
        return "unknown"
    # Is `other` nearer the drawn-down level, or nearer the untouched baseline?
    return "shared" if other <= (drawn + baseline) / 2 else "independent"


def probe(pool: list[Egress], store_id: str, province: str,
          echo=print, drawdown: int = PROBE_DRAWDOWN) -> dict:
    """Do these routes hold separate rate-limit budgets? Measure, don't assume.

    The whole case for a multi-route pool rests on the answer, so this spends
    ~12-15 requests to get it properly rather than inferring it from a run that
    felt faster than the last one.

    Method: read route 0's remaining budget, spend `drawdown` requests on that
    route alone, then immediately read every other route. A shared counter
    comes back drawn down; an independent one comes back near full.

    One honest limitation: a route whose budget is already partly spent by
    somebody else -- a shared commercial proxy, say -- reads low and will be
    called shared. That is the safe direction to be wrong in, since it
    understates the pool rather than overselling it.
    """
    url = (config.API_BASE + "/product/search?"
           + urllib.parse.urlencode({"title": "a", "storeId": store_id,
                                     "limit": 50, "page": 1,
                                     "province": province}))

    def read(eg):
        """One unpaced request. Returns (status, limit, remaining, error)."""
        req = urllib.request.Request(
            url, headers={"User-Agent": config.USER_AGENT,
                          "Content-type": "application/json"})
        try:
            with eg.open_unpaced(req, config.API_TIMEOUT_S * 3) as r:
                r.read()
                return (r.status, _int(r.headers.get("X-RateLimit-Limit")),
                        _int(r.headers.get("X-RateLimit-Remaining")), "")
        except urllib.error.HTTPError as e:
            return (e.code, _int(e.headers.get("X-RateLimit-Limit")),
                    _int(e.headers.get("X-RateLimit-Remaining")), "")
        except Exception as e:                                    # noqa: BLE001
            return None, None, None, f"{type(e).__name__}: {e}"[:80]
        finally:
            time.sleep(PROBE_SPACING_S)

    rows = []
    echo(f"{'route':<10} {'via':<26} {'status':>6} {'limit':>6} "
         f"{'remaining':>10}  note")
    echo("-" * 78)

    def show(name, route, status, limit, remaining, note, error=""):
        rows.append({"name": name, "route": route, "status": status,
                     "limit": limit, "remaining": remaining, "note": note,
                     "error": error})
        echo(f"{name:<10} {route[:26]:<26} {str(status or '-'):>6} "
             f"{str(limit or '-'):>6} {str(remaining if remaining is not None else '-'):>10}"
             f"  {error or note}")

    first = pool[0]
    st, lim, baseline, err = read(first)
    show(first.name, first.describe(), st, lim, baseline, "baseline", err)

    if len(pool) < 2:
        working = 1 if st == 200 else 0
        return {"routes": rows, "working": working, "verdict": "unknown",
                "independent": False, "baseline": baseline, "drawn": None,
                "drawdown": 0, "speedup": 1}

    # Spend route 0's allowance, and nobody else's.
    drawn, st0, lim0 = baseline, st, lim
    for _ in range(drawdown):
        st0, lim0, drawn, _e = read(first)
    show(first.name, first.describe(), st0, lim0, drawn,
         f"after {drawdown} more requests on this route alone")

    tested = []
    for eg in pool[1:]:
        st, lim, rem, err = read(eg)
        v = verdict_for(baseline, drawn, rem, drawdown) if not err else "unknown"
        note = {"shared": "SHARED - saw route 0's spending",
                "independent": "independent - own budget",
                "unknown": "inconclusive"}[v]
        show(eg.name, eg.describe(), st, lim, rem, note, err)
        if st == 200 and not err:
            tested.append(v)

    if not tested:
        verdict = "unknown"
    elif all(v == "independent" for v in tested):
        verdict = "independent"
    elif all(v == "shared" for v in tested):
        verdict = "shared"
    elif any(v == "unknown" for v in tested):
        verdict = "unknown"
    else:
        verdict = "mixed"

    # Route 0 appears twice in `rows` (baseline and drawn), so count names.
    working = len({r["name"] for r in rows
                   if r["status"] == 200 and not r["error"]})
    return {"routes": rows, "working": working, "verdict": verdict,
            "independent": verdict == "independent",
            "baseline": baseline, "drawn": drawn, "drawdown": drawdown,
            "speedup": 1 + sum(1 for v in tested if v == "independent")}


def summarize_probe(res: dict) -> str:
    """One honest sentence about whether the pool actually bought anything."""
    n = res["working"]
    if n < 2:
        return (f"only {n} route(s) answered -- nothing to compare against. "
                f"Add a second route, or fix the failing ones, and probe again.")
    if res["verdict"] == "unknown":
        return (f"inconclusive: route 0 went {res['baseline']} -> "
                f"{res['drawn']} over {res['drawdown']} requests, which is not "
                f"the draw-down this test needs. Most likely the 60-second "
                f"window reset mid-probe -- just run it again.")
    if res["verdict"] == "independent":
        return (f"{n} routes, and every one holds its own budget. Expect "
                f"roughly {res['speedup']}x on an index run.")
    if res["verdict"] == "mixed":
        return (f"{n} routes, but only some hold their own budget -- expect "
                f"about {res['speedup']}x, not {n}x. The shared ones are "
                f"leaving the same address as route 0; check the table.")
    return (f"{n} routes answered but they all drew from route 0's budget. "
            f"The limit is NOT keyed on source address here, so the pool "
            f"will not raise throughput -- run direct.")


def main(argv=None) -> int:
    """`python egress.py` -- show the configured pool without touching it."""
    pool = build_pool()
    print(f"{len(pool)} route(s) configured, {config.EGRESS_RATE_PER_MIN}/min "
          f"each, jitter {config.EGRESS_JITTER:+.0%} one-sided")
    for eg in pool:
        print(f"  {eg.name:<10} {eg.describe()}")
    print()
    print("Combined ceiling: %d requests/min"
          % (len(pool) * config.EGRESS_RATE_PER_MIN))
    print("Prove it is real:  python index_builder.py --probe-egress")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
