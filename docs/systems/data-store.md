# Data store

## What it owns

The SQLite history (`history.db`): the append-only record of every observation ever scraped, the
two lookup tables it's normalized against, and every "what do we know right now" read path the
rest of the system (CLI, server, index builder, Pages export) uses.

## How it works

Physical tables are `obs` (one row per run × store × SKU), `products` (one row per SKU), and
`store_meta` (one row per store). `observations` is a **view**, not a table, joining the three
back into the original 34-column shape
([db.py:150-167](../../CannaScraper/db.py)) — every existing query, including a bare `SELECT *`,
keeps working unchanged across the schema split described in
[Decisions.md](../Decisions.md#2026-08-31--observations-table-split-into-products--store_meta--obs).

`write_rows()` ([db.py:225](../../CannaScraper/db.py)) is the **only** allowed write path: it
upserts `products`/`store_meta` using
`COALESCE(NULLIF(excluded.col,''), table.col)` so a blank field from one fetcher never overwrites
a good value the other fetcher already recorded, then `INSERT OR REPLACE`s into `obs` keyed on
`(run_id, store_id, sku)` — that key is what makes a rerun or `--resume` idempotent.

`latest_observations()` ([db.py:351](../../CannaScraper/db.py)) is the instant-answer API: the
most-recent-good row per `(sku, store_id)` across all runs. `server.py` and
`index_builder.py`'s `_close_out()` both read through it.

`connect()` ([db.py:199](../../CannaScraper/db.py)) enables WAL mode plus a busy timeout so the
web server can read while an index build is writing, and it **refuses to silently migrate** a
pre-split legacy database — a non-empty legacy schema raises `LegacySchema`
(db.py:172, db.py:208-218) rather than auto-converting, pointing at `normalize_db.py` instead.

## Invariants

- `thc`, `cbd`, and `default_price` stay **per-observation**, deliberately never promoted to
  `products` — 899 SKUs carry more than one genuine THC value because different stores hold
  differently-tested lots (db.py:26-30, 61-68). Collapsing them to the product level would destroy
  the evidence the potency-range fix (see
  [Decisions.md](../Decisions.md#2026-08-25--thccbd-potency-shown-as-a-consensus-range-not-max))
  depends on.
- `VIEW_COLUMNS`' order must match the **original physical table's** column order, not creation
  order, so CSV exports stay byte-comparable across the schema split (db.py:78-81).
- Nothing may write to `obs` except through `write_rows()` — writing directly lets `products` /
  `store_meta` drift out of step with it.
- Every "authoritative" read (`latest_observations`, `in_stock`, etc.) filters
  `COALESCE(store_id_match, 1) = 1` — a row where the site priced the wrong store is excluded by
  default, not merely flagged.

## Traps

- **db.py:14-24** documents the reason the split exists at all: the pre-split single table
  duplicated a product's title ~55× and a store's name ~1,300× — roughly 86 MB of a 128 MB file
  was pure duplication. Worth reading before "simplifying" back toward one table.
- **`connect()` explicitly refuses to auto-migrate** a non-empty legacy database
  (db.py:208-218). This is intentional friction, not an oversight — `normalize_db.py` backs up to
  `history.db.pre-normalize`, verifies every one of the original rows column-by-column, and only
  then drops the old table. Do not "fix" `connect()` to skip this check.
- `url` is not stored anywhere — it's rebuilt from `handle` + `store_id` at read time. This was
  verified to reproduce all 295,512 previously-stored URLs exactly before the column was dropped
  (see the README's "How it is stored" section). If a URL format ever needs a fourth component,
  this reconstruction is the thing to update, not a new stored column.
