# Roadmap

What 0–100% means for this project, and what "done" looks like for each milestone. See
[ProjectState.md](ProjectState.md) for where things actually stand against this right now.

---

## 1. Core scraper — 100%

Pull a comparable price/stock dataset for every Canna Cabana store in a province, without paid
APIs or accounts.

**Contains:** the store registry parser (`stores.py`), the public catalog reader (`catalog.py`),
both fetch backends (browser DOM scrape via `browser.py`/`scrape.py`, and the faster
unauthenticated API backend via `fetchers/api_fetcher.py`), the SQLite history store (`db.py`),
and the `main.py` CLI orchestration.

**Acceptance:** `python main.py --limit 3` produces rows with real prices and no errors, and
`python main.py --product <sku> --top 8 --compare` shows the browser and API backends agreeing
on price, member price, stock, carried, and available for the same stores. Checked — this is the
project's original, longest-running mode of operation, exercised nightly via Task Scheduler per
the README.

---

## 2. Full-province stock index — 100%

Index every in-stock product at every store in a province in one pass, so a lookup afterward is
instant instead of triggering a live scrape.

**Contains:** `index_builder.py` (paged `product/search` walk, `_close_out()` for items that
dropped out of stock), `workqueue.py` (durable per-store claims), `egress.py` (optional
multi-route throughput), `ratelimit.py` (budget tracking).

**Acceptance:** a full province build completes and populates `history.db` such that
`db.latest_observations()` answers instantly with no further network calls; a re-index correctly
zeroes out items that were in stock last run and are absent now (verified in the README by
planting a stale row and confirming a re-index closes it out). Checked, and in continuous nightly
use per the README's `schtasks` job.

---

## 3. Desktop app distribution — 100%

Ship the tool as something anyone with Windows can run with no Python, no setup, reachable
publicly over a link.

**Contains:** `paths.py` (bundled vs. `%LOCALAPPDATA%` data dir), `jobs.py` (serialized
background worker for index builds and live checks), `auth.py` (password gate on
scrape-triggering actions), `tunnel.py` (Cloudflare quick/named tunnel), `app.py` (the exe entry
point), `build.ps1` + `CannaCabana.spec` (PyInstaller packaging).

**Acceptance:** `.\build.ps1` produces `dist\CannaCabana\`, running the exe from a different
machine/profile with no Python installed serves local + LAN + public URLs, geolocation works over
the tunnel, and writes are correctly gated behind the admin password while reads stay open. This
was the subject of [PLAN_app_distribution.md](../CannaScraper/PLAN_app_distribution.md), whose
own verification steps (11, including a fresh-machine run) define what "done" means here; see
[Decisions.md](Decisions.md#2026-08-23--ship-as-a-distributable-desktop-app-cloudflare-tunnel-open-reads--password-gated-writes).

---

## 4. Keep the exe from silently drifting behind source — in progress

Make it impossible for the packaged exe to quietly run stale code without anyone noticing (the
concrete failure this milestone exists to prevent: search returning nothing because the running
exe predated a fix by four days, with no signal anywhere that it was out of date).

**Contains:** `buildinfo.py` (build stamp + staleness check via source mtime), a `build.ps1` step
to refresh the catalogue and write the stamp before bundling, `GET /api/build/status` and
`POST /api/build/rebuild` on `server.py`, a stale-build banner in `web/index.html`, and
`rebuild.ps1` (detached helper: wait for exit → rebuild → relaunch).

**Acceptance:** per
[docs/plans/fix-search-and-build-staleness.md](plans/fix-search-and-build-staleness.md) —
touching a source file and reloading shows the stale banner and `/api/build/status` reports
`stale: true`; clicking **Rebuild and restart** exits the app, rebuilds, and relaunches on its
own; `selftest.py` gains checks for both. **Not yet checked** — the plan is written and its
verification steps are explicitly marked `UNTESTED:`; see
[ProjectState.md](ProjectState.md#4-keep-the-exe-from-drifting-behind-source).

---

## 5. GitHub Pages per-province mirror — in progress

A second, always-on, read-only front door for "what's in stock, where, right now" per province —
no install, reachable from any device, updated nightly by GitHub Actions, independent of whether
anyone's desktop app is running.

**Contains:** `CannaScraper/export_pages_json.py` (thin JSON export from a freshly-built SQLite),
`.github/workflows/_build-province.yml` (shared build-and-push logic), five per-province
workflows (`scrape-<province>.yml`), `.github/workflows/deploy-pages.yml`,
`.github/workflows/heartbeat.yml` (60-day schedule-disable backstop), and
`CannaScraper/site/` (the static, read-only frontend).

**Acceptance:** per
[docs/plans/github-pages-per-province.md](plans/github-pages-per-province.md)'s Verification
section — a manually dispatched province workflow produces a `data` branch commit with that
province's JSON, `deploy-pages.yml` republishes automatically, the Pages URL serves the updated
"data as of" line, and the near-me button works on a real phone. **Not yet checked, and not yet
possible to check** — see
[ProjectState.md](ProjectState.md#5-github-pages-per-province-mirror) for why the pipeline as
currently staged cannot run end to end yet.

---

## 6. Shipment receiving → central stock ledger — not started

Replace "ask the website" with a real perpetual-inventory ledger derived from shipment receiving
and POS sales events, with the scrape demoted to a periodic drift audit against the ledger.

**Contains:** a new `inventory/` package (`repo.py`, `ledger.py`, `identity.py`, `receiving.py`,
`pos.py`, `reconcile.py`, `projection.py`, `routes.py`), new `stock_ledger` /
`stock_on_hand` / `product_alias` / `shipment` schema in `db.py`, `web/receive.html` (offline-first
scanner PWA), and per-store device credentials.

**Acceptance:** per
[PLAN_receiving_ledger.md](../CannaScraper/PLAN_receiving_ledger.md)'s Verification section (7
checks: idempotent replay, projection into the existing UI with zero UI changes, the close-out
guard, coverage, cache invalidation, offline replay, and an end-to-end watchable script) — none
run yet; no `inventory/` package exists in the tree. This is a large, phased plan (7 phases) and
explicitly assumes off-machine backups and a named tunnel are in place before Phase 5 (POS
ingest) — an operational precondition, not a coding one.

---

## Milestones considered and left out of this list

None — the six above cover every plan and every implemented subsystem found in the repo as of
2026-09-20.
