"""Assert the exported province JSON and data/index.json have the shape the
static site's page code (site/static-api.js) actually reads.

This is a narrower, faster check than the browser-driven parity jobs: it
looks only at the JSON files ci/export_province.py, ci/export_catalog.py and
ci/build_manifest.py produce, without starting any server. See
docs/4-systems/ci-checks.md, "export-shape".

    python3 ci/check_export_shape.py DATA_DIR --province Saskatchewan

DATA_DIR must hold <slug>.json (the province export) and index.json (the
manifest). Exits 1 and prints every problem found, not just the first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Stock row fields, in order, per ci/export_province.py's own comment:
#   [store_id, api_stock, price, tier_label, tier_amt, thc, cbd,
#    available(0/1), stock_text, time_index(int), carried]
MIN_STOCK_ROW_LEN = 11


def slugify(province: str) -> str:
    return province.strip().lower().replace(" ", "-")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", help="directory holding <slug>.json and index.json")
    ap.add_argument("--province", required=True)
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    problems: list[str] = []

    slug = slugify(args.province)
    export_path = data_dir / f"{slug}.json"
    if not export_path.exists():
        print(f"::error::{export_path} does not exist", file=sys.stderr)
        return 1
    export = json.loads(export_path.read_text(encoding="utf-8"))

    for key in ("province", "generated_at", "run", "stores", "products", "stock", "times"):
        if key not in export:
            problems.append(f"{export_path}: missing key {key!r}")

    if "run" in export and export["run"] is not None:
        failed_stores = export["run"].get("failed_stores")
        if not isinstance(failed_stores, list):
            problems.append(
                f"{export_path}: run.failed_stores is {type(failed_stores).__name__}, expected a list")

    stock = export.get("stock") or {}
    if not isinstance(stock, dict):
        problems.append(f"{export_path}: stock is {type(stock).__name__}, expected an object")
    else:
        for sku, rows in stock.items():
            if not isinstance(rows, list):
                problems.append(f"{export_path}: stock[{sku!r}] is not a list")
                continue
            for i, row in enumerate(rows):
                if not isinstance(row, list) or len(row) < MIN_STOCK_ROW_LEN:
                    problems.append(
                        f"{export_path}: stock[{sku!r}][{i}] has {len(row) if isinstance(row, list) else 'n/a'} "
                        f"fields, expected >= {MIN_STOCK_ROW_LEN}")
                    continue
                available = row[7]
                if available not in (0, 1):
                    problems.append(
                        f"{export_path}: stock[{sku!r}][{i}][7] (available) = {available!r}, expected 0 or 1")
                time_idx = row[9]
                if not isinstance(time_idx, int):
                    problems.append(
                        f"{export_path}: stock[{sku!r}][{i}][9] (time index) = {time_idx!r}, expected an int")

    index_path = data_dir / "index.json"
    if not index_path.exists():
        problems.append(f"{index_path} does not exist")
    else:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        provinces = index.get("provinces") if isinstance(index, dict) else index
        names = [p.get("province") for p in provinces] if isinstance(provinces, list) else []
        if args.province not in names:
            problems.append(f"{index_path}: {args.province!r} not listed (found: {names})")

    if problems:
        for p in problems:
            print(f"::error::{p}")
        print(f"{len(problems)} problem(s)")
        return 1

    print(f"OK: {export_path} and {index_path} match the expected shape for {args.province}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
