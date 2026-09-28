# Fetchers, rate limiting & egress

## What it owns

Getting per-store price/stock data out of cannacabana.com without exceeding its rate limit:
`fetchers/` (browser + API backends), `ratelimit.py` (budget tracking), `egress.py` (multi-route
pool + pacing).

## How it works

**Two backends** (`fetchers/api_fetcher.py`, 290 lines; `fetchers/browser_fetcher.py`, 48
lines), selected via `config.FETCHER` or `--fetcher`:

- **Browser** (Playwright/Chromium) — necessary because per-store pricing is applied
  client-side: requesting a product page with `?sID=<store_id>` returns byte-identical HTML for
  every store until JS runs (`README.md:66-73`). Slower: ~18 min for 92 stores × 1 product
  (`README.md:469-471`).
- **API** (default) — calls the same `scan-single-item`/`scan-multiple-items` endpoints the
  site's own page calls, reading the reply instead of throwing it away; unauthenticated
  (`README.md:85-89, 473-478`). One call carries the whole watchlist for a store, so cost scales
  with stores, not stores × products (`README.md:473-475`). ~3.4 min for 92 stores × 1 product
  (`README.md:469-471`).

**The `store_id_match` hazard.** In delivery mode, the site prices 36 Calgary-area "hub" stores
as their hub rather than themselves (`HUB_STORE_MAP`, `README.md:118-134`), so multiple stores
report an identical price/stock. `config.AGE_GATE_STATE` sets
`age_verification_delivery = "false"` to force pickup mode, where every store reports its own
shelf. `store_id_match=0` on an `ok` row is the signal this has regressed
(`README.md:136-137`).

**Rate limiting** (`ratelimit.py`): the site advertises `X-RateLimit-Limit: 60`/min
(`ratelimit.py:1-19`). Measured empirically by `ratelimit_probe.py` (297 requests,
2026-08-25): cost is exactly 1 unit/request, the window is fixed at 60s (snaps back at a
wall-clock boundary, does not trickle), and the budget is **shared** across
`product/search` and `scan-multiple-items` (`README.md:628-647`, `ratelimit.py:9-19`).
`config.API_RATE_PER_MIN = 50` paces starts to stay under that (`README.md:481`).
`ratelimit.py` records the low-water mark per run by reading response headers already received
— no extra requests (`ratelimit.py:1-6`).

**Concurrency**: the live-check path runs 6 concurrent API calls (`API_CONCURRENCY`), measured
against the 60/min budget with headroom to spare (`README.md:483-495`). The index-build path
(`product/search`, ~0.74s/call) is genuinely rate-limit-bound, so threading a single route does
not help — only holding more than one budget does (`README.md:497-517`).

**Egress pool** (`egress.py`): a pool of N independent routes to the site, each with its own
proxy, `Pacer` and health state (`egress.py:1-30`, `Pacer.__init__` at `egress.py:72`,
`Egress.__init__` at `egress.py:111`). Configured via `config.EGRESS_PROXIES` or a packaged
copy's `settings.json` `"egress_proxies"` list, merged (`egress.py:205`, `README.md:563-567`).
**Empty by default** — deliberately, because nothing proves the site's counter is keyed on
source IP; `index_builder.py --probe-egress` / `egress.verdict_for()` settle that empirically
per deployment rather than assuming it (`README.md:519-561`). `jobs.py` runs at most one job at
a time system-wide, precisely because the rate budget is shared and a second concurrent job
would breach one 60/min allowance rather than getting its own (`README.md:642-644`,
`jobs.py:1-9`).

**Jitter**: pacer jitter (`EGRESS_JITTER`, default 0.12) only ever lengthens the gap between
requests (one-sided), keeping the configured rate a hard ceiling; retry backoff is separately
jittered so workers hitting the same 429 don't retry in lockstep. Neither rotates user agents or
otherwise disguises traffic (`README.md:569-575`).

## Invariants

- Never fetch product pages with plain `requests`/BeautifulSoup — pricing is client-rendered and
  this produces one repeated price for every store with no visible error
  (`README.md:71-73`).
- Pickup mode (not delivery) must stay forced via `AGE_GATE_STATE`, or hub stores silently share
  one price/stock reading (`README.md:130-137`).
- Only one job (index or live) runs at a time — the rate budget is shared, not per-job
  (`jobs.py:1-9`, `README.md:642-644`).
- Egress pool routes must be proven independent (via `verdict_for()`) before being trusted to
  add throughput — an unproven pool can silently "just spend the same 60/min faster"
  (`README.md:519-533`).

## Traps

- `api_fetcher._call()` used to treat a normal "Bag Changed" response as a hard failure, and
  separately misread `missingItems` (variant IDs) against SKU-keyed data — both fixed
  2026-08-25, verify with `python verify_scan_fix.py` (`PLAN_followups.md:9-42`).
- An earlier egress-verdict probe couldn't distinguish a shared budget from two independent
  budgets that both happened to read near-full — `egress.verdict_for()` replaced it with a pure
  function and a truth table in `selftest.py` (`README.md:555-561`).
- Store 528 (Gateway Village) returns HTTP 500 from the API and is reported as an error rather
  than guessed at — not a bug to "fix" by suppressing the error (`README.md:673-675`).
