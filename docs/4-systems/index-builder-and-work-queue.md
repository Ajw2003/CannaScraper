# Index builder & work queue

## What it owns

Building a whole-province stock index (every in-stock product at every store) durably, with
multiple workers over the egress pool: `index_builder.py` (615 lines), `workqueue.py`
(277 lines).

## How it works

- Uses `/api/product/search?title=a&storeId=<id>`, which returns a store's entire in-stock
  catalogue 50/page (~25 calls/store) with price tiers, exact quantity, gram equivalence, and
  THC/CBD (`README.md:374-376`, `index_builder.py:1-8`). Paged `product/search` takes ~45 min
  for Alberta vs. ~30 hours for the per-SKU `scan-multiple-items` route
  (`README.md:378-383`, `index_builder.py:8`).
- Verified exhaustive before being trusted: 11 search terms surfaced nothing beyond `title='a'`,
  and 148 catalogue variants absent from results were independently confirmed
  not-in-stock via `scan-multiple-items` (`index_builder.py:11-14`, `README.md:439-444`).
- **Every store is a durable row** in a `work_queue` table (`workqueue.py`), claimed atomically
  by workers (`README.md:579-581`, `index_builder.py:19-21`). `claim()` (`workqueue.py:123`) is
  a conditional `UPDATE` that re-checks row state; the winner is whoever gets `rowcount == 1` —
  no `BEGIN IMMEDIATE`, no `RETURNING` (`workqueue.py:29-32`, `README.md:595-598`).
- A claim carries a lease (`WORK_LEASE_S`); a worker that dies mid-store has its item redelivered
  after the lease expires. Redelivery is safe because `db.write_rows()` is `INSERT OR REPLACE`
  keyed on `(run_id, store_id, sku)` (`workqueue.py:34-37`, `README.md:582-584, 597-598`).
- Failed stores retry up to `WORK_MAX_ATTEMPTS` rather than being silently dropped
  (`README.md:583-584`).
- Each worker owns one route from the egress pool (see fetchers doc); with the pool empty
  (default), this degenerates to the old sequential loop plus crash-durability and per-store
  retries (`index_builder.py:26-28`).
- `--resume <run_id>` and a crash-restart are the same code path, because all state is in the
  queue table, not in memory (`workqueue.py:22-23`, `README.md:...`).
- `_close_out()` zeroes anything that was in-stock at a store last run and is now absent from the
  response, because the index endpoint only returns in-stock items — a sold-out product simply
  vanishes rather than appearing with qty 0 (`README.md:446-451`).

## Invariants

- A store that fails partway through pagination must go back in the queue as failed, **not** be
  recorded as a partial success. `_get()` used to return `None` on error, which `index_store()`
  read as "no more pages," producing a store that `_close_out()` then marked as fully sold out —
  fixed so `_get()` raises instead (`README.md:600-605`).
- `_close_out()` must run every index cycle, or `db.latest_observations()` keeps serving a stale
  in-stock row as current fact (`README.md:446-451`).
- Only one index/live job runs at a time system-wide (owned by `jobs.py`, see fetchers doc) —
  the work queue does not itself enforce this.

## Traps

- Treating a `None` return from an internal fetch as "nothing more to page through" instead of
  "something failed" — this exact bug produced silently-partial stores before the fix described
  above (`README.md:600-605`).
- The index cannot distinguish "never carried" from "carried but sold out" — for that,
  `--fetcher api` on a specific product is still needed (`README.md:453-455`).
- Assuming more worker threads always means more throughput: only true if the egress pool has
  independently-budgeted routes; with the default single/direct route, threading buys nothing
  because the endpoint itself is rate-limit-bound (`README.md:497-501`).
