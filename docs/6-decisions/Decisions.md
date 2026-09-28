# Decisions

A running, dated, append-mostly log of what was decided, when, why, and what it replaced. Newest
entry at the top. Entries are never rewritten or deleted; the only edit an existing entry gets is
flipping its `Status` line to `Superseded`, pointing at the entry that replaced it.

---

## 2026-09-28 — Pages site dropped features without saying so

**Context.** After the move to GitHub Pages the user noticed the site "feels like it's missing a
few things". An audit (`docs/generated/pages-audit/index.html`) found 10 features lost that a
static site could have kept, 2 changed for the worse and 2 new bugs, none of which had been
disclosed. Only 3 server-only losses had been named. Worst case: search for "pre-roll" went from
232 in-stock results to 7.

**Decision.** Treat it as a process failure and record it as input for a house-rules guard:
`docs/plans/house-rules-guard-silent-feature-loss.md`. A replacement, even as a new file, gets a
feature inventory of the original from its code, shown to the user as keep / change / drop, and
is verified against the original with the same inputs.

**Why.** The spec for the new page was written from memory, verification only compared the new
page with itself, and the existing rewrite rule (`edit-place.md`) only covers rewriting an
existing file, not a new file that replaces one. Restoring the lost features is a separate,
still-open piece of work.

**Status.** Standing.

## 2026-09-28 — One workflow run per province, started by an orchestrator

**Context.** All five provinces ran as one matrix in `scrape-province.yml`, so the published data
only moved when the slowest province (Ontario, 50–75 min depending on how fast the site answers)
finished, and every push touching the pipeline or `site/` started a full scrape. Run 36368189008
showed every province ~1.6× slower than the run before with identical request counts and no
retries — the site's response time, not our pacing — which pushed Alberta/Ontario past an hour.

**Decision.** Split into `scrape-one.yml` (one province end to end, including its own publish;
per-province concurrency group) and `scrape-all.yml` (hourly orchestrator calling it per
province, parallel by default or sequential on request, for all or a chosen list). No workflow
scrapes on push; page-only changes go through `publish-site.yml`. Every write to `gh-pages` goes
through `ci/publish_gh_pages.sh`, which pushes with `--force-with-lease` against the commit it
built from and retries on refusal.

**Why.** The user asked for per-province runs behind one central job that can run them either
way. Per-province concurrency keeps a province from overlapping itself (two runs would restore
the same history and the second save would drop the first's rows) without making small
provinces wait on Ontario. The lease replaces a plain force-push, which with independent
publishers would let the last one erase the others' data; tested with three provinces
publishing at the same moment into a local bare repo: all three landed (two after one retry)
and `gh-pages` stayed one commit. Rejected: a shared publish concurrency group, because GitHub
keeps only one waiting run per group and would silently cancel a queued province's publish.

**Status.** Standing.

## 2026-09-28 — Scrape hourly; history keeps only the current state

**Context.** The user asked for hourly runs once the repo was public. Each run appends a full
copy of every in-stock row to the province's history DB: measured on Saskatchewan's second run
(run 36363444274), 6.3 MB → 11.9 MB, i.e. ~5.6 MB for 15,148 rows. Ontario has ~8× the rows,
so an estimated ~40 MB per run, ~1 GB/day hourly. The DB is stored as a GitHub release file,
capped at 2 GB.

**Decision.** Run hourly (`cron: '23 * * * *'`) and prune each province's DB after every run
to the newest good row per (sku, store) — `ci/prune_history.py`. Past price/stock history is
not kept.

**Why.** The site, index_builder's sold-out close-out, and the failed-store fallback all read
only `db.latest_observations()`, and the prune provably leaves that unchanged (the script
compares before/after and refuses otherwise; on the real Saskatchewan DB it removed 15,171 of
30,342 rows, 11.9 → 6.0 MB, with an identical export). Rejected: hourly plus 14 daily snapshots
(~0.6 GB moved every hour for a history nothing reads yet); daily with 30 days (~1.2 GB, data
up to a day old). Chosen by the user from those three options.

**Status.** Standing.

## 2026-09-27 — Runner IP test passed; the Actions direction goes ahead

**Context.** The 2026-09-27 "static Pages + per-province Actions" entry below was conditional on
cannacabana.com serving GitHub Actions runners.

**Decision.** The condition is met; proceed to a per-province scrape workflow. Treat the store
locator as optional on runners: scrapes use the committed `stores.json`.

**Why.** Run [36352242167](https://github.com/Ajw2003/CannaScraper/actions/runs/36352242167), 2026-09-27, runner IP 172.208.153.2: catalog HTTP 200, stock API HTTP 200, and `index_builder.py
--province Saskatchewan --limit 3` stored 3,255 rows with no failures or retries. The locator
page returned HTTP 503 (`retry-after: 139`) from Shopify; one sample cannot say whether that is
a data-centre block or a transient error, and nothing in a scrape needs it. Limit of the
evidence: one run, 66 API calls. A full province is ~2,300 calls and is not yet shown to stay
unblocked.

**Status.** Standing.

## 2026-09-27 — Explore static GitHub Pages site fed by per-province GitHub Actions scrapers

**Context.** The current public-facing product is a Cloudflare tunnel out of one desktop machine
running the packaged `.exe` (roadmap M5) — fine for demo use, but the tunnel is best-effort with
no SLA in its default `quick` mode (`tunnel.py:9-14`), and it depends on that one machine staying
on. Today the user decided to explore moving the public site to something that doesn't depend on
a machine being left running: a static GitHub Pages site, fed by scheduled, per-province GitHub
Actions jobs that scrape and publish data to a public repo — modelled on
`Ajw2003/RockSkipping`'s `renew-cert.yml` pattern (a scheduled Action, a repo-scoped token,
publishing to a public repo).

**Decision.** Pursue this direction, starting with the smallest possible test of its central
unknown: whether cannacabana.com's catalog and stock API will even answer requests from GitHub
Actions runner IPs at all, rather than blocking them. That test —
`.github/workflows/runner-ip-test.yml` — is being built by a different, concurrent agent session
as of this writing; **its result is not yet known.** If it passes, the plan continues with
per-province scheduled scrape workflows, a public-repo publish step, and a static Pages site
reading that published data. Making the CannaScraper repo itself public is part of this plan,
because GitHub Actions minutes are free for public repos, whereas a private repo caps at 2,000
minutes/month and a daily all-province run is estimated at roughly 120 minutes/day (3,600+
minutes/month) — well over the private-repo allowance.

**Why.** Alternatives considered: keep the current tunnel-based model (rejected as a long-term
public product — no uptime guarantee, ties the product to one machine being on); a hosted server
(not explored yet — adds cost and ops surface the project has otherwise avoided, per
`README.md:6-7`'s "everything here is free" principle). The GitHub Actions approach keeps costs
at zero (same principle) if the repo is public, and removes the single-machine dependency. The
`RockSkipping` `renew-cert.yml` pattern was chosen as the reference because it's a proven,
working example of the same shape (scheduled Action, scoped token, publish to a separate
artifact) already in the user's own account.

**Status.** Standing, **conditional on the IP test in `runner-ip-test.yml` passing**. If
cannacabana.com blocks GitHub Actions runner IPs, this entire direction is blocked and needs a
different approach (see `docs/2-roadmap/Roadmap.md` M7's acceptance criteria). Recorded as
roadmap milestone M7.
