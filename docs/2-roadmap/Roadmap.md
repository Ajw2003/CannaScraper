# Roadmap

Milestones, each with a rough completion percentage, what it is, what it contains, and the
acceptance criterion that makes "done" checkable. Percentages and status are informed by
`git log` (most recent: `b567045` merge of branch `ApiFork`), the README, and the `PLAN_*.md`
documents at the repo root, cross-checked against the code where cited.

---

## M1 — Core scraper: catalog + store registry + browser fetch — 100%

**What it is.** The original tool: pull the public catalog, parse the store registry, sweep
stores with a real browser (age gate, store switching, per-store extraction), write to
SQLite/CSV.

**Contains.** `catalog.py`, `stores.py`, `browser.py`, `scrape.py`, `db.py` (pre-split schema),
`main.py`, `config.py`, `watchlist.txt`.

**Acceptance.** `python selftest.py` passes; `python main.py --limit 3` produces a
`results.csv` with real per-store data. Confirmed shipped and in active use per the README's
"Quick start" section (`README.md:24-47`).

---

## M2 — API fetcher (fast path) — 100%

**What it is.** A second backend that reads the site's own `scan-single-item`/
`scan-multiple-items` API responses instead of driving a browser, ~5-6× faster.

**Contains.** `fetchers/api_fetcher.py`, `fetchers/__init__.py`, the `store_id_match` /
hub-pricing detection, the two bugs fixed 2026-08-25 (`PLAN_followups.md:9-42`).

**Acceptance.** `main.py --product <sku> --top 8 --compare` runs both backends over the same
stores and reports agreement (`README.md:663-671`); `verify_scan_fix.py` passes
(`PLAN_followups.md:14-15`).

---

## M3 — Rate limiting, egress pool & durable index builder — 100%

**What it is.** Empirically-measured rate limiting (`ratelimit.py`, `ratelimit_probe.py`), a
multi-route egress pool for throughput (`egress.py`), and a crash-durable, resumable
province-wide index build on a SQLite work queue (`index_builder.py`, `workqueue.py`).

**Contains.** See `docs/4-systems/fetchers-and-rate-limiting.md` and
`docs/4-systems/index-builder-and-work-queue.md`.

**Acceptance.** `ratelimit_probe.py` has produced a dated empirical result
(297 requests, 2026-08-25 — `README.md:628-647`); `index_builder.py --probe-egress` gives a
verdict per configured pool; a full province index build completes and closes out stale rows
(`README.md:446-451`). Landed via commit `dc20f43` ("index: fan out across an egress pool with a
durable work queue").

---

## M4 — Normalized schema (obs/products/store_meta split) — 100%

**What it is.** Splitting the single observations table into `obs` + `products` + `store_meta`
behind an `observations` view, cutting file size from 203 MB to 97 MB.

**Contains.** `db.py`, `normalize_db.py`.

**Acceptance.** `normalize_db.py` backs up, verifies all 34 columns of all rows against the
pre-split table, and only then swaps — this check has been run and is described as verified in
`README.md:192-194`. Landed via commit `9abd4b9` ("migrate app over to new db format").

---

## M5 — Packaged, publicly-servable desktop app — ~95%

**What it is.** Ship the tool as a `.exe` anyone can run with no Python, serving a public URL via
Cloudflare tunnel, with an admin password gating scraping actions and a build-freshness check
(`PLAN_app_distribution.md`).

**Contains.** `app.py`, `server.py`, `auth.py`, `tunnel.py`, `jobs.py`, `build.ps1`,
`CannaCabana.spec`, `buildinfo.py`, `paths.py`, `Set password.bat`.

**Acceptance** (from `PLAN_app_distribution.md`'s three gaps — distributable, in-app province
refresh, public not just LAN — all satisfied): `build.ps1` runs to a green PASS on every step,
including the packaged exe passing `--selftest` (`build.ps1:8-10`); the exe prints local/LAN/
public URLs and an admin password on first run (`README.md:232-244`); a province refresh is
reachable from the UI, not CLI-only. The remaining ~5% is the build-staleness stamping edge
cases and any not-yet-observed packaging regressions — commits `a0a2aaf` and `4c2cb87` show this
was still being actively fixed as recently as the last two commits ("fix exe launching a stale
build", "Fix search returning nothing in the packaged app").

---

## M6 — Receiving ledger (perpetual inventory) — 0%, planned only

**What it is.** Move from "observed by scraping" to "derived from owned events" — ingest
shipment-receiving and POS-sales events into a central ledger, with the scrape becoming a drift
audit rather than the source of truth, across all 225 stores / 5 provinces
(`PLAN_receiving_ledger.md:1-30`).

**Contains.** Not yet built. Plan only, at `PLAN_receiving_ledger.md`.

**Acceptance.** Per the plan: a repository seam such that Postgres is a backend swap, not a
rewrite; nightly off-machine backups and a named tunnel in place before real stores depend on it
(`PLAN_receiving_ledger.md:19-23`). No code exists yet — 0% is not a placeholder, it reflects
that this plan has not been started.

---

## M7 — Static GitHub Pages site + per-province GitHub Actions scrapers — 0%, exploratory, conditional

**What it is.** Move the public-facing product off a single machine's Cloudflare tunnel onto a
static GitHub Pages site fed by scheduled, per-province GitHub Actions jobs that publish scraped
data to a public repo — modelled on `Ajw2003/RockSkipping`'s `renew-cert.yml` pattern (scheduled
Action, repo-scoped token, publish to a public repo). Decided 2026-09-27; see
`docs/6-decisions/Decisions.md`.

**Contains.**
- `.github/workflows/runner-ip-test.yml` — first step, being built by another agent concurrently
  with this documentation pass. Checks whether cannacabana.com's catalog and stock API accept
  requests from GitHub Actions runner IPs. **Result not yet known as of this writing.**
- Not yet built: the per-province scrape workflows themselves, the publish-to-public-repo step,
  and the static Pages site that reads the published data.

**Acceptance.** Conditional, in order:
1. `runner-ip-test.yml` shows cannacabana.com's catalog/stock API accepts GitHub Actions runner
   IPs without being blocked. **If this fails, the milestone is blocked** — the whole approach
   depends on it.
2. A per-province scrape workflow runs on schedule and successfully publishes data to a public
   repo.
3. The CannaScraper repo is made public (required for free Actions minutes: a private repo caps
   at 2,000 min/month, and a daily all-province run is estimated at ~120 min/day — 3,600+
   min/month, well over the private cap).
4. A static Pages site reads and displays the published per-province data.

This milestone is **exploratory and not committed** beyond step 1 until the IP test result is
known.
