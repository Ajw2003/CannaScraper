# Rate limiting & egress

## What it owns

Staying under the site's shared rate-limit budget on every path that talks to
`cannacabana.com`, and — optionally — holding more than one such budget at once via multiple
egress routes, so a province index can go faster than one client's budget otherwise allows.

## How it works

**`ratelimit.py`** is pure telemetry: `observe()` (ratelimit.py:102) reads the
`X-RateLimit-*` response headers on every call already being made — it never issues an extra
request — and `classify_429()` (ratelimit.py:85) tells a genuine budget exhaustion apart from a
store-specific refusal. History accumulates in `ratelimit.json` in the data directory; a 429
arriving with budget to spare is attributed to the store that produced it, so a broken store can't
masquerade as rate pressure.

**`egress.py`** is a pool of routes, each an `Egress` object (egress.py:105) wrapping either a
direct connection or a proxy, with its own `Pacer` (egress.py:64 — enforced minimum spacing plus
one-sided jitter) and its own health/cooldown state. `build_pool()` (egress.py:223) always returns
at least one route (the direct one). `index_builder.py` runs one worker thread per healthy route.

`probe()` / `verdict_for()` (egress.py:308, egress.py:275) empirically prove whether two routes
hold **independent** rate-limit budgets or a **shared** one: draw down route 0's budget by ten
requests, then immediately read every other route's remaining count, all inside one fixed 60-second
window (so a slower comparison can't straddle a window reset and produce a false independent
reading). Invoked via `python index_builder.py --probe-egress`.

## Invariants

- Pacer jitter is **one-sided**: `gap * (1 + uniform(0, jitter))` (egress.py:83-86) — it can only
  ever make a request wait *longer*, never sooner, so the configured rate stays a hard ceiling.
- `verdict_for()`'s shared-vs-independent comparison must use the **midpoint** between the
  baseline and drawn-down readings, not a fixed tolerance — see Traps.
- A route with no proxy configured uses an **empty** `ProxyHandler({})`
  (egress.py:126-135) — never the process's ambient `HTTP_PROXY`/`HTTPS_PROXY` environment, so a
  "direct" route can't accidentally inherit a system-wide proxy.
- The egress pool ships **empty by default** — nothing proves the site's rate-limit counter is
  IP-keyed rather than keyed on something every route sends; see
  [Decisions.md](../Decisions.md#2026-08-25--egress-pool-added-for-province-index-throughput-defaulting-to-empty).

## Traps

- **egress.py:288-293** documents a fixed bug in the probe itself: an earlier version compared
  readings within `routes - 1` of each other and called that "shared" — but two truly
  *independent* budgets both read near the top of their own window too, so they also land within
  1 of each other. That version would have silently talked you out of a working pool. The current
  `verdict_for()` uses the baseline/drawn midpoint specifically to avoid this.
- The module docstring at **egress.py:1-49** is effectively a lab notebook — measured numbers
  from `ratelimit_probe_summary.json` justify the whole pool design, with an explicit epistemic
  caveat: *"Nothing here proves the counter is keyed on IP."* Read it before changing anything
  about how routes are scored or how the pool decides a route is healthy.
- The measured limit itself (60/min, exactly 1 unit per request, fixed 60s window, shared across
  `product/search` and `scan-multiple-items`) came from `ratelimit_probe.py`, run live on
  2026-08-25 — see the README's "The limit, measured" section for the raw numbers. Re-running it
  is the way to check whether any of this has changed, not guessing from a slow run.
