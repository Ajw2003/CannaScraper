"""Split the old single `observations` table into obs + products + store_meta.

Run once per database. Safe to re-run: it detects an already-migrated file and
exits without touching it.

What it does, in order, and nothing is destroyed until step 5 has passed:

    1  PRAGMA integrity_check on the existing file
    2  copy the file to <db>.pre-normalize
    3  build products, store_meta and obs from observations
    4  VERIFY -- compare all 34 columns of every row, old vs new
    5  drop the old table, create the `observations` view, VACUUM

Step 4 is the reason this is a script and not something connect() does. It
reads back every migrated row through the same join the view will use and
compares it, column by column, against the original table. Differences are
classified:

    filled   old was NULL or '', new has a value   -- benign, and expected:
             the two fetchers populate different field subsets, so one row's
             blank title is another row's real title once they share a SKU
    changed  old and new both have values, and they differ -- must be
             confined to the handful of SKUs that genuinely carry more than
             one product title, or the migration aborts

    python normalize_db.py                  # the configured database
    python normalize_db.py --db history.db
    python normalize_db.py --dry-run        # build and verify, change nothing
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time

import config
import db

# The budget is in SKUs, not rows, and that distinction matters: 3 SKUs whose
# title changed produce 438 differing rows once multiplied across 92 stores.
# Rows measure how widely a product is stocked; SKUs measure how much product
# information the split actually loses. Measured on the live database, the
# affected counts are title 3, image 2, handle 1, category 1 -- so 25 leaves
# ample room while still catching a wrong premise.
MAX_CHANGED_SKUS = 25

# Only these may differ at all. Everything else lives on the observation row
# and must survive byte-identical.
MOVABLE = set(db.PRODUCT_COLUMNS) | set(db.STORE_COLUMNS) | {"url"}


def _say(step: str, verdict: str, msg: str) -> None:
    print(f"  {verdict:<6} {msg}")


def _legacy(conn) -> bool:
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name='observations'").fetchone()
    return bool(row) and row[0] == "table"


def _build(conn) -> None:
    """Populate the three new tables from the old one.

    Product/store values are taken from each SKU's most recent row, then any
    blank is back-filled from the newest row that did have a value. Two cheap
    passes rather than one correlated subquery per column per SKU.
    """
    cols = db.PRODUCT_COLUMNS
    conn.execute(f"""
        INSERT INTO products (sku, {','.join(cols)})
        SELECT sku, {','.join(cols)} FROM (
            SELECT sku, {','.join(cols)},
                   ROW_NUMBER() OVER (PARTITION BY sku
                                      ORDER BY scraped_at DESC) rn
            FROM observations
        ) WHERE rn = 1
    """)
    # Back-fill blanks. MAX(NULLIF(...)) picks an arbitrary non-empty value,
    # which is fine precisely because these columns were verified ~1:1 per SKU.
    sets = ", ".join(
        f"{c} = COALESCE(NULLIF(products.{c}, ''), "
        f"(SELECT MAX(NULLIF(o.{c}, '')) FROM observations o "
        f"WHERE o.sku = products.sku))" for c in cols)
    conn.execute(f"UPDATE products SET {sets}")

    scols = db.STORE_COLUMNS
    conn.execute(f"""
        INSERT INTO store_meta (store_id, {','.join(scols)})
        SELECT store_id, {','.join(scols)} FROM (
            SELECT store_id, {','.join(scols)},
                   ROW_NUMBER() OVER (PARTITION BY store_id
                                      ORDER BY scraped_at DESC) rn
            FROM observations
        ) WHERE rn = 1
    """)
    ssets = ", ".join(
        f"{c} = COALESCE(NULLIF(store_meta.{c}, ''), "
        f"(SELECT MAX(NULLIF(o.{c}, '')) FROM observations o "
        f"WHERE o.store_id = store_meta.store_id))" for c in scols)
    conn.execute(f"UPDATE store_meta SET {ssets}")

    conn.execute(f"""
        INSERT OR REPLACE INTO obs ({','.join(db.OBS_COLUMNS)})
        SELECT {','.join(db.OBS_COLUMNS)} FROM observations
    """)
    conn.commit()


def _cleanup(conn) -> None:
    """Undo step 3.

    A plain rollback() is not enough: _build() commits, because building 295k
    rows inside one open transaction is how you get a 200 MB journal. So
    "leave the database as we found it" means explicitly dropping what we
    added. The old `observations` table is never touched before step 5, so
    this really does restore the original state.
    """
    for t in ("obs", "products", "store_meta"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()


_VIEW_HEAD = "CREATE VIEW IF NOT EXISTS observations AS"


def _joined_sql() -> str:
    """The view's own SELECT, extracted from db.SCHEMA_VIEW.

    Deliberately NOT a copy. The view cannot be created yet -- the old table
    still owns the name -- so step 4 has to run the query standalone. Writing
    it out a second time is how the two drift, and a verification that checks
    something other than what will be served is worse than no verification.
    """
    sql = db.SCHEMA_VIEW
    if _VIEW_HEAD not in sql:
        raise RuntimeError(
            "db.SCHEMA_VIEW no longer starts with the expected CREATE VIEW "
            "statement; update _VIEW_HEAD to match it.")
    return sql[sql.index(_VIEW_HEAD) + len(_VIEW_HEAD):].rstrip().rstrip(";")


def _verify(conn) -> tuple[bool, dict]:
    """Compare every row, every column, old table vs new join."""
    old_n = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    new_n = conn.execute("SELECT COUNT(*) FROM obs").fetchone()[0]
    if old_n != new_n:
        _say("4", "FAIL", f"row count changed: {old_n:,} -> {new_n:,}")
        return False, {}
    _say("4", "PASS", f"row count preserved: {new_n:,}")

    old = conn.execute(
        "SELECT " + ",".join(db.VIEW_COLUMNS) +
        " FROM observations ORDER BY run_id, store_id, sku")
    new = conn.execute(
        f"SELECT * FROM ({_joined_sql()}) ORDER BY run_id, store_id, sku")

    sku_i = db.VIEW_COLUMNS.index("sku")

    def _blank(v) -> bool:
        return v is None or v == ""

    filled: dict[str, int] = {}
    blanked: dict[str, int] = {}
    changed: dict[str, int] = {}
    changed_skus: dict[str, set] = {}
    examples: dict[str, tuple] = {}
    n = 0
    while True:
        a, b = old.fetchone(), new.fetchone()
        if a is None or b is None:
            break
        n += 1
        for col, va, vb in zip(db.VIEW_COLUMNS, a, b):
            if va == vb:
                continue
            if _blank(va) and _blank(vb):
                # '' became NULL or vice versa. Provably invisible downstream:
                # export_csv maps None to '', and every query that reads these
                # columns finishes with `or ""` or COALESCE. Counted, not
                # treated as data loss.
                blanked[col] = blanked.get(col, 0) + 1
            elif _blank(va):
                filled[col] = filled.get(col, 0) + 1
            else:
                changed[col] = changed.get(col, 0) + 1
                changed_skus.setdefault(col, set()).add(a[sku_i])
                examples.setdefault(col, (va, vb))

    _say("4", "PASS", f"compared {n:,} rows across {len(db.VIEW_COLUMNS)} columns")

    ok = True
    touched = set(filled) | set(changed) | set(blanked)
    illegal = touched - MOVABLE
    if illegal:
        _say("4", "FAIL", f"observation-level columns differ: {sorted(illegal)}")
        ok = False

    if blanked:
        print()
        print("         blank representation normalised ('' <-> NULL, no")
        print("         downstream reader distinguishes them):")
        for c, k in sorted(blanked.items(), key=lambda kv: -kv[1]):
            print(f"           {c:<16} {k:>9,} rows")

    if filled:
        print()
        print("         blanks filled in (benign — one fetcher knew what the")
        print("         other did not, and they now share a product row):")
        for c, k in sorted(filled.items(), key=lambda kv: -kv[1]):
            print(f"           {c:<16} {k:>9,} rows")

    if changed:
        print()
        print("         values CHANGED — real information dropped:")
        print(f"           {'column':<16} {'rows':>9} {'SKUs':>6}   example")
        for c, k in sorted(changed.items(), key=lambda kv: -len(changed_skus[c])):
            was, now = examples[c]
            print(f"           {c:<16} {k:>9,} {len(changed_skus[c]):>6}   "
                  f"{str(was)[:22]!r} -> {str(now)[:22]!r}")
        worst = max(len(s) for s in changed_skus.values())
        if worst > MAX_CHANGED_SKUS:
            _say("4", "FAIL", f"{worst} SKUs changed on one column, over the "
                              f"{MAX_CHANGED_SKUS} allowed")
            ok = False
        else:
            _say("4", "PASS", f"at most {worst} SKU(s) affected on any column "
                              f"(budget {MAX_CHANGED_SKUS}) — these are "
                              f"renamed/re-imaged products, newest value kept")

    return ok, {"rows": n, "filled": filled, "changed": changed,
                "blanked": blanked}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--dry-run", action="store_true",
                    help="build and verify, then roll back and change nothing")
    args = ap.parse_args(argv)

    print("=" * 74)
    print(f"normalize {args.db}")
    print("=" * 74)

    if not os.path.exists(args.db):
        print(f"\n  FAIL  no such database: {args.db}")
        return 2

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout=10000")

    if not _legacy(conn):
        print("\n  PASS  already migrated — `observations` is a view. "
              "Nothing to do.")
        conn.close()
        return 0

    # --- STEP 1 -----------------------------------------------------------
    print("\nSTEP 1  Check the existing file")
    t0 = time.time()
    res = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if res != "ok":
        _say("1", "FAIL", f"integrity_check says: {res[:200]}")
        conn.close()
        return 1
    n = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    mb = os.path.getsize(args.db) / 1048576
    _say("1", "PASS", f"integrity ok — {n:,} rows, {mb:,.1f} MB "
                      f"({time.time() - t0:.0f}s)")

    # --- STEP 2 -----------------------------------------------------------
    print("\nSTEP 2  Back up before touching anything")
    backup = args.db + ".pre-normalize"
    if os.path.exists(backup):
        _say("2", "FAIL", f"{backup} already exists — move or delete it first, "
                          f"so an earlier backup is never overwritten.")
        conn.close()
        return 1
    conn.close()                       # flush WAL so the copy is complete
    t0 = time.time()
    shutil.copy2(args.db, backup)
    _say("2", "PASS", f"copied to {os.path.basename(backup)} "
                      f"({os.path.getsize(backup) / 1048576:,.1f} MB, "
                      f"{time.time() - t0:.0f}s)")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout=10000")

    # --- STEP 3 -----------------------------------------------------------
    print("\nSTEP 3  Build the new tables")
    t0 = time.time()
    # Clear any half-built tables a previous interrupted attempt left behind,
    # so this always starts from the old table and nothing else. The old table
    # is not touched until step 5, so this cannot lose data.
    _cleanup(conn)
    # Only the tables -- the view cannot exist yet, the old table owns the name.
    conn.executescript(db.SCHEMA_TABLES)
    _build(conn)
    p = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    s = conn.execute("SELECT COUNT(*) FROM store_meta").fetchone()[0]
    o = conn.execute("SELECT COUNT(*) FROM obs").fetchone()[0]
    _say("3", "PASS", f"{p:,} products, {s:,} stores, {o:,} observations "
                      f"({time.time() - t0:.0f}s)")

    # --- STEP 4 -----------------------------------------------------------
    print("\nSTEP 4  Verify every row against the original")
    t0 = time.time()
    ok, _stats = _verify(conn)
    print(f"\n         ({time.time() - t0:.0f}s)")
    if not ok:
        print("\n  ABORTED — the original table was never touched. It is still")
        print(f"  in {args.db}, and a copy is at {os.path.basename(backup)}.")
        print("  Investigate the differences above before retrying.")
        _cleanup(conn)
        conn.close()
        return 1

    if args.dry_run:
        print("\n  --dry-run: dropping the new tables, database unchanged.")
        _cleanup(conn)
        conn.close()
        return 0

    # --- STEP 5 -----------------------------------------------------------
    print("\nSTEP 5  Swap in the view and reclaim the space")
    t0 = time.time()
    conn.execute("DROP TABLE observations")
    conn.executescript(db.SCHEMA_VIEW)
    conn.commit()
    conn.execute("VACUUM")
    # Fold the write-ahead log back in BEFORE measuring. Without this the file
    # is measured mid-checkpoint and the saving is overstated -- the first run
    # of this script reported 68.7 MB for a database that settled at 97.4 MB.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    after = os.path.getsize(args.db) / 1048576
    _say("5", "PASS", f"view created, vacuumed — {mb:,.1f} MB -> {after:,.1f} MB "
                      f"({(1 - after / mb) * 100:.0f}% smaller, "
                      f"{time.time() - t0:.0f}s)")

    print("\n" + "=" * 74)
    print("DONE.  Rollback, if you ever need it:")
    print(f"    del \"{args.db}\"")
    print(f"    ren \"{os.path.basename(backup)}\" \"{os.path.basename(args.db)}\"")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted — the original table is only dropped in step 5.")
        sys.exit(130)
