# Project state

**Headline: ~65% of the roadmap complete** (5 of 7 milestones essentially done; M6 not started,
M7 in progress: all five provinces scrape, publish and are served; hourly schedule waits on merging PR #4).

## Status table

| Milestone | Status | % |
|---|---|---|
| M1 — Core scraper (catalog/stores/browser) | Done | 100% |
| M2 — API fetcher (fast path) | Done | 100% |
| M3 — Rate limiting, egress pool, durable index builder | Done | 100% |
| M4 — Normalized schema | Done | 100% |
| M5 — Packaged, publicly-servable desktop app | Nearly done, actively stabilizing | ~95% |
| M6 — Receiving ledger (perpetual inventory) | Not started, plan exists | 0% |
| M7 — Static Pages + per-province Actions scrapers | All provinces live on Pages; schedule starts on merge | ~85% |

## Per-milestone detail

**M1-M4** are built, exercised by `selftest.py`, and referenced throughout the README as the
tool's normal operation (see `docs/2-roadmap/Roadmap.md` for citations). No open gaps found in
this pass.

**M5.** The exe builds, self-tests, and serves local/LAN/public URLs with password-gated writes.
The two most recent commits (`a0a2aaf`, `4c2cb87`) are bug fixes specifically to the packaged
build's freshness detection and search — i.e. real users hit real bugs in this milestone
recently, which is why it is not called 100%.

**M6.** `PLAN_receiving_ledger.md` is a detailed plan but no ledger code exists in the repo.
0% is accurate, not a placeholder.

**M7.** Decided 2026-09-27. Acceptance step 1 is **checked**: `.github/workflows/runner-ip-test.yml`
(run [36352242167](https://github.com/Ajw2003/CannaScraper/actions/runs/36352242167), 2026-09-27, runner IP 172.208.153.2) showed a GitHub runner gets clean answers from the catalog (HTTP 200) and the stock
API (HTTP 200, `x-ratelimit-remaining: 59`), and the real `index_builder.py` indexed 3
Saskatchewan stores (3,255 rows, 1.4 min, 0 failed, 0 retried). One gap: the store-locator page
answered **HTTP 503** from Shopify with `retry-after: 139` on that run, then **HTTP 200** on the
next ([36352432606](https://github.com/Ajw2003/CannaScraper/actions/runs/36352432606), a
different runner), which also repeated the 3-store index identically (3,255 rows, 0 failed) —
so the 503 looks transient. Scrapes read the committed `stores.json` and do not need the locator
anyway. Two short runs (66 API calls each) do not show a full province (~2,300 requests) stays
unblocked.
Everything downstream (per-province workflows, public repo, Pages site) is unbuilt.

M7 step 2, one province end to end (plan: `docs/plans/static-site-one-province.md`). Run [36353925298](https://github.com/Ajw2003/CannaScraper/actions/runs/36353925298)
(push-triggered from the feature branch) scraped all 13 Saskatchewan stores in 6.5 min, 0
failed, 15,171 in-stock rows; exported `data/saskatchewan.json` (1.45 MB, 2,270 products);
saved `history-saskatchewan.db` (6.3 MB) to the `scrape-history` release; pushed `gh-pages`.
The published branch, served locally and driven headless, searches and shows per-store stock
and member prices from that real data with no JS errors and no horizontal scroll at 390px
(`docs/generated/static-site-real-*.png`). **Not yet true:** nothing is served publicly — the
run warned that GitHub Pages is switched off; the daily schedule only fires once the workflow
is on the default branch; product thumbnails could not be checked (this sandbox cannot reach
`cdn.shopify.com`); a second run restoring history has not happened yet.

M7, all provinces (2026-09-28). Pages switched on by the user and confirmed in their browser at
https://ajw2003.github.io/CannaScraper/ (product images load). Run
[36363444274](https://github.com/Ajw2003/CannaScraper/actions/runs/36363444274) scraped all 225
stores with no failures — Alberta 92 (40 min), Ontario 100 (50 min), Saskatchewan 13, Manitoba
12, BC 8 — and its publish step confirmed the live site served the new data. History file sizes
after one run: Ontario 42.3 MB, Alberta 33.7 MB. Hourly schedule plus per-run pruning added
(`ci/prune_history.py`; decision 2026-09-28). Run
[36364459762](https://github.com/Ajw2003/CannaScraper/actions/runs/36364459762) pruned in CI:
Saskatchewan restored 11.9 MB and saved 6.06 MB after its third run. **Not yet true:** the
hourly cron only fires after PR [#4](https://github.com/Ajw2003/CannaScraper/pull/4) merges to
`main`; Alberta/Ontario had not yet been through a prune at the time of writing.

## The one thing that is not what it looks like

**The egress pool reads as a completed throughput feature, but ships disabled by default and
unverified for this specific site.** `egress.py` and `index_builder.py --probe-egress` are
fully implemented (M3, 100%), but `config.EGRESS_PROXIES` is empty by default, and the README is
explicit that nothing proves the site's rate counter is actually keyed on source IP
(`README.md:519-522`). Someone skimming the code would reasonably conclude "multi-route
throughput is a solved, working feature" — what is actually true is "the *mechanism* to test and
use that is solved; whether it helps against this specific site has not been demonstrated,
because no proxy routes are configured in this repo." Anyone deploying it for real is expected to
run `--probe-egress` against their own configured routes first.

## Cross-cutting issues that belong to no milestone

- **The repo went public on 2026-09-27.** That move with consequences for every other milestone's
  assumptions (e.g. `settings.json`/`egress_proxies` secrecy, `auth.py`'s password model) that
  no milestone's acceptance criterion currently re-checks. Worth flagging explicitly rather than
  letting it surface only inside M7.
- **THC/CBD per-lot variance** (`db.py:23-26`) and the broader "what belongs in `products` vs.
  per-observation" question is called out as an open follow-up in `PLAN_followups.md` and isn't
  owned by any single milestone above — it's a standing data-modeling concern that touches M4
  (schema) and M6 (ledger) both.
- **Bundled `cloudflared.exe` vs. named tunnel** — M5's default (`quick` tunnel) is fine for demo
  use but explicitly best-effort/no-SLA (`tunnel.py:9-14`); nothing in the roadmap currently
  tracks "get a named tunnel + token set up for a link that needs to keep working," which
  `PLAN_receiving_ledger.md` itself flags as a prerequisite before real stores depend on this
  (`PLAN_receiving_ledger.md:19-23`).
