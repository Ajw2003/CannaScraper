# Systems

One document per runtime-critical system — each covering what it owns, how it works, its
invariants, and its traps, in that order.

| System | Owns |
|---|---|
| [scraping-and-fetching.md](scraping-and-fetching.md) | Getting one store's price/stock off the live site, via either backend |
| [rate-limiting-and-egress.md](rate-limiting-and-egress.md) | Staying under the site's shared rate-limit budget; optional multi-route throughput |
| [data-store.md](data-store.md) | The SQLite history: schema, writes, and every "what do we know now" read |
| [catalog-and-stores.md](catalog-and-stores.md) | Product metadata cache, watchlist resolution, the 225-store registry, nearest-store math |
| [indexing-pipeline.md](indexing-pipeline.md) | Building a full-province stock snapshot durably and resumably |
| [web-server-and-jobs.md](web-server-and-jobs.md) | The desktop app's HTTP API, the serialized job worker, the password gate |
| [desktop-packaging.md](desktop-packaging.md) | Turning the source tree into a working, self-verifying, self-updating exe |
| [pages-deployment.md](pages-deployment.md) | The GitHub Pages per-province mirror: nightly export, workflows, static frontend |
| [diagnostics.md](diagnostics.md) | The regression-test harness and the historical one-off investigation scripts |

## Considered and left out

- **The frontend, `web/index.html`, was folded into [web-server-and-jobs.md](web-server-and-jobs.md)**
  rather than given its own document — at 851 lines it's substantial, but it's a thin client with
  no logic of its own beyond what the API routes already define; documenting the routes covers it.
- **`main.py` (CLI orchestration)** was folded into whichever system its call sites belong to
  (mainly [scraping-and-fetching.md](scraping-and-fetching.md) and
  [web-server-and-jobs.md](web-server-and-jobs.md), via `fill_missing_stores()`) rather than
  documented standalone — it's an orchestration layer over those systems, not a system with its
  own invariants.
- **`config.py`** was not given its own document — it's the single place every other system's
  tunables live (by deliberate design, per
  [BUILD_INSTRUCTIONS.md](../../CannaScraper/BUILD_INSTRUCTIONS.md) Step 2: "put every URL,
  selector, and tunable here and nowhere else"), so its content is referenced from whichever
  system doc uses a given setting rather than duplicated into a document of its own.
- **The (not yet built) receiving ledger** (`inventory/` package, per
  [PLAN_receiving_ledger.md](../../CannaScraper/PLAN_receiving_ledger.md)) has no system doc yet —
  nothing exists to document. It'll get one when Phase 1 lands; see
  [Roadmap.md](../Roadmap.md#6-shipment-receiving--central-stock-ledger--not-started).
