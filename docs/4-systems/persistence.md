# Persistence (db.py)

## What it owns

The SQLite history database: schema, the `observations` view, and all writes (`db.py`,
696 lines).

## How it works

- Rows are written after **every store** completes a run, so a crash at store 80 of 92 never
  costs the first 79 (`db.py:3-4`, `README.md:818-819`).
- `observations` is a **view**, not a table. Physical tables:
  - `obs` — one row per `(run_id, store_id, sku)`, the volatile facts (295,512 rows as of the
    README's measurement) (`db.py:9-11`, `README.md:164`).
  - `products` — one row per SKU: `handle`, `title`, `brand`, `category`, `size`, `image`
    (5,322 rows) (`db.py:10`, `README.md:162`).
  - `store_meta` — one row per `store_id`: `store_name`, `city`, `province` (225 rows)
    (`db.py:10`, `README.md:163`).
- The split exists because the old single-table design stored every product/store fact on every
  row — a title written ~55× over, a store name ~1,300× — accounting for ~86 MB of a 128 MB
  file, i.e. taking the on-disk size from 203 MB to 97 MB (`db.py:13-16`, `README.md:166-168`).
- The view reproduces the old table's 34 columns in the old column order, so `SELECT *` (in
  `export_csv`) and `SELECT o.*` (in `latest_observations`) keep working unchanged
  (`db.py:17-21`, `README.md:169`).
- `url` is **not stored** — it's rebuilt from `handle` + `store_id`, verified to reproduce all
  295,512 stored URLs exactly (`db.py:21-22`, `README.md:171-172`).
- `thc`/`cbd` deliberately stayed per-observation, not moved to `products`: they're per-*lot*,
  not per-product — 899 SKUs carry more than one THC value because different stores hold
  differently-tested batches. Collapsing to one row/SKU would destroy that
  (`db.py:23-26`, `README.md:174-178`).
- All writes must go through `write_rows()`, which upserts `products`/`store_meta` and inserts
  the observation — nothing else may write `obs` directly or the lookup tables drift out of sync
  (`db.py:28-30`, `README.md:182-183`).
- A pre-split database is refused with instructions, not silently migrated. `normalize_db.py`
  backs up to `history.db.pre-normalize`, verifies all 34 columns of all rows against the
  original, then drops the old table. Rollback = delete the new file, rename the backup back
  (`README.md:185-194`).

## Invariants

- Nothing writes to `obs` directly — only `write_rows()` (`db.py:28-30`).
- `thc`/`cbd` stay per-observation, never collapsed to one value per SKU
  (`db.py:23-26`).
- The `observations` view's 34-column order must keep matching the old table's, since callers do
  `SELECT *` against it (`db.py:17-21`).

## Traps

- "Simplifying" by writing straight to `obs` — this breaks the `products`/`store_meta` upsert
  and the two lookup tables silently drift out of sync with the data (`db.py:28-30`).
- Assuming a database file predates the split is safe to open directly — it is refused rather
  than silently migrated; run `normalize_db.py` first (`README.md:185-194`).
- TODO: exact column list/types not enumerated here — read `db.py` schema definitions directly
  before adding a column (`PLAN_followups.md:26` references this as an open item area).
