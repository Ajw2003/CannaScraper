# CannaScraper — documentation index

## What this is

A tool that sweeps every Canna Cabana store (225 stores, 5 provinces) for price and stock data,
because the site itself only shows one store at a time. Runs as a CLI, a local/LAN/public web
app, or a packaged Windows `.exe`. Persists to SQLite, exports CSV, and can build a whole-province
stock index. See the root [`README.md`](../../README.md) for the full, detailed walkthrough —
that document remains the primary technical reference; this index is the map on top of it.

## The moving parts

| Part | Role |
|---|---|
| `catalog.py` | Public catalog + watchlist resolution |
| `stores.py` | Store registry (225 stores), parsed from embedded JS |
| `fetchers/` | Two backends: real browser, and the site's own API |
| `ratelimit.py`, `egress.py` | Rate-limit tracking and a multi-route throughput pool |
| `index_builder.py`, `workqueue.py` | Durable, resumable, whole-province stock index build |
| `db.py`, `normalize_db.py` | SQLite persistence (`obs`/`products`/`store_meta` + view) |
| `server.py`, `app.py`, `auth.py`, `tunnel.py` | Web UI, admin auth, public tunnel |
| `build.ps1`, `CannaCabana.spec`, `buildinfo.py` | Packaging into a distributable `.exe` |
| `main.py`, `find.bat` | CLI orchestration and interactive launcher |
| `selftest.py` | 19+ checks, no network required |

## The tiers

| Tier | File | Answers |
|---|---|---|
| 1 — Landing | This file | What is this, where is everything |
| 2 — Roadmap | [`docs/2-roadmap/Roadmap.md`](../2-roadmap/Roadmap.md) | What "done" means, per milestone |
| 3 — State | [`docs/3-state/ProjectState.md`](../3-state/ProjectState.md) | Where it stands right now |
| 4 — Systems | [`docs/4-systems/README.md`](../4-systems/README.md) | How each runtime-critical system works |
| 5 — Today | [`docs/5-today/Today.md`](../5-today/Today.md) | What's being worked on today, and why |
| 6 — Decisions | [`docs/6-decisions/Decisions.md`](../6-decisions/Decisions.md) | Why a decision was made |

## Systems (tier 4)

| System | Owns |
|---|---|
| [Catalog & watchlist](../4-systems/catalog.md) | Pulling the public catalog, resolving what to track |
| [Store registry](../4-systems/store-registry.md) | Parsing/caching the 225-store list |
| [Fetchers, rate limiting & egress](../4-systems/fetchers-and-rate-limiting.md) | Getting per-store data out without tripping the rate limit |
| [Index builder & work queue](../4-systems/index-builder-and-work-queue.md) | Whole-province stock index, durable and resumable |
| [Persistence](../4-systems/persistence.md) | SQLite schema, the `observations` view, writes |
| [Web app, auth & tunnel](../4-systems/web-app-auth-tunnel.md) | FastAPI server, admin password, public URL |
| [Packaging & build](../4-systems/packaging-and-build.md) | Turning the checkout into `CannaCabana.exe` |

## Live plans (root-level, not yet executed or in progress)

These stay at the repo root by design (see `PLAN_*` filenames) and are referenced, not
duplicated, here:

- [`PLAN_app_distribution.md`](../../PLAN_app_distribution.md) — the plan behind roadmap M5
  (packaged, publicly-servable app). Largely executed; see `docs/3-state/ProjectState.md`.
- [`PLAN_followups.md`](../../PLAN_followups.md) — deferred fixes and known issues, each with
  evidence. Partially executed (item 1 fixed 2026-08-25); check it before assuming an item is
  still open.
- [`PLAN_receiving_ledger.md`](../../PLAN_receiving_ledger.md) — the plan behind roadmap M6
  (perpetual inventory ledger). Not started.

## Other reference material at the repo root

- [`BUILD_INSTRUCTIONS.md`](../../BUILD_INSTRUCTIONS.md) — how to build the distributable app.
- [`Set password.bat`](../../Set%20password.bat) — resets the admin password on a packaged copy.

## Conventions

- **Cite claims to `file:line`.** Every doc in this tree that makes a factual claim about the
  code links to where that claim can be checked.
- **`docs/plans/`** — a plan for specific new work, live until executed, then archived. Empty
  today; new planning docs for *this* documentation tree (as opposed to the root-level `PLAN_*`
  files, which predate and remain outside this structure) belong here.
- **`docs/archive/`** — inert documents, with a `README.md` explaining why each is inert. Empty
  today; nothing has gone inert yet.
- **`docs/generated/`** — tool-produced deliverables (e.g. `report.html` output). Empty in this
  tree today; the existing `report.html` at the repo root predates this structure and was left
  in place rather than moved, per this scaffold's constraint not to move existing files.
- **`docs/README.md`** (one level up from here) is the short, plain-English entry point for
  people; this file is the full index.
