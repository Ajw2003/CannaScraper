"""Write data/index.json, the list of provinces the static page offers.

Called by ci/publish_gh_pages.sh after it has gathered every province's
data/<slug>.json. Every data file is parsed here, so a corrupt one fails the
publish instead of breaking the live page.

    python ci/build_manifest.py DATA_DIR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: build_manifest.py DATA_DIR", file=sys.stderr)
        return 2
    data = Path(argv[1])
    provinces = []
    for f in sorted(data.glob("*.json")):
        # Non-province files published alongside the province exports: the
        # slim catalogue and the copied registries. Skipped by name, and
        # again by shape below, so a future non-province file that slips
        # past the name check still doesn't crash this on a missing key.
        if f.name in ("index.json", "catalog.json", "stores.json", "geocode.json"):
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        if "province" not in d:
            continue
        provinces.append({
            "province": d["province"], "slug": f.stem,
            "generated_at": d["generated_at"],
            "stores": sum(1 for s in d["stores"] if s.get("scraped_at")),
            "products": len(d["products"]),
        })
    if not provinces:
        print(f"error: no province data in {data}", file=sys.stderr)
        return 1
    (data / "index.json").write_text(json.dumps({"provinces": provinces}), encoding="utf-8")
    for p in provinces:
        print(f"{p['province']:<18} {p['stores']:>3} stores  {p['products']:>5} products  "
              f"{p['generated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
