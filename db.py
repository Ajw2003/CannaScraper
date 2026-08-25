"""Persistence: append-only SQLite history + a latest-snapshot CSV.

Rows are written after every store completes, so a crash at store 80 of 92
never costs the first 79.

Storage shape -- read this before writing a query
-------------------------------------------------
`observations` is a VIEW, not a table. The physical tables are:

    obs         one row per (run_id, store_id, sku) -- the volatile facts
    products    one row per sku      -- title, brand, image, ...
    store_meta  one row per store_id -- name, city, province

The old single table stored every product and store fact on every row. With
5,322 distinct SKUs and 225 stores across 295k rows, a product's title was
written ~55 times over and a store's name ~1,300 times: ~86 MB of the 128 MB
of content was duplication.

The view exists so that split costs nothing at the call site. It reproduces the
old table's 34 columns, in the old table's physical column order, so every
existing SELECT -- including `SELECT *` in export_csv and `SELECT o.*` in
latest_observations -- keeps working unchanged. `url` is not stored at all; it
is recomputed from handle + store_id, which was verified to reproduce all
295,512 stored urls exactly.

What deliberately did NOT move to `products`: **thc and cbd**. They look like
product attributes and the old code assumed they were, but 899 SKUs carry more
than one distinct THC value -- real per-lot potency, since stores hold
different tested batches. Collapsing them to one row per SKU would have
destroyed that. See PLAN_followups.md #2.

Writes go through write_rows() only, which upserts the two lookup tables and
then inserts the observation. Nothing else may write to `obs` directly, or the
lookups will drift out of sync with it.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

import config

# --- the split -------------------------------------------------------------
# One row per sku. Measured across all 295,512 rows, the number of SKUs (of
# 5,322) carrying more than one distinct non-empty value:
#
#     brand 0   size 0   category 1   handle 1   image 2   title 3
#
# Those are products that were renamed or re-photographed over time. Collapsing
# them keeps the most recent value and loses the older one, which is the right
# trade for a tool that answers "who has this in stock now" -- the SKU, not the
# title, is the identity.
PRODUCT_COLUMNS = ["handle", "title", "brand", "category", "size", "image"]

# One row per store_id. Verified: zero stores disagree on any of these.
STORE_COLUMNS = ["store_name", "city", "province"]

# Everything genuinely per-observation.
#
# thc/cbd are here on purpose -- they are per-lot, not per-product (899 SKUs
# carry more than one THC value). See the module docstring.
#
# default_price is here for the same class of reason: it is a PRICE, and prices
# are the one thing this database exists to track over time. It is NULL in
# 293,058 of 295,512 rows, so keeping it per-observation costs almost nothing,
# while making it product-level would rewrite every historical row with today's
# list price.
OBS_COLUMNS = [
    "run_id", "scraped_at", "store_id", "sku",
    "price", "member_price", "default_price", "available", "carried",
    "stock_text", "thc", "cbd", "store_label", "status", "error",
    "api_store_id", "store_id_match", "api_stock", "api_price",
    "api_elite_price", "api_equiv_g", "api_raw", "api_member_price",
    "is_elite",
]

# The view's column order. This is the ORIGINAL TABLE's physical order, not the
# order of the CREATE TABLE that used to be here -- migrated columns had been
# appended to the end over time. Matching the physical order keeps results.csv
# byte-comparable with files exported before the split.
VIEW_COLUMNS = [
    "run_id", "scraped_at", "store_id", "store_name", "city", "province",
    "sku", "handle", "title", "brand", "category", "size", "url",
    "price", "member_price", "default_price", "available", "carried",
    "stock_text", "thc", "cbd", "store_label", "status", "error",
    "api_store_id", "store_id_match", "api_stock", "api_price",
    "api_elite_price", "api_equiv_g", "api_raw", "api_member_price",
    "is_elite", "image",
]

# Kept as the full set of fields a row dict may carry, for callers building one.
COLUMNS = VIEW_COLUMNS

# Tables and view are kept separate because normalize_db.py needs the tables
# without the view: during migration the old physical `observations` table
# still owns that name. Splitting a combined string on ";" is not an option --
# several column comments below contain one.
SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS products (
  sku           TEXT PRIMARY KEY,
  handle        TEXT,
  title         TEXT,
  brand         TEXT,
  category      TEXT,
  size          TEXT,
  image         TEXT     -- Shopify CDN url; resize with ?width=N
);

CREATE TABLE IF NOT EXISTS store_meta (
  store_id   TEXT PRIMARY KEY,
  store_name TEXT,
  city       TEXT,
  province   TEXT
);

CREATE TABLE IF NOT EXISTS obs (
  run_id        TEXT NOT NULL,
  scraped_at    TEXT,
  store_id      TEXT NOT NULL,
  sku           TEXT NOT NULL,
  price         REAL,
  member_price  REAL,
  default_price TEXT,
  available     INTEGER,
  carried       INTEGER,
  stock_text    TEXT,
  thc           TEXT,     -- per-LOT, not per-product; see module docstring
  cbd           TEXT,     -- per-LOT, not per-product; see module docstring
  store_label   TEXT,
  status        TEXT,
  error         TEXT,
  api_store_id   TEXT,     -- store the site actually priced against
  store_id_match INTEGER,  -- 0 = priced as a DIFFERENT store; treat as suspect
  api_stock       INTEGER, -- exact unit count (DOM only says "In Stock")
  api_price       REAL,    -- authoritative retail price
  api_elite_price REAL,    -- ELITE price, never rendered in the DOM
  api_equiv_g     REAL,    -- gram equivalence toward the 30 g limit
  api_raw         TEXT,    -- full positional CSV, for fields not yet decoded
  api_member_price REAL,   -- member price straight from the pricing call
  is_elite        INTEGER, -- 1 = ELITE-tier product (member price N/A)
  PRIMARY KEY (run_id, store_id, sku)
);

CREATE INDEX IF NOT EXISTS idx_sku_time  ON obs(sku, scraped_at);
CREATE INDEX IF NOT EXISTS idx_run       ON obs(run_id);
CREATE INDEX IF NOT EXISTS idx_store_sku ON obs(store_id, sku);
"""

SCHEMA_VIEW = f"""
CREATE VIEW IF NOT EXISTS observations AS
SELECT
  o.run_id, o.scraped_at, o.store_id,
  s.store_name, s.city, s.province,
  o.sku, p.handle, p.title, p.brand, p.category, p.size,
  '{config.BASE}/products/' || COALESCE(p.handle, '') || '?sID=' || o.store_id
    AS url,
  o.price, o.member_price, o.default_price,
  o.available, o.carried, o.stock_text,
  o.thc, o.cbd, o.store_label, o.status, o.error,
  o.api_store_id, o.store_id_match, o.api_stock, o.api_price,
  o.api_elite_price, o.api_equiv_g, o.api_raw, o.api_member_price,
  o.is_elite, p.image
FROM obs o
LEFT JOIN products   p ON p.sku      = o.sku
LEFT JOIN store_meta s ON s.store_id = o.store_id;
"""

SCHEMA = SCHEMA_TABLES + SCHEMA_VIEW


class LegacySchema(RuntimeError):
    """The database still has the pre-split single `observations` table."""


def _is_legacy(conn: sqlite3.Connection) -> bool:
    """True if `observations` is still a physical table rather than a view."""
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name='observations'").fetchone()
    return bool(row) and row[0] == "table"


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    conn = sqlite3.connect(path)
    # The web server reads while an index build writes. WAL lets those happen
    # at once, and the busy timeout makes a brief lock wait rather than raise
    # "database is locked" mid-request.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")

    # A pre-split database is NOT migrated here. Rewriting a 200 MB file is a
    # destructive, minutes-long operation, and doing it silently inside a
    # function every command calls would mean it happening while someone was
    # only trying to read. normalize_db.py does it deliberately, takes a backup
    # first, and verifies before dropping anything.
    if _is_legacy(conn):
        conn.close()
        raise LegacySchema(
            f"{path} still uses the pre-split `observations` table.\n"
            f"Migrate it once, with a backup and verification:\n"
            f"    python normalize_db.py --db \"{path}\"")

    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def write_rows(conn: sqlite3.Connection, run_id: str, rows: Iterable[dict]) -> int:
    """Persist observations, keeping the product/store lookups current.

    The lookup upserts never overwrite a good value with a blank one: the two
    fetchers populate slightly different field sets (the API index has no
    default_price, the scan path has no productType), so a plain REPLACE would
    let whichever ran last erase what the other knew.
    """
    rows = [{**r, "run_id": run_id} for r in rows]
    if not rows:
        return 0

    def _keep(table: str, col: str) -> str:
        return f"{col}=COALESCE(NULLIF(excluded.{col}, ''), {table}.{col})"

    prods = {}
    stores = {}
    for r in rows:
        if r.get("sku"):
            prods[r["sku"]] = tuple([r["sku"]] + [r.get(c) for c in PRODUCT_COLUMNS])
        if r.get("store_id"):
            stores[r["store_id"]] = tuple(
                [r["store_id"]] + [r.get(c) for c in STORE_COLUMNS])

    if prods:
        cols = ["sku"] + PRODUCT_COLUMNS
        conn.executemany(
            f"INSERT INTO products ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(sku) DO UPDATE SET "
            + ", ".join(_keep("products", c) for c in PRODUCT_COLUMNS),
            list(prods.values()))

    if stores:
        cols = ["store_id"] + STORE_COLUMNS
        conn.executemany(
            f"INSERT INTO store_meta ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(store_id) DO UPDATE SET "
            + ", ".join(_keep("store_meta", c) for c in STORE_COLUMNS),
            list(stores.values()))

    # INSERT OR REPLACE + the (run_id, store_id, sku) primary key is what makes
    # re-running a partial sweep idempotent, and therefore --resume safe.
    conn.executemany(
        f"INSERT OR REPLACE INTO obs ({','.join(OBS_COLUMNS)}) "
        f"VALUES ({','.join('?' * len(OBS_COLUMNS))})",
        [tuple(r.get(c) for c in OBS_COLUMNS) for r in rows],
    )
    conn.commit()
    return len(rows)


def done_store_ids(conn: sqlite3.Connection, run_id: str) -> set[str]:
    cur = conn.execute(
        "SELECT DISTINCT store_id FROM observations WHERE run_id=? AND status='ok'",
        (run_id,),
    )
    return {r[0] for r in cur.fetchall()}


def latest_run_id(conn: sqlite3.Connection) -> str | None:
    cur = conn.execute("SELECT MAX(run_id) FROM observations")
    row = cur.fetchone()
    return row[0] if row else None


def export_csv(conn: sqlite3.Connection, run_id: str,
               path: str | None = None) -> int:
    """Dump one run to CSV.

    This used pandas, which was the only thing in the project that did -- and
    it dragged numpy, tzdata and dateutil into every build for one
    read_sql_query and one to_csv. utf-8-sig keeps Excel happy, as before.
    """
    import csv

    path = path or config.CSV_PATH
    cur = conn.execute(
        "SELECT * FROM observations WHERE run_id=? ORDER BY sku, price",
        (run_id,),
    )
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow([d[0] for d in cur.description])
        for row in cur:
            writer.writerow(["" if v is None else v for v in row])
            n += 1
    return n


def mismatched_stores(conn: sqlite3.Connection, run_id: str) -> list[tuple]:
    """Stores the site priced as some OTHER store. Their prices are suspect."""
    cur = conn.execute(
        """
        SELECT DISTINCT store_id, store_name, city, api_store_id
        FROM observations
        WHERE run_id=? AND store_id_match = 0
        ORDER BY city, store_name
        """,
        (run_id,),
    )
    return cur.fetchall()


def in_stock(conn: sqlite3.Connection, run_id: str) -> list[tuple]:
    """Every store holding stock in this run, most stock first.

    Suspect rows (priced as a different store) are excluded -- their stock
    figure belongs to whichever store the site actually answered for.
    """
    cur = conn.execute(
        """
        SELECT sku, title, size, store_name, city, api_stock,
               price, member_price, api_elite_price
        FROM observations
        WHERE run_id=? AND status='ok' AND carried=1 AND available=1
          AND COALESCE(store_id_match, 1) = 1
        ORDER BY sku, COALESCE(api_stock, 0) DESC, city
        """,
        (run_id,),
    )
    return cur.fetchall()


def latest_observations(conn: sqlite3.Connection, skus: list[str] | None = None,
                        store_ids: list[str] | None = None) -> list[dict]:
    """Most recent good row per (sku, store_id), across ALL runs.

    This is the cache behind instant answers: we don't care which run a fact
    came from, only that it is the freshest one we hold for that store.
    """
    def clauses(p: str) -> tuple[str, list]:
        """Build the predicates with a table prefix (`` or `o.`).

        Both the subquery and the outer query filter identically; the outer one
        must qualify its columns or `sku`/`store_id` are ambiguous after the
        join.
        """
        w = [f"{p}status = 'ok'", f"COALESCE({p}store_id_match, 1) = 1"]
        vals: list = []
        if skus:
            w.append(f"{p}sku IN ({','.join('?' * len(skus))})")
            vals += [str(s) for s in skus]
        if store_ids:
            w.append(f"{p}store_id IN ({','.join('?' * len(store_ids))})")
            vals += [str(s) for s in store_ids]
        return " AND ".join(w), vals

    inner, params = clauses("")
    outer, params2 = clauses("o.")

    cur = conn.execute(
        f"""
        SELECT o.* FROM observations o
        JOIN (
            SELECT sku, store_id, MAX(scraped_at) AS newest
            FROM observations
            WHERE {inner}
            GROUP BY sku, store_id
        ) m ON o.sku = m.sku AND o.store_id = m.store_id
           AND o.scraped_at = m.newest
        WHERE {outer}
        """,
        params + params2,
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # One row per (sku, store) even if two runs share a timestamp.
    seen, out = set(), []
    for r in rows:
        key = (r["sku"], r["store_id"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def index_coverage(conn: sqlite3.Connection,
                   store_ids: list[str]) -> tuple[int, float | None]:
    """How many of these stores appear in a stock index, and how old is it?

    Lets us tell "we've never looked" apart from "the index covers this store
    and the product simply isn't in stock" -- the index holds in-stock items
    only, so absence is meaningful information rather than missing data.
    """
    if not store_ids:
        return 0, None
    marks = ",".join("?" * len(store_ids))
    cur = conn.execute(
        f"""
        SELECT COUNT(DISTINCT store_id), MAX(scraped_at)
        FROM observations
        WHERE run_id LIKE 'index-%' AND store_id IN ({marks})
        """,
        [str(s) for s in store_ids],
    )
    n, newest = cur.fetchone()
    if not n:
        return 0, None

    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(newest)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    except (TypeError, ValueError):
        age = None
    return n, age


def last_scan_attempt(conn: sqlite3.Connection,
                      store_ids: list[str] | None = None) -> dict[str, str]:
    """When we last *attempted* each store on the scan endpoint.

    Index runs are excluded deliberately: they use product/search, which is a
    different endpoint with different failure modes -- store 528 answers it
    fine while refusing every scan call. Mixing them would make a broken store
    look healthy.

    Errors count as attempts, which is the point: a failed re-test pushes the
    timestamp forward and so restarts the skip clock.
    """
    sql = ("SELECT store_id, MAX(scraped_at) FROM observations "
           "WHERE run_id NOT LIKE 'index-%'")
    params: list = []
    if store_ids:
        sql += f" AND store_id IN ({','.join('?' * len(store_ids))})"
        params = [str(s) for s in store_ids]
    sql += " GROUP BY store_id"
    return {row[0]: row[1] for row in conn.execute(sql, params) if row[1]}


def scan_failure_streaks(conn: sqlite3.Connection,
                         window: int = 5) -> list[dict]:
    """Stores whose recent scan attempts all failed -- skip-list candidates.

    Read-only. This is how the next store 528 gets found from evidence rather
    than from someone noticing a run felt slow.
    """
    cur = conn.execute(
        """
        SELECT store_id, store_name, city, status, error, scraped_at
        FROM observations
        WHERE run_id NOT LIKE 'index-%'
        ORDER BY store_id, scraped_at DESC
        """
    )
    seen: dict[str, list] = {}
    for sid, name, city, status, error, when in cur:
        rows = seen.setdefault(sid, [])
        if len(rows) < window:
            rows.append((name, city, status, error, when))

    out = []
    for sid, rows in seen.items():
        if len(rows) < 2 or any(r[2] == "ok" for r in rows):
            continue
        name, city = rows[0][0], rows[0][1]
        out.append({"store_id": sid, "name": name, "city": city,
                    "failures": len(rows), "last_seen": rows[0][4],
                    "error": rows[0][3] or ""})
    out.sort(key=lambda r: -r["failures"])
    return out


def index_runs(conn: sqlite3.Connection, province: str,
               limit: int = 5) -> list[dict]:
    """Recent index runs for one province, newest first.

    Drives the Resume button: a run covering fewer stores than the province
    has was interrupted, and `index_builder --resume <run_id>` will pick up
    exactly the stores it never reached.

    The run_id format is fixed by build_index() -- `index-{Province}-{stamp}`
    with spaces stripped from the province -- so a LIKE on that prefix is the
    lookup, and sorting by run_id sorts by time.
    """
    prefix = f"index-{province.replace(' ', '')}-"
    cur = conn.execute(
        """
        SELECT run_id, COUNT(DISTINCT store_id), MAX(scraped_at)
        FROM observations
        WHERE run_id LIKE ? AND status = 'ok'
        GROUP BY run_id
        ORDER BY run_id DESC
        LIMIT ?
        """,
        (prefix + "%", limit),
    )
    return [{"run_id": r[0], "stores": r[1], "at": r[2]}
            for r in cur.fetchall()]


def indexed_store_ids(conn: sqlite3.Connection,
                      store_ids: list[str] | None = None) -> set[str]:
    """Stores that appear in some stock index run.

    If a store is indexed and a product has no row for it, the product is not
    in stock there -- that is an answer, not missing data, and the report says
    so rather than silently omitting the store.
    """
    sql = "SELECT DISTINCT store_id FROM observations WHERE run_id LIKE 'index-%'"
    params: list = []
    if store_ids:
        sql += f" AND store_id IN ({','.join('?' * len(store_ids))})"
        params = [str(s) for s in store_ids]
    return {r[0] for r in conn.execute(sql, params)}


def province_facts(conn: sqlite3.Connection,
                   store_ids: list[str]) -> dict[str, dict]:
    """Per-SKU facts for a province, from the freshest row at each store.

    One pass gives everything the search UI needs -- whether anything is in
    stock, the category, and THC/CBD -- instead of three separate scans of a
    90k-row table.

    THC/CBD are product-level, so MAX() just picks a non-null value rather
    than aggregating anything meaningful.
    """
    if not store_ids:
        return {}
    marks = ",".join("?" * len(store_ids))
    ids = [str(s) for s in store_ids]
    cur = conn.execute(
        f"""
        SELECT o.sku,
               MAX(COALESCE(o.available, 0)),
               MAX(o.category),
               MAX(o.thc),
               MAX(o.cbd)
        FROM observations o
        JOIN (
            SELECT sku, store_id, MAX(scraped_at) AS newest
            FROM observations
            WHERE store_id IN ({marks}) AND status = 'ok'
            GROUP BY sku, store_id
        ) m ON o.sku = m.sku AND o.store_id = m.store_id
           AND o.scraped_at = m.newest
        WHERE o.store_id IN ({marks})
        GROUP BY o.sku
        """,
        ids + ids,
    )
    return {r[0]: {"available": bool(r[1]), "category": r[2] or "",
                   "thc": r[3] or "", "cbd": r[4] or ""}
            for r in cur.fetchall()}


def available_skus(conn: sqlite3.Connection, store_ids: list[str]) -> set[str]:
    """Every SKU in stock at at least one of these stores, freshest data only.

    Used to hide products from search that the whole province is out of. Takes
    the newest row per (sku, store) first, so a sold-out item cannot look
    available on the strength of a stale observation.
    """
    if not store_ids:
        return set()
    marks = ",".join("?" * len(store_ids))
    ids = [str(s) for s in store_ids]
    cur = conn.execute(
        f"""
        SELECT DISTINCT o.sku
        FROM observations o
        JOIN (
            SELECT sku, store_id, MAX(scraped_at) AS newest
            FROM observations
            WHERE store_id IN ({marks}) AND status = 'ok'
            GROUP BY sku, store_id
        ) m ON o.sku = m.sku AND o.store_id = m.store_id
           AND o.scraped_at = m.newest
        WHERE o.store_id IN ({marks}) AND o.available = 1
        """,
        ids + ids,
    )
    return {r[0] for r in cur.fetchall()}


def cache_age_hours(rows: list[dict]) -> float | None:
    """Age of the freshest row, in hours."""
    stamps = [r.get("scraped_at") for r in rows if r.get("scraped_at")]
    if not stamps:
        return None
    from datetime import datetime, timezone
    try:
        newest = max(datetime.fromisoformat(s) for s in stamps)
    except ValueError:
        return None
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - newest).total_seconds() / 3600.0


def price_summary(conn: sqlite3.Connection, run_id: str) -> list[tuple]:
    """Cheapest and dearest store per SKU for a run."""
    cur = conn.execute(
        """
        SELECT sku, title,
               COUNT(*)                                  AS stores,
               SUM(COALESCE(carried, 0))                 AS carried,
               SUM(COALESCE(available, 0))               AS in_stock,
               MIN(price), MAX(price),
               MIN(member_price), MAX(member_price)
        FROM observations
        WHERE run_id=? AND status='ok'
        GROUP BY sku, title
        ORDER BY title
        """,
        (run_id,),
    )
    return cur.fetchall()
