# Project State

As of 2026-09-20. See [Roadmap.md](Roadmap.md) for what each milestone means and its acceptance
criterion.

**Headline: ~70% against the roadmap.** The core scraper, the province index, and desktop
distribution are done and in real nightly use. The two most recently-worked milestones — exe
drift prevention and the GitHub Pages mirror — both have real, matching code on disk, but neither
has been proven to work end to end yet, for two different reasons below.

## Status by milestone

| # | Milestone | Status | % |
|---|---|---|---|
| 1 | Core scraper | Done, in continuous use | 100% |
| 2 | Full-province stock index | Done, in continuous use | 100% |
| 3 | Desktop app distribution | Done, verified fresh-machine | 100% |
| 4 | Keep the exe from drifting behind source | Code written and built once; plan's own verification never run | ~70% |
| 5 | GitHub Pages per-province mirror | All pieces exist; pipeline cannot complete an end-to-end run yet | ~55% |
| 6 | Receiving → central stock ledger | Not started | 0% |

## The one thing that is not what it looks like

**The GitHub Pages deployment reads as "ready to turn on" — every file the plan called for
exists — but it cannot currently produce a working site, for two independent reasons, neither
visible from a file listing:**

1. **`CannaScraper/` is still its own git repository** (nested `.git`, remote
   `github.com/Ajw2003/CannaScraper`, branch `ApiFork`). The outer repo's `git status` reports
   `CannaScraper/` as one untracked entry, not ~60 tracked files. `.github/workflows/_build-province.yml`
   checks out the outer repo's `main` and immediately runs `pip install -r requirements.txt` and
   `python index_builder.py` inside `CannaScraper/` — which requires those files to actually be
   present in that checkout. They won't be: a nested `.git` makes git store the outer repo's
   reference to `CannaScraper/` as an empty gitlink, and there is no `.gitmodules` telling
   `actions/checkout` to fetch it as a submodule either. **Every one of the five province
   workflows would fail at the `pip install` step**, on the first run. See
   [Decisions.md](Decisions.md#2026-09-20--cannascraper-still-has-its-own-git-the-outer-repo-cannot-track-its-files).

2. **Even with #1 fixed, the site would still fail to load anything.** `site/app.js` needs
   `data/catalog.json` and `data/stores.json` (product metadata and the store registry) before it
   can render a single search result — `export_pages_json.py --catalog-only` is what produces
   them. No workflow calls it: the repo has exactly the 8 workflow files the
   [per-province plan](plans/github-pages-per-province.md) describes for province builds, deploy,
   and the heartbeat, but the plan's own item 4 (a weekly catalog/stores refresh) was never
   written. A visitor to the deployed site would see "Could not load the catalog:" forever, not a
   missing-data message — `site/app.js:261-263` renders that fetch failure as its permanent empty
   state.

Both gaps are small (a git restructure; one missing ~10-line workflow file plus wiring it into
`deploy-pages.yml`'s data sources), but until both are closed, dispatching any of the five
province workflows right now will fail on the very first step.

A smaller version of the same pattern shows up in milestone 4: `buildinfo.json` on disk
(`CannaScraper/buildinfo.json`, `built_at: 2026-09-01T04:59:28Z`, `commit: 4c2cb87`) proves the
stamping mechanism has run at least once and the exe has been rebuilt since the search fix — but
[the plan's own Verification section](plans/fix-search-and-build-staleness.md#verification) still
has every step marked `UNTESTED:`. The staleness banner, the rebuild button, and the new
selftest checks may all work — the stamping half is proven, the interactive half just hasn't
been exercised and confirmed.

## Milestone detail

### 1. Core scraper — done

Both fetch backends (`fetchers/api_fetcher.py`, `browser.py`/`scrape.py`) are cross-verified
against each other (`main.py --compare`) and match on price, member price, stock, carried, and
available. In continuous nightly use per the README's documented `schtasks` job. See
[docs/systems/scraping-and-fetching.md](systems/scraping-and-fetching.md).

### 2. Full-province stock index — done

`index_builder.py` + `workqueue.py` build a full province (~92–100 stores) in ~45 minutes on a
single route, faster with more egress routes. `_close_out()` correctly zeroes items that fell out
of stock; verified in the README by planting a stale row and confirming a re-index closes it.
See [docs/systems/indexing-pipeline.md](systems/indexing-pipeline.md).

### 3. Desktop app distribution — done

`build.ps1` → `dist/CannaCabana/` is a real, working artifact — confirmed running from a
different machine/profile with no Python installed, serving local + LAN + a public tunnel URL,
open reads and password-gated writes. See
[docs/systems/desktop-packaging.md](systems/desktop-packaging.md).

### 4. Keep the exe from drifting behind source

The code this plan called for is written: `buildinfo.py`, `rebuild.ps1`, the `build.ps1` stamping
step, `GET /api/build/status` / `POST /api/build/rebuild` on `server.py`, and the stale-build
banner in `web/index.html` all exist and match the plan
([docs/plans/fix-search-and-build-staleness.md](plans/fix-search-and-build-staleness.md)). The
exe has been rebuilt at least once carrying a build stamp (`buildinfo.json`, Sep 1). What hasn't
happened: the plan's own four verification steps (rebuild proves green; the search-latency
regression check; a browser-driven check of the actual complaint; the stale-banner-then-rebuild
round trip) are all still marked `UNTESTED:` in the plan doc itself. Until one of those runs,
"the exe silently predates the source" — the exact failure this milestone exists to catch —
remains unproven to be caught.

### 5. GitHub Pages per-province mirror

All the pieces called for in
[docs/plans/github-pages-per-province.md](plans/github-pages-per-province.md) exist on disk:
`CannaScraper/export_pages_json.py`, `CannaScraper/site/` (`index.html` + `app.js`, a full
client-side port of catalog search and nearest-store ranking), and all 8 workflow files
(`_build-province.yml`, five `scrape-<province>.yml`, `deploy-pages.yml`, `heartbeat.yml`). None
of it has run — the repo has no commits yet (`git status`: "No commits yet"), no GitHub remote is
configured, and the two blockers in "the one thing that is not what it looks like" above would
stop it even after a first push. See [docs/systems/pages-deployment.md](systems/pages-deployment.md).

### 6. Shipment receiving → central stock ledger — not started

[PLAN_receiving_ledger.md](../CannaScraper/PLAN_receiving_ledger.md) is a fully-designed,
7-phase plan (schema, ledger writer, identity resolution, receiving routes, offline scanner PWA,
POS ingest, reconciliation, Postgres cutover). No `inventory/` package exists in the tree yet;
Phase 1 (schema + `repo.py` + `ledger.py` + `projection.py`) is the next actionable step whenever
this is picked up. Explicitly assumes off-machine backups and a named tunnel are in place before
Phase 5 (POS ingest) — an operational precondition on the project owner, not a coding task.

## Cross-cutting issues that belong to no milestone

- **THC/CBD "over 100 = must be milligrams" fallback is still unreliable.**
  [Decisions.md](Decisions.md#2026-08-25--thccbd-potency-shown-as-a-consensus-range-not-max)
  fixed the headline-potency display with a consensus-range rule, but `server.potency()`'s
  magnitude fallback is still reachable with a single raw value and can still mislabel one (e.g.
  a legitimate 10x-corrupted reading). Recorded in `CannaScraper/PLAN_followups.md` item 7,
  not urgent because the consensus rule already hides it in practice — the wrong value has to win
  a majority vote to surface, and currently never does.
- **`run_id`/`scraped_at` denormalization** (`PLAN_followups.md` item 3) and **`stock_text`
  derivability** (item 4) are both measured, understood, and deliberately deferred — recovering
  ~18 MB combined, deferred so each schema change stays independently attributable if something
  regresses.
- **`site/app.js`'s "top N" and "near me" controls don't re-render after a result is already
  showing.** `showResults()` (`site/app.js:164-212`) never sets the `data-active` attribute or
  `.active` class that the `#top` change handler (`site/app.js:235-238`) and the geolocation
  callback (`site/app.js:250-252`) both look for — so changing "5 nearest" to "Whole province," or
  using 📍 Near me, after a product is already selected, silently does nothing until the search box
  is used again. Small, but worth fixing alongside whatever closes gap #2 above, since both touch
  the same file.
- **No `robots.txt` in `CannaScraper/site/`.** The per-province plan recommends one "either way, so
  it doesn't show up in Google results by accident" — not present yet.
- **Diagnostic/probe scripts live at the `CannaScraper/` root next to real systems.**
  `payload_probe.py`, `ratelimit_probe.py`, `scan_ceiling_probe.py`, and `verify_scan_fix.py` are
  each a one-off, already-answered investigation (see
  [docs/systems/diagnostics.md](systems/diagnostics.md)) rather than a live dependency of
  anything else. Not urgent to move, but worth knowing they're historical evidence, not
  maintained tooling, if one of them ever looks like it needs updating for a site change.
