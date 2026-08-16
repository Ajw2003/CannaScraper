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
    "api_equiv_g", "api_raw", "status", "error",
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
]


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH)
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
