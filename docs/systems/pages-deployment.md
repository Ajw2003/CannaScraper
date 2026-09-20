# GitHub Pages per-province deployment

## What it owns

A second, always-on, read-only front door — "what's in stock, where, right now" per province —
published as static files on GitHub Pages, updated nightly by GitHub Actions, independent of
whether anyone's desktop app is running. Full design in
[docs/plans/github-pages-per-province.md](../plans/github-pages-per-province.md).

**Not yet working end to end** — see
[ProjectState.md](../ProjectState.md#the-one-thing-that-is-not-what-it-looks-like) for the two
blockers. This doc describes what each finished piece does; it does not mean the pipeline has run.

## How it works

**`CannaScraper/export_pages_json.py`** (149 lines) is a thin export layer on top of `db.py` /
`catalog.py` / `stores.py` — it doesn't modify any of them. `export_province()` reads
`db.latest_observations()`, filters to that province and `carried == 1`, and writes two files
through a temp-file-then-`os.replace()` pattern (export_pages_json.py:37-44) so a killed write
never leaves a truncated JSON file for the live site to fetch:

- `data/<slug>.json` — one row per (product, store) in stock: sku, store_id, qty, price,
  member_price, is_elite, thc, cbd. Deliberately thin — no title/brand/image, which live in the
  shared catalog file instead, keeping a full province to a few MB rather than tens of MB.
- `data/<slug>-meta.json` — build timestamp, store coverage, row count — what the site's "data as
  of" line reads.

`export_catalog()` (`--catalog-only`) writes the two **shared** files every province's page
needs: `data/catalog.json` (thinned product metadata) and `data/stores.json` (the full registry).
**No workflow currently calls this** — see Traps.

**`.github/workflows/_build-province.yml`** is the shared `workflow_call` logic: checkout `main`
with `working-directory: CannaScraper`, `pip install -r requirements.txt`, run
`index_builder.py --province <name> --db <throwaway>`, run `export_pages_json.py` against that
same throwaway DB, then checkout/create the `data` branch and push the new JSON (with a
fetch-rebase-retry loop in case two provinces' pushes land close together).

**Five thin per-province workflows** (`scrape-<province>.yml`) each carry their own staggered
cron (Alberta 2:00 UTC, Ontario 2:15, Saskatchewan 2:30, Manitoba 2:45, British Columbia 3:00) plus
`workflow_dispatch` for a manual "refresh now," and call `_build-province.yml` with their
province name.

**`.github/workflows/deploy-pages.yml`** triggers on any push to `data` or `workflow_dispatch`,
checks out `site/` from `main` and `data/*.json` from `data`, combines them into one output
directory, and publishes via `actions/upload-pages-artifact` + `actions/deploy-pages` — the
modern Actions-based deploy, not the legacy branch-build mode, so there's no 10-builds/hour soft
limit.

**`.github/workflows/heartbeat.yml`** runs weekly and does nothing but commit a timestamp file to
`main` — a backstop against GitHub's 60-day "no activity" auto-disable for a public repo's
scheduled workflows. The five province workflows already generate real activity every night
(a genuine commit, not just a run) via their push to `data`, so this is redundant under normal
operation and exists only for the pathological case where all five are stuck simultaneously.

**`CannaScraper/site/`** (`index.html` + `app.js`, 371 lines total) is a fully client-side,
read-only frontend — no login, no refresh/rebuild buttons, matching the fact that a Pages site
can't offer them. It ports two pieces of Python logic directly to JS, cited in its own comments:
`searchCatalog()` (site/app.js:42-70) mirrors `catalog.search()` (catalog.py:183), and
`nearest()`/`distanceKm()` (site/app.js:73-92) mirror `stores.nearest()` (stores.py:156). It fetches
`data/catalog.json` and `data/stores.json` once on load, then `data/<slug>.json` +
`data/<slug>-meta.json` on each province switch.

## Invariants

- Data lands on a dedicated `data` branch, never `main` — nightly bot commits of regenerated JSON
  must never mix into the project's real commit history.
- Each province's JSON stays **separate**, never combined into one Canada-wide file — a combined
  file would run 400–600k+ rows (tens of MB even trimmed), against Pages' 1 GB soft site-size
  limit and, more practically, a phone on cellular. Fetching only the selected province client-side
  keeps each request to a few MB.
- `export_pages_json.py` must be run against a **freshly-built, throwaway** SQLite file per
  province run (`$RUNNER_TEMP/history.db`), not a persisted one — a fresh index every night is
  what matches the desktop app's existing cadence and keeps a CI runner from needing to persist
  multi-hundred-MB state between runs.

## Traps

- **The pipeline cannot run at all right now.** `CannaScraper/` is still its own git repository
  (nested `.git`), so the outer repo can't see its files as trackable content, and
  `_build-province.yml`'s checkout of `main` would find `CannaScraper/` empty. See
  [Decisions.md](../Decisions.md#2026-09-20--cannascraper-still-has-its-own-git-the-outer-repo-cannot-track-its-files).
- **Even once that's fixed, the site would still fail to load.** No workflow ever calls
  `export_pages_json.py --catalog-only`, so `data/catalog.json` and `data/stores.json` — which
  `site/app.js` fetches before it can render anything — would never exist. The plan
  ([item 4](../plans/github-pages-per-province.md)) calls for a weekly catalog/stores refresh
  workflow; it was never written. `site/app.js:261-263` renders that fetch failure as a permanent
  "Could not load the catalog:" message, not a transient error.
- **The site's "top N" and "📍 Near me" controls don't refresh an already-open result table** —
  `showResults()` (site/app.js:164-212) never sets the `data-active` marker the `#top` change
  handler (site/app.js:235-238) and the geolocation callback (site/app.js:250-252) both look for.
  Changing "5 nearest" to "Whole province," or using location, after a product is already
  selected silently does nothing until the search box is used again.
- No `robots.txt` exists yet in `site/` — the plan recommends one so the published per-store
  pricing/stock data doesn't get indexed by search engines by accident, independent of the repo's
  public/private setting (Pages content is reachable by URL regardless of that setting on the
  Free plan).
