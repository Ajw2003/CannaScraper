"""Persistence: append-only SQLite history + a latest-snapshot CSV.

Rows are written after every store completes, so a crash at store 80 of 92
never costs the first 79.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  run_id        TEXT NOT NULL,
  scraped_at    TEXT,
  store_id      TEXT NOT NULL,
  store_name    TEXT,
  city          TEXT,
  province      TEXT,
  sku           TEXT NOT NULL,
  handle        TEXT,
  title         TEXT,
  brand         TEXT,
  category      TEXT,
  size          TEXT,
  url           TEXT,
  price         REAL,
  member_price  REAL,
  default_price TEXT,
  available     INTEGER,
  carried       INTEGER,
  stock_text    TEXT,
  thc           TEXT,
  cbd           TEXT,
  store_label   TEXT,
  api_store_id   TEXT,     -- store the site actually priced against
  store_id_match INTEGER,  -- 0 = priced as a DIFFERENT store; treat as suspect
  api_stock       INTEGER, -- exact unit count (DOM only says "In Stock")
  api_member_price REAL,   -- member price straight from the pricing call
  api_price       REAL,    -- authoritative retail price
  api_elite_price REAL,    -- ELITE price, never rendered in the DOM
  is_elite        INTEGER, -- 1 = ELITE-tier product (member price N/A)
  image           TEXT,    -- Shopify CDN url; resize with ?width=N
  api_equiv_g     REAL,    -- gram equivalence toward the 30 g limit
  api_raw         TEXT,    -- full positional CSV, for fields not yet decoded
  status        TEXT,
  error         TEXT,
  PRIMARY KEY (run_id, store_id, sku)
);
CREATE INDEX IF NOT EXISTS idx_sku_time  ON observations(sku, scraped_at);
CREATE INDEX IF NOT EXISTS idx_run       ON observations(run_id);
CREATE INDEX IF NOT EXISTS idx_store_sku ON observations(store_id, sku);
"""

COLUMNS = [
    "run_id", "scraped_at", "store_id", "store_name", "city", "province",
    "sku", "handle", "title", "brand", "category", "size", "url",
    "price", "member_price", "default_price", "available", "carried",
    "stock_text", "thc", "cbd", "store_label", "api_store_id",
    "store_id_match", "api_stock", "api_member_price", "api_price", "api_elite_price",
    "api_equiv_g", "api_raw", "is_elite", "image", "status", "error",
]

# Columns added after the first schema shipped. CREATE TABLE IF NOT EXISTS will
# not add them to an existing database, so patch them in on connect.
_MIGRATIONS = [
    ("carried", "INTEGER"),
    ("api_store_id", "TEXT"),
    ("store_id_match", "INTEGER"),
    ("api_stock", "INTEGER"),
    ("api_member_price", "REAL"),
    ("api_price", "REAL"),
    ("api_elite_price", "REAL"),
    ("api_equiv_g", "REAL"),
    ("api_raw", "TEXT"),
    ("is_elite", "INTEGER"),
    ("image", "TEXT"),
]


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH)
    # The web server reads while an index build writes. WAL lets those happen
    # at once, and the busy timeout makes a brief lock wait rather than raise
    # "database is locked" mid-request.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(observations)")}
    for name, decl in _MIGRATIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {name} {decl}")
    conn.commit()
    return conn


def write_rows(conn: sqlite3.Connection, run_id: str, rows: Iterable[dict]) -> int:
    payload = []
    for r in rows:
        r = {**r, "run_id": run_id}
        payload.append(tuple(r.get(c) for c in COLUMNS))
    if not payload:
        return 0
    # INSERT OR REPLACE + the (run_id, store_id, sku) primary key is what makes
    # re-running a partial sweep idempotent, and therefore --resume safe.
    conn.executemany(
        f"INSERT OR REPLACE INTO observations ({','.join(COLUMNS)}) "
        f"VALUES ({','.join('?' * len(COLUMNS))})",
        payload,
    )
    conn.commit()
    return len(payload)


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
    import pandas as pd

    path = path or config.CSV_PATH
    df = pd.read_sql_query(
        "SELECT * FROM observations WHERE run_id=? ORDER BY sku, price",
        conn, params=(run_id,),
    )
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return len(df)


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
