"""Shrink a province's history DB to its current state.

Built for the hourly scrape (.github/workflows/scrape-one.yml). Every run
adds a full copy of every in-stock row -- about 5.6 MB per run for
Saskatchewan's 15k rows, ~40 MB for Ontario -- and the DB travels as a GitHub
release file capped at 2 GB, so hourly runs would hit the cap within days.

What survives is exactly what the pipeline reads: for each (sku, store), the
newest good row, which is what db.latest_observations() returns. That drives
the published JSON, index_builder's "sold out since last run" close-out, and
the fallback for a store that failed this run. Only rows with a NEWER good row
for the same (sku, store) are deleted, so latest_observations() gives the same
answer before and after -- this script checks that and refuses to save
otherwise. Past price and stock history is deliberately not kept
(docs/6-decisions/Decisions.md, 2026-09-28).

    python ci/prune_history.py --db PATH
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402  (needs the repo root on sys.path first)

# A row goes only if the same (sku, store) has a newer "good" row -- good as
# db.latest_observations() defines it: status ok, and not priced as a
# different store than the one asked for.
PRUNE_SQL = """
DELETE FROM obs
WHERE EXISTS (
    SELECT 1 FROM obs newer
    WHERE newer.sku = obs.sku
      AND newer.store_id = obs.store_id
      AND newer.scraped_at > obs.scraped_at
      AND newer.status = 'ok'
      AND COALESCE(newer.store_id_match, 1) = 1
)
"""


def snapshot(conn) -> set[tuple]:
    """What the pipeline would read, as a comparable set."""
    return {(r["sku"], r["store_id"], r["scraped_at"], r["available"], r["api_stock"],
             r["price"]) for r in db.latest_observations(conn)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Keep only the current state in a history DB")
    ap.add_argument("--db", required=True)
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"error: {args.db} does not exist", file=sys.stderr)
        return 2

    size_before = os.path.getsize(args.db)
    conn = db.connect(args.db)
    try:
        rows_before = conn.execute("SELECT COUNT(*) FROM obs").fetchone()[0]
        before = snapshot(conn)

        conn.execute(PRUNE_SQL)
        after = snapshot(conn)
        if after != before:
            conn.rollback()
            print(f"error: pruning would change the current state "
                  f"({len(before)} -> {len(after)} rows); nothing deleted.", file=sys.stderr)
            return 1
        conn.commit()

        rows_after = conn.execute("SELECT COUNT(*) FROM obs").fetchone()[0]
        # DELETE only marks pages free; VACUUM is what makes the file smaller.
        conn.execute("VACUUM")
    finally:
        conn.close()

    size_after = os.path.getsize(args.db)
    print(f"Pruned {rows_before - rows_after} of {rows_before} rows "
          f"({len(before)} current rows kept); "
          f"{size_before / 1e6:.1f} MB -> {size_after / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
