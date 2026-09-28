# Static site, one province end to end

Roadmap M7, step 2 onward. Goal: a scheduled GitHub Action scrapes **Saskatchewan**, publishes
the latest stock as JSON, and a static GitHub Pages page reads it. Saskatchewan first because
13 stores index in ~7 minutes, so each test cycle is short. Adding a province afterwards is one
line in the workflow's province list.

## Where things live

| What | Where | Why |
|---|---|---|
| Scrape history (`history-<slug>.db`) | Release asset on tag `scrape-history` in this repo | Keeps a growing binary out of git; survives between runs so resume, close-out ("sold out since last run") and failed-store fallback keep working. Same pattern as RockSkipping's certificate. |
| Latest stock (`data/<slug>.json`) | `gh-pages` branch, served by Pages | Browsers can read Pages files; release downloads have no CORS headers. |
| The page (`site/index.html`) | Source in `main`, copied to `gh-pages` on publish | One source of truth; the branch is build output. |

Why this repo and not a separate public data repo (the original plan): the repo went public on
2026-09-27, so the workflow's own `GITHUB_TOKEN` can write the branch and the release. A
separate repo would need a personal access token the user has to create and store.

`gh-pages` is rebuilt as a **single orphan commit** on every publish, so daily data never
accumulates in git history. The publish job starts from the current branch contents so a run
that scraped one province does not drop the others' files.

## Pieces

1. `ci/export_province.py` — reads the history DB, writes `data/<slug>.json`: stores (with
   coordinates from `stores.json`), products, and in-stock rows per SKU from
   `db.latest_observations` (freshest good row per store across runs). Exits non-zero if the
   province has no in-stock rows at all.
2. `site/index.html` — static page: province picker (only provinces with data), search by
   title/brand, product list, per-store table (qty, price, tier price, distance when the browser
   shares location), data age. No server calls.
3. `.github/workflows/scrape-province.yml` — `plan` job builds the province matrix (dispatch
   input, or the default list); `scrape` job per province restores history, runs
   `index_builder.py`, exports, uploads history; `publish` job assembles `gh-pages` and pushes.
   Runs hourly and on manual dispatch. *(Until 2026-09-28 it also ran on every push touching the
   pipeline or `site/`; that made each merge start a full ~50-minute scrape, so it was removed.
   Page-only changes now go live through `.github/workflows/publish-site.yml`, which republishes
   `site/` over the existing data without scraping.)*

## Out of scope for this step

Live re-check, admin login, rebuild button (need a server). *(Later done on 2026-09-28: all
five provinces, an hourly schedule, and pruning history to the current state each run — see
`docs/6-decisions/Decisions.md`.)*

## Needs the user once

GitHub Pages switched on for the `gh-pages` branch (Settings → Pages). A workflow token cannot
enable Pages itself.
