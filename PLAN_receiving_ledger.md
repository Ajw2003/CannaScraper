# Shipment receiving → central stock ledger

## Context

Today the only way this project learns a stock number is by asking
cannacabana.com. `index_builder.py` sweeps a whole province through
`/api/product/search` and writes one `observations` row per (store, SKU) — 45
minutes for Alberta, ~49 for Ontario, ~276k rows in a 197 MB `history.db`. The
number is stale the moment it lands, and refreshing it costs a full re-scrape
against someone else's rate limit.

We now assume access to the operational data the chain already generates:
shipments received at the back door (scanned, keyed off a paper slip, or
exported from the POS) **and** the POS sales feed. That changes the problem
completely. With both the inflow and the outflow we can run a real perpetual
inventory ledger, where stock is *derived from events we own* rather than
*observed from a website*. Receiving a shipment becomes an instant write to the
central database, and the scrape stops being the source of truth and becomes a
**drift audit** — a periodic check that our ledger still matches the shelf.

Target: all 225 stores across 5 provinces. Storage stays SQLite for now but
every write goes through a repository seam so Postgres is a backend swap, not a
rewrite.

### One thing to flag before building

At 225 stores this stops being a scraper side-project and becomes production
inventory infrastructure for a regulated business. A packaged exe writing a
single SQLite file on a desktop behind a quick-tunnel is not where that should
live long term. The plan below is built to be correct and swappable, but it
assumes someone commits to **nightly off-machine backups and a named tunnel**
before real stores depend on it. That is an operational decision, not a coding
one, and it is called out again in Phase 5.

---

## Core idea

Three layers, each doing one job:

| Layer | Table | Role |
|---|---|---|
| **Events** | `stock_ledger` | Append-only signed deltas. Every receipt, sale, adjustment, transfer, count. Never updated, never deleted. The audit trail. |
| **State** | `stock_on_hand` | One row per (store_id, sku). The O(1) read. Materialized by applying deltas in the same transaction that appends them. |
| **Projection** | `observations` (existing) | A row written back into the table the whole UI already reads, so search, `/api/results` and `report.py` light up with **zero changes**. |

A shipment post is one transaction: append N ledger rows → upsert N on-hand
rows → replace N projection rows → invalidate the province cache. A 200-line
shipment is milliseconds, versus a 45-minute province scrape.

**Why event-sourced rather than just storing a quantity:** receiving happens on
a loading dock with bad wifi, from three different sources, sometimes twice.
Signed deltas with a unique natural key make a replayed shipment a no-op
instead of a double-count, make an offline scanner safe to sync whenever it
reconnects, and make "why does this say 14" answerable.

---

## Schema

Added to the `SCHEMA` string in [db.py:14](db.py:14) — it runs through
`executescript` on every `connect()`, so new `CREATE TABLE IF NOT EXISTS`
blocks are idempotent and need no migration machinery. Same database file: the
variance report joins ledger against `observations`, and keeping one file
avoids `ATTACH`.

```sql
-- Append-only. Never UPDATE, never DELETE.
CREATE TABLE IF NOT EXISTS stock_ledger (
  event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key    TEXT NOT NULL UNIQUE,  -- idempotency: 'recv:<shipment>:<line>'
  store_id     TEXT NOT NULL,
  sku          TEXT NOT NULL,
  delta        INTEGER NOT NULL,      -- signed: +receipt, -sale, ±adjust
  kind         TEXT NOT NULL,         -- receipt|sale|return|adjust|transfer|count|reconcile
  occurred_at  TEXT NOT NULL,         -- when it happened at the store
  recorded_at  TEXT NOT NULL,         -- when we learned about it (offline gap)
  ref_type     TEXT,                  -- shipment|pos_txn|count_session
  ref_id       TEXT,
  lot_code     TEXT,                  -- regulated traceability, cheap to capture
  packaged_on  TEXT,
  actor        TEXT,                  -- device/user that posted it
  note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_led_store_sku ON stock_ledger(store_id, sku, occurred_at);
CREATE INDEX IF NOT EXISTS idx_led_ref       ON stock_ledger(ref_type, ref_id);

-- The hot read. Bounded at 225 stores x ~5,320 SKUs.
CREATE TABLE IF NOT EXISTS stock_on_hand (
  store_id      TEXT NOT NULL,
  sku           TEXT NOT NULL,
  qty           INTEGER NOT NULL DEFAULT 0,
  updated_at    TEXT NOT NULL,
  last_event_id INTEGER,
  last_count_at TEXT,      -- last physical count / reconcile; confidence anchor
  PRIMARY KEY (store_id, sku)
);

-- Barcode / distributor / POS identifiers -> our sku. The unlock for scanning.
CREATE TABLE IF NOT EXISTS product_alias (
  alias_type  TEXT NOT NULL,   -- gtin|agcl_sku|ocs_item|pos_sku|vendor_sku|case_gtin
  alias_value TEXT NOT NULL,
  sku         TEXT NOT NULL,
  units       INTEGER DEFAULT 1,  -- case_gtin: units per case
  source      TEXT,               -- manifest|manual|inferred
  created_at  TEXT,
  PRIMARY KEY (alias_type, alias_value)
);
CREATE INDEX IF NOT EXISTS idx_alias_sku ON product_alias(sku);

-- Anything we scanned/received but could not map. NEVER silently dropped.
CREATE TABLE IF NOT EXISTS alias_quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alias_type TEXT, alias_value TEXT, store_id TEXT, shipment_id TEXT,
  qty INTEGER, seen_at TEXT, resolved_sku TEXT, resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS shipment (
  shipment_id  TEXT PRIMARY KEY,      -- '<store_id>-<vendor_ref>' or a uuid
  store_id     TEXT NOT NULL,
  province     TEXT,
  vendor       TEXT,
  vendor_ref   TEXT,                  -- ASN / packing slip number
  status       TEXT NOT NULL,         -- open|counting|posted|cancelled
  source       TEXT,                  -- manifest|blind|pos_export
  created_at   TEXT, posted_at TEXT, posted_by TEXT
);

CREATE TABLE IF NOT EXISTS shipment_line (
  shipment_id  TEXT NOT NULL,
  line_no      INTEGER NOT NULL,
  sku          TEXT,                  -- null until the alias resolves
  raw_alias    TEXT, alias_type TEXT,
  expected_qty INTEGER,               -- from the manifest; null on a blind receipt
  counted_qty  INTEGER,               -- what staff actually scanned/keyed
  lot_code     TEXT, packaged_on TEXT,
  PRIMARY KEY (shipment_id, line_no)
);

-- Per-store device identity. 225 stores cannot share one admin password.
CREATE TABLE IF NOT EXISTS store_credential (
  token_id   TEXT PRIMARY KEY,
  store_id   TEXT NOT NULL,
  token_hash TEXT NOT NULL,   -- scrypt, same primitives as auth.py
  token_salt TEXT NOT NULL,
  label      TEXT, created_at TEXT, revoked_at TEXT
);
```

---

## New code

A new `inventory/` package next to the existing flat modules. Stdlib only — no
new dependency, consistent with the deliberately tiny
[requirements.txt](requirements.txt).

| File | Contents |
|---|---|
| `inventory/repo.py` | **The Postgres seam.** Every statement the inventory code runs lives here as a named method (`append_events`, `apply_deltas`, `on_hand`, `upsert_alias`, …). Nothing else in `inventory/` writes SQL. Swapping backends means one new implementation of this class. |
| `inventory/ledger.py` | `post_events(conn, events)` — the single write path. One transaction: `INSERT OR IGNORE` on `event_key` (idempotency), then upsert `stock_on_hand`, then project. Returns applied vs skipped counts. |
| `inventory/identity.py` | `resolve(alias_type, value)` → sku, with fallback chain: `product_alias` → exact `sku` match in `catalog.json` → quarantine. Reuses [catalog.resolve_terms](catalog.py:145) for the human-search path in the mapping UI. |
| `inventory/receiving.py` | Shipment lifecycle: `create` (manifest import or blind), `add_count`, `variance`, `post`. Posting is what calls `ledger.post_events`. |
| `inventory/pos.py` | Sales ingest — `POST /api/pos/sales` batch, plus a poller for POS receiving/sales exports. Event key `pos:<store>:<txn>:<line>`. |
| `inventory/reconcile.py` | Drift: ledger `qty` vs scraped `api_stock` per (store, sku). Emits a report and, past a threshold, a `kind='reconcile'` event that snaps the ledger to observed truth — recorded as an event, so nothing is silently rewritten. |
| `inventory/projection.py` | Writes on-hand back into `observations` (below). |
| `inventory/routes.py` | A FastAPI `APIRouter` mounted from [server.py:37](server.py:37). |
| `web/receive.html`, `web/sw.js` | The scanner client (below). |

---

## The bridge: making the existing UI show ledger numbers with no rewrite

Every read query — [db.latest_observations](db.py:190),
[db.province_facts](db.py:322), [db.available_skus](db.py:362) — picks the
freshest row per (sku, store_id) by `MAX(scraped_at)`, filtered to
`status='ok'` and `COALESCE(store_id_match,1)=1`.

So `projection.py` writes an `observations` row with `run_id = f"live-{store_id}"`,
`scraped_at = now`, `api_stock = qty`, `available = 1 if qty else 0`,
`status='ok'`, `store_id_match=1`, reusing [index_builder._row](index_builder.py:95)'s
field mapping and sourcing the denormalized `store_name`/`city`/`province` from
`stores.json` and `title`/`brand`/`category`/`size`/`image` from `catalog.json`.
Because the PK is `(run_id, store_id, sku)` and the run_id is fixed per store,
`INSERT OR REPLACE` keeps exactly one live row per (store, sku) — bounded,
self-updating, and automatically the freshest fact. The web UI, `/api/results`
and `report.py` need no changes at all.

**Three specific guards this requires:**

1. [index_builder._close_out](index_builder.py:173) zeroes any previously-available
   SKU absent from a fresh scrape. At a ledger-enabled store that would clobber
   a true ledger quantity with a newer-timestamped 0. Guard it: skip
   (store, sku) pairs that have a `stock_on_hand` row. Reconciliation, not
   close-out, owns those.
2. [db.index_coverage](db.py:244) and [db.indexed_store_ids](db.py:306) filter
   `run_id LIKE 'index-%'`, so a store with only ledger data would render as
   **"not checked"** despite our having perfect data. Widen both to
   `run_id LIKE 'index-%' OR run_id LIKE 'live-%'`.
3. Call [server.invalidate_facts](server.py:75) after every post, or the
   120-second `_FACTS` cache makes a just-received shipment invisible. Mirror
   the existing `jobs.index_finished_hook` pattern ([jobs.py:37](jobs.py:37))
   rather than importing `server` into `inventory/`.

---

## Receiving flow, end to end

```
manifest CSV  ─┐
paper slip    ─┼─▶ shipment + shipment_line (expected_qty)
POS export    ─┘         │
                         ▼
              scan/key counts  ──▶  counted_qty per line
                         │
                         ▼
                 variance review (expected vs counted)
                         │
                    POST .../post
                         ▼
   ┌─── one transaction ─────────────────────────────┐
   │ stock_ledger   +qty, event_key recv:<ship>:<ln> │
   │ stock_on_hand  qty += delta                     │
   │ observations   live-<store> row, scraped_at=now │
   └─────────────────────────────────────────────────┘
                         │
                  invalidate_facts(province)
                         ▼
            visible chain-wide, immediately
```

Routes on the new router:

| Method | Route | Auth |
|---|---|---|
| POST | `/api/receiving/shipments` | store token — create, optionally with a manifest |
| POST | `/api/receiving/shipments/{id}/lines` | store token — scanned or keyed counts |
| GET | `/api/receiving/shipments/{id}/variance` | store token |
| POST | `/api/receiving/shipments/{id}/post` | store token — the write |
| GET | `/api/receiving/lookup?barcode=` | store token — alias → product, for the scanner |
| POST | `/api/pos/sales` | store token — batch sales events |
| GET/POST | `/api/inventory/aliases` | admin — the mapping queue |
| GET | `/api/inventory/drift?province=` | admin — ledger vs scrape |

Store tokens: issued per device, hashed with the existing scrypt helpers in
[auth.py:71](auth.py:71), verified by a new `require_store` dependency
alongside [auth.require_admin](auth.py:194). A store token may only write its
own `store_id` — enforced server-side, never trusted from the payload.

## Identity resolution — the part that actually decides whether this works

A receipt line arrives as a GTIN off a case, a distributor item number off a
manifest, or a POS SKU — **none of which is the chain's internal `sku`**, and
there is no barcode column anywhere in the current schema. `product_alias` is
the mapping, and it gets seeded three ways: bulk-imported from a distributor
catalogue if one exists, learned from POS exports (which carry both the POS
SKU and the internal SKU on the same line), and filled in by hand from the
quarantine queue.

Unmapped lines go to `alias_quarantine` and **block nothing** — the rest of the
shipment posts, the unmapped units are held, and an admin maps them once.
Mapping is retroactive: resolving a quarantine row replays it as a ledger
event. A dropped receipt line is invisible stock, which is the exact failure
this system exists to prevent.

## Offline scanner

Receiving happens at a back door with unreliable wifi, so `web/receive.html` is
a PWA: an alias cache and a pending-event queue in IndexedDB, a service worker,
and a sync-on-reconnect that POSTs the queue. Unique `event_key` is what makes
replaying that queue safe. Built the same way as the existing 26 KB
`web/index.html` — vanilla JS, no build step, no npm.

## Reconciliation — what the scraper becomes

Once the ledger is live, the province index is no longer how we learn stock.
It becomes the independent check on it. `reconcile.py` compares ledger `qty`
against scraped `api_stock` per (store, sku) and reports drift. Small drift is
shrink and miscounts; large or one-sided drift means a movement source is
missing. Past a threshold it writes a `kind='reconcile'` event snapping the
ledger to observed truth — as an event, never a silent overwrite. The scrape
cadence can then drop from nightly-full to a weekly audit, which is the actual
"stop re-scraping the API each time" win.

## Scale and the Postgres cutover

`stock_on_hand` stays small (~1.2M rows worst case). `stock_ledger` is what
grows: chain-wide POS at ~880 lines/store/day across 225 stores is roughly
200k events/day, ~72M/year. SQLite will hold that but a single desktop file is
the wrong home for it.

Concrete trigger: **SQLite is fine through the pilot and through
receiving-only rollout; move to Postgres before enabling chain-wide POS sales
ingest.** Because everything goes through `repo.py`, that is one new class.
Alongside it, compaction — keep 90 days of raw events, roll older ones into a
monthly `ledger_rollup(store_id, sku, month, received, sold, adjusted, closing_qty)`.

---

## Build order

| Phase | Deliverable |
|---|---|
| **1** | Schema + `repo.py` + `ledger.py` + `projection.py`. Prove one hand-written receipt event changes what the existing web UI shows. |
| **2** | `identity.py` + alias/quarantine + admin mapping UI. |
| **3** | `receiving.py` + routes + `web/receive.html` scanner. Manual entry first, scanning second, manifest import third. |
| **4** | Store credentials and `require_store`. |
| **5** | `pos.py` sales ingest. Backups and a named tunnel land here, before real stores depend on it. |
| **6** | `reconcile.py` + drift report; drop the scrape to a weekly audit. |
| **7** | Postgres backend in `repo.py` + ledger compaction. |

## Verification

Each phase ships with a runnable, committed script — consistent with
[selftest.py](selftest.py), which already runs 19 checks against a sandboxed
data dir via `CANNACABANA_SELFTEST_DIR` and no network. Extend it rather than
starting something new.

1. **Idempotency** — post the same 200-line shipment three times against a
   sandbox DB; assert `stock_on_hand.qty` moved exactly once and
   `stock_ledger` holds exactly 200 rows.
2. **Projection** — receive 5 units of a known SKU at a known store, then call
   `db.latest_observations(conn, skus=[sku], store_ids=[store])` and assert
   `api_stock == 5` and the row's `run_id` is `live-<store>`. Then hit
   `GET /api/results?sku=...` on a running server and assert the same number
   appears in the JSON.
3. **Close-out guard** — plant a ledger quantity, run `index_builder --limit 1`
   against that store with the SKU absent from the scrape, assert the ledger
   value survives and a drift entry is raised instead.
4. **Coverage** — a store with only ledger data must render as in-stock, not
   "not checked", after widening `indexed_store_ids`.
5. **Cache invalidation** — post a receipt, then assert `/api/search` reflects
   it in under a second rather than after the 120 s `_FACTS` TTL.
6. **Offline replay** — queue 50 events with the network down, reconnect,
   assert exactly 50 applied and a second sync applies 0.
7. **End to end, watchable** — a numbered script that creates a shipment, adds
   counts, prints the variance table, posts, and prints the before/after stock
   for each SKU with an explicit PASS/FAIL per step, run in the foreground.

## Assumptions

- Receiving data covers **all 225 stores**; until a given store is enabled it
  keeps using the scraped index, and the UI must distinguish the two.
- POS gives us both sales and receiving exports; the sales feed is what makes
  the ledger trustworthy through a trading day.
- The `sku` in `catalog.json` stays the canonical product key — everything else
  maps to it through `product_alias`.
- Off-machine backups and a named tunnel are in place before Phase 5.
