# Systems index

One document per runtime-critical system: if it breaks, the product stops working or lies about
its data. Each covers what it owns, how it works, its invariants, and its traps.

| System | Owns | Doc |
|---|---|---|
| Catalog & watchlist | Pulling the public product catalog, resolving what to track | [catalog.md](catalog.md) |
| Store registry | Parsing/caching the 225-store list embedded in the site's JS | [store-registry.md](store-registry.md) |
| Fetchers, rate limiting & egress | Getting per-store data out of the site without tripping its limit | [fetchers-and-rate-limiting.md](fetchers-and-rate-limiting.md) |
| Index builder & work queue | Building a whole-province stock index durably, with multiple workers | [index-builder-and-work-queue.md](index-builder-and-work-queue.md) |
| Persistence (db.py) | The SQLite history: schema, the `observations` view, writes | [persistence.md](persistence.md) |
| Web app, auth & tunnel | The FastAPI server, the admin password, the public URL | [web-app-auth-tunnel.md](web-app-auth-tunnel.md) |
| Packaging & build | Turning the checkout into `CannaCabana.exe` and stamping what it is | [packaging-and-build.md](packaging-and-build.md) |
| Hourly trigger | Getting the scrape to actually run hourly despite GitHub's unreliable `schedule:` | [hourly-trigger.md](hourly-trigger.md) |
| CI checks | The regression checks that run on every PR and push to main | [ci-checks.md](ci-checks.md) |

## Considered and left out

- **`main.py`** (CLI orchestration) — a thin orchestration layer over the systems above
  (catalog, stores, fetchers, db). Its own logic (arg parsing, `--compare`, resume) is not
  something that, if wrong, silently corrupts data the way e.g. `db.write_rows` would; it just
  fails loudly. Not treated as its own tier-4 system.
- **`report.py`** (the standalone HTML report) — a rendering layer over data `index_builder.py`
  already produced. If it breaks, nothing about the underlying data is wrong, only its optional
  static export.
- **`config.py`** — pure configuration (URLs, selectors, tunables), not a system with behaviour
  of its own; referenced from every system doc instead of getting one.
- **`discover.py`, `payload_probe.py`, `scan_ceiling_probe.py`, `verify_scan_fix.py`,
  `ratelimit_probe.py`, `db_bench.py`** — one-off dev/verification tools, not part of the
  running product.
- **`selftest.py`** — a test harness (19+ checks, see `selftest.py:16`), not a runtime system.
