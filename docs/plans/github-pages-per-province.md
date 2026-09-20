# CannaCabanaScraper → GitHub Pages, per-province

## Context

`focus-deck-app` is a pure client-side PWA — HTML/CSS/JS with no server — so GitHub Pages hosts it directly: a GitHub Actions workflow builds and deploys the repo's static files, and it's reachable from any device.

`CannaCabanaScraper` is a different shape. `CannaScraper/server.py` is a FastAPI app with 18 routes, a background job queue (`jobs.py`), a SQLite history database (`db.py`, currently 97 MB), a Playwright/browser fetcher, a Cloudflare tunnel for a public URL, and password-gated admin actions (rebuild the index, refresh live, change settings). None of that can run on GitHub Pages — Pages serves static files only, full stop. There is no server process, no database query at request time, no way to run Playwright, and no way to gate an action behind a password without a backend to check it against.

What *can* move to Pages is the read side: "what's in stock, where, right now" for a province. The existing `index_builder.py` already produces exactly that — a full per-store, per-product stock snapshot in SQLite — on a schedule (the README already documents a nightly `schtasks` job for this). The plan is to run that same build inside GitHub Actions instead of on your machine, export the result as static JSON, and publish it to Pages. The desktop app (`build.ps1` → `CannaCabana.exe`) keeps working exactly as it does today for live/local use; Pages becomes a second, always-on, read-only front door — the one you'd hand to Canna Cabana or to a friend, since it needs nothing installed and updates itself.

Per your answers: the repo goes public (data is published either way once it's on Pages, so there's no privacy gained by going private — see Limitations), scope is all five provinces the store registry currently reports (Alberta, Ontario, Saskatchewan, Manitoba, British Columbia) with each one as its own scheduled Action rather than one Canada-wide sweep, and refreshes are both scheduled (nightly, matching the current cadence) and manually triggerable per province from your phone.

## Architecture

```
                    ┌─ schedule (nightly, staggered) ─┐
                    │                                  │
scrape-alberta.yml ─┤                                  │
scrape-ontario.yml ─┼─→ calls _build-province.yml ─────┼─→ commits data/<province>.json
scrape-sk.yml ──────┤     (index_builder.py --fetcher   │   + data/<province>-meta.json
scrape-mb.yml ──────┤      api, then export_pages       │   to the `data` branch
scrape-bc.yml ──────┘      _json.py)                    │
                    └─ workflow_dispatch (manual) ──────┘
                                                          │
                                                          ▼
                                          deploy-pages.yml (on push to `data`,
                                          or chained after each build) merges
                                          site/ (static frontend, from `main`)
                                          with data/*.json (from `data` branch)
                                          and publishes via actions/deploy-pages
```

Five thin, near-identical per-province workflows so each one can be scheduled, disabled, or run by hand independently (a Saskatchewan failure doesn't touch Alberta's data), calling one shared reusable workflow that holds the actual build logic. Cron start times are staggered 15 minutes apart so two provinces never race to commit at once.

Data lands on a dedicated `data` branch, not `main` — daily bot commits of regenerated JSON never mix into the project's real commit history, and `main` stays exactly as clean as any other code change. The Pages deploy step checks out both branches and publishes their union.

## What gets built

**1. `CannaScraper/export_pages_json.py`** (new) — given a province and a freshly-built SQLite file, calls `db.latest_observations()` (already exists, `db.py:351`) filtered to `carried=1`, and writes:
- `data/<province-slug>.json` — one row per (product, store) currently in stock: sku, store_id, qty, price, member_price, is_elite, thc/cbd. Kept intentionally thin (no title/brand/image — those live in the shared catalog file) so a full-province file stays a few MB, not tens of MB.
- `data/<province-slug>-meta.json` — build timestamp, store coverage count, run duration — what today's HTML report banner shows, reused for the "data as of" line on the page.
- `data/catalog.json` and `data/stores.json` (built once, refreshed weekly rather than nightly — the catalog and store registry "changes rarely" per `catalog.py`/`stores.py`'s existing cache comments) — product metadata and the 225-store registry, shared across all five provinces.

This is a thin export layer on top of code that already exists; it does not touch `catalog.py`, `stores.py`, `db.py`, or `index_builder.py`.

**2. `.github/workflows/_build-province.yml`** (new, `workflow_call`) — the shared logic:
1. Checkout `main` (code) 
2. `pip install -r requirements.txt` (no Playwright install — the default `--fetcher api` path needs no browser, so this stays fast and simple)
3. `python index_builder.py --province <input> --fetcher api` into a throwaway SQLite in the runner (no need to persist the DB between runs — a fresh build every night matches current behavior)
4. `python export_pages_json.py --province <input> --db <throwaway path>`
5. Checkout/create the `data` branch, copy in the new JSON, commit, push (with a pull-rebase retry in case two provinces' pushes land close together)

**3. Five thin workflows** — `.github/workflows/scrape-alberta.yml`, `scrape-ontario.yml`, `scrape-saskatchewan.yml`, `scrape-manitoba.yml`, `scrape-british-columbia.yml`. Each is ~10 lines: a `schedule:` cron (staggered), a `workflow_dispatch:` trigger (this is what gives you the manual "refresh now" button in the GitHub mobile app or `gh workflow run scrape-alberta.yml`), and a call to `_build-province.yml` with that province's name. Adding a sixth province later (if Canna Cabana expands) is copy one file, not touching the shared logic.

**4. `.github/workflows/deploy-pages.yml`** (new) — triggered on push to the `data` branch (so any province's refresh republishes automatically) plus `workflow_dispatch`. Checks out `site/` from `main` and `data/*.json` from `data`, combines them into one output directory, and publishes with `actions/upload-pages-artifact` + `actions/deploy-pages` — the modern Actions-based deploy, not the legacy "Pages builds from a branch" mode, so there's no 10-builds/hour soft limit to worry about (that limit is explicitly waived for a custom Actions workflow deploy).

**5. `CannaScraper/site/`** (new folder) — a static counterpart to `web/index.html`, not a port of the whole file (most of it — login, catalog rebuild, index build/cancel/resume — has no meaning without a server). It keeps: province picker, product search (client-side, over `catalog.json`), nearest-store math (client-side, over `stores.json` + geolocation — reused logic from `stores.nearest()`, `stores.py:156`, translated to JS), and the results table (reused logic from `catalog.search()`, `catalog.py:183`). No login, no refresh/rebuild buttons — the published site is read-only for everyone, which is simpler than today's password-gated split and matches what a Pages site can actually offer. A "data last refreshed: Alberta 6h ago · Ontario 6h ago · …" line reads the five `-meta.json` files.

**6. `.github/workflows/heartbeat.yml`** (new) — see "Keeping the schedule alive indefinitely" below.

**7. `CannaScraper/.gitignore`** (new — the project isn't a git repo yet) — excludes `history.db`, `.venv/`, `dist/`, `build/`, `raw/`, `*.csv`, cache JSON files, `settings.json` (has the admin password hash and any egress proxy credentials).

## Keeping the schedule alive indefinitely

GitHub auto-disables a **public repo's `schedule:`-triggered workflows after 60 days with no repository activity** — the actual failure mode to design against, since "I don't want to babysit this" means the nightly builds must not be able to quietly stop on their own. Two things make that essentially a non-issue here rather than something to watch for by hand:

- **The pipeline already generates activity every night.** Each province workflow ends with a `git push` to the `data` branch — a real commit, not just a workflow run — and that happens five times a night across staggered schedules. For the whole repo to go 60 days with zero activity, every one of the five nightly crons would have to fail to fire *and* fail to push, simultaneously, for two months straight. A single missed night (GitHub's documented "delayed under high load" case) doesn't come close to that.
- **A dedicated heartbeat as a backstop.** `.github/workflows/heartbeat.yml` runs weekly (8× the safety margin under the 60-day window) and does nothing but bump a timestamp file on `main` and commit it — a few seconds of Actions time. It exists purely so that even in a pathological scenario where all five province workflows are somehow stuck for an extended stretch, something is still reliably touching the repo. It has no dependency on the scraper, the data branch, or anything that could itself be the thing that's broken.

Between the two, keeping this running requires no recurring action from you. The one tail risk genuinely outside this design's control is GitHub Actions itself having an outage lasting close to two months — at which point re-enabling a workflow is a single click on the repo's Actions tab, not a rebuild. Turning on email notifications for workflow-run failures (repo Settings → Notifications) is worth doing once, so you'd hear about a real problem (e.g. Canna Cabana changing their site and every province run failing) passively instead of only noticing when you next open the page.

## Setup steps (once the above is written)

1. `git init`, add the `.gitignore`, initial commit
2. Create the GitHub repo (public, matching your answer) — `gh repo create`
3. Push `main`
4. Create an empty `data` branch (orphan, no shared history with `main`)
5. Enable Pages in repo settings, source = "GitHub Actions"
6. Push the five province workflows + `_build-province.yml` + `deploy-pages.yml` — this alone doesn't run anything until a schedule fires or you dispatch one manually
7. Manually dispatch `scrape-alberta.yml` once (`gh workflow run scrape-alberta.yml`) to prove the pipeline end to end before waiting for the cron

## Limitations and restrictions

**No live checks from the published site, ever.** This is the one restriction the plan can't design around — Pages is static. "Check live now" and the admin-gated index rebuild stay desktop-app-only features. The published site is only ever as fresh as the last successful Action run for that province (nightly, or whenever you manually dispatch one).

**The data is public, regardless of repo visibility.** A Pages site's URL is reachable by anyone who has it — private-repo access restriction (limiting the published site itself to signed-in collaborators) is a GitHub Pro/Team/Enterprise feature, not available on the Free plan. Since you're going public repo + public data, this isn't a behavior change, just worth having stated plainly: Canna Cabana's per-store pricing and stock levels become visible to anyone with the link, indexable by search engines unless you add a `robots.txt` disallowing it (recommended either way, so it doesn't show up in Google results by accident).

**Actions minutes.** Public repos get unlimited free minutes on standard runners, so this is a non-issue at the current scope. If the repo ever needs to go private, the Free plan's private-repo allowance is 2,000 minutes/month — Alberta alone (~45 min) run nightly is ~1,350 min/month; running all five provinces nightly would be several times that and would need either a cheaper cadence (every-other-day, weekly for the smaller provinces) or a paid plan.

**Scheduled workflows can, in principle, silently stop.** GitHub auto-disables a public repo's `schedule:` workflows after 60 days with no repository activity, and delivery of the `schedule` event is best-effort (can be delayed under high platform load, especially at the top of the hour — the five provinces' staggered, off-the-hour start times dodge that). See "Keeping the schedule alive indefinitely" above for how the design makes the 60-day case a non-issue without any recurring action from you.

**Payload size, and why it's chunked per province.** A full province's in-stock snapshot is roughly 90–120k rows (per the README's own index-builder numbers). Shipped as one combined Canada-wide file that would be 400–600k+ rows — several tens of MB even after trimming fields, which starts to matter against Pages' 1 GB soft site-size limit and, more practically, against a phone loading it on cellular. Keeping one JSON per province (and fetching only the selected province client-side) keeps each request to a few MB, well within the 100 GB/month soft bandwidth limit even at real usage.

**Scraper origin changes.** Requests will run from GitHub-hosted runner IPs (Azure datacenter ranges) instead of your home connection. The site's rate limiter (60 req/min, measured in the README) is almost certainly IP-keyed, so this shouldn't trip anything the current tool doesn't already handle — but a shared, well-known IP range is more likely to be caught by broader anti-bot infrastructure than a residential address, and if a range does get flagged, other unrelated GitHub Actions users on the same range could theoretically be affected too. Worth a quiet first run before trusting the nightly schedule.

**No historical trend data on the Pages site.** This plan ships only the *latest* snapshot per province (matching the current `--fetcher api` live-index behavior), not the full time-series `history.db` holds locally. Nothing stops adding a "price over time" view later, but it would mean either persisting a growing database between Action runs (via `actions/cache` or an external store) or committing daily snapshots instead of overwriting — out of scope for this pass unless you want it.

**`server.py`'s write endpoints have no static equivalent, by design.** `/api/refresh`, `/api/index/{province}`, `/api/catalog/refresh`, `/api/login` — none of these exist in the Pages version. That's not a gap to fill; it's the correct shape for a read-only public mirror. If Canna Cabana themselves ever wanted the live/admin functionality, that would need actual hosting (a small always-on server), which is a different, later conversation.

## Verification

- After the pipeline is wired up: `gh workflow run scrape-alberta.yml`, watch it in the Actions tab, confirm `data` branch gets a new `alberta.json` + `alberta-meta.json` commit, confirm `deploy-pages.yml` fires off that push and the Pages URL serves the updated "data last refreshed" timestamp.
- Open the Pages URL on a phone (real device, not just resized desktop browser) and confirm the 📍 near-me button works — this is genuinely better than today's LAN setup, since Pages is HTTPS by default and browsers refuse geolocation over plain HTTP.
- Confirm the site works with JavaScript network throttled to "Slow 3G" in devtools, given the per-province JSON is the biggest asset on the page.
- Run `python export_pages_json.py --province Alberta` locally once against an existing local `history.db` before it ever runs in CI, to catch schema mismatches early rather than debugging inside a GitHub Actions log.
