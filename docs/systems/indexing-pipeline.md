# Indexing pipeline

## What it owns

Building a whole-province in-stock snapshot cheaply (~2,300 calls, ~45 minutes on one route,
versus roughly 30 hours if it went one SKU at a time), and making that build durable, resumable,
and parallelizable across egress routes.

## How it works

`build_index()` ([index_builder.py:246](../../CannaScraper/index_builder.py)) seeds a
`work_queue` row per store (`workqueue.enqueue`, workqueue.py:82), reconciles it against what's
already in `obs` for `--resume` (`mark_done_from_history`, workqueue.py:103), resets any `claimed`
rows a dead process left behind (`reset_stuck`, workqueue.py:247 — safe because
[jobs.py](web-server-and-jobs.md) only ever runs one index job at a time), then spins one worker
thread per healthy [egress route](rate-limiting-and-egress.md), each looping on
`workqueue.claim()`.

`claim()` (workqueue.py:123) is a conditional `UPDATE ... WHERE state='pending' OR (state='claimed'
AND claimed_at < stale)`; whichever worker gets `rowcount == 1` wins the row. No `BEGIN IMMEDIATE`,
no external broker — SQLite's own writer serialization is the lock (workqueue.py:29-40 explains
why not Redis/Celery: this app ships as an exe someone double-clicks, and requiring a broker
daemon trades a working desktop tool for an ops problem).

`index_store()` (index_builder.py:188) pages `/api/product/search` until `hasNextPage` is false,
capped at `MAX_PAGES=200` as a runaway guard. The search endpoint returns **in-stock items only**,
so `_close_out()` (index_builder.py:213) is what actually marks a previously-in-stock SKU as
sold-out when it silently disappears from a fresh run's results.

## Invariants

- `_get()` **raises** `FetchError` rather than returning `None` or a short list on any failure
  (index_builder.py:79-84) — a partial fetch must never look like "no more pages," because
  `_close_out()` would then confidently mark everything unreached as sold-out. See
  [Decisions.md](../Decisions.md#2026-08-25--durable-sqlite-work-queue-for-province-indexing-not-rediscelery)
  for the incident this fixed.
- A store gets `WORK_MAX_ATTEMPTS` tries (`workqueue.fail`, workqueue.py:173) before being marked
  `failed`; `--resume` gives failed stores a fresh attempt budget via `requeue_failed`
  (workqueue.py:209).
- `release()` (workqueue.py:195) rolls back the attempt counter on a deliberate cancel — stopping
  a run must not itself burn a retry.
- Redelivery safety depends entirely on `db.write_rows()`'s `INSERT OR REPLACE` on
  `(run_id, store_id, sku)` (workqueue.py:38-40) — a store indexed twice by two racing workers
  just overwrites itself harmlessly.

## Traps

- **index_builder.py:16-17**: an explicit in-code warning to read `_close_out()`'s comment before
  changing anything nearby — the in-stock-only contract of the source endpoint is what makes
  close-out necessary in the first place, and it's easy to "fix" in a way that reintroduces stale
  rows.
- **index_builder.py:67-69**: `SKIP_TITLE_PATTERNS` (membership/renewal/gift-card SKUs) exist
  because those items report six-figure "stock" at all ~92 stores and wreck aggregates — a
  data-quality workaround for junk in the source data, not a bug in this codebase.
- **index_builder.py:432-437** (`_report_broken`): `config.SCAN_SKIP_STORES` should be derived
  from this report, not from noticing a run "felt slow." Store 528 reportedly went unnoticed for
  weeks this way, costing ~90s of backoff on every single run.
- The module docstring (index_builder.py:1-35) is worth reading before touching pacing at all —
  throughput here is rate-limit-bound, not latency-bound, so adding threads without adding egress
  routes buys nothing. See [rate-limiting-and-egress.md](rate-limiting-and-egress.md).
- `product/search` was chosen over the per-SKU `scan-multiple-items` endpoint for this exact
  purpose, after measuring both — see
  [Decisions.md](../Decisions.md#2026-08-25--productsearch-chosen-over-scan-multiple-items-for-province-indexing).
