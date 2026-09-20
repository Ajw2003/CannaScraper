# Decisions

Newest entry at the top. Entries are never rewritten or deleted — only appended to, or, when
superseded, have their `Status` line flipped with a pointer to the entry that replaced them.

---

## 2026-09-20 — New outer repo as the home for GitHub Pages deployment

**Context.** `CannaScraper/` already existed as its own git repo (`github.com/Ajw2003/CannaScraper`,
branch `ApiFork`) with the full scraper/desktop-app history. The
[github-pages-per-province plan](plans/github-pages-per-province.md) needed a place to hold
`.github/workflows/`, a `data` branch for nightly JSON, and project-wide docs — none of which
belong inside `CannaScraper/`'s own history as a scraper-specific repo.

**Decision.** Create a new outer repo, `CannaCabanaScraper`, at the parent folder, with
`CannaScraper/` as a subdirectory holding the application code, and `.github/workflows/`,
`docs/` living at the outer root.

**Why.** Keeps the deployment plumbing (workflows, Pages config, cross-cutting docs) separate
from the application's own commit history, while still keeping everything in one place on disk
and (intended) in one push.

**Status.** Standing, but see the 2026-09-20 entry below — the mechanism for getting
`CannaScraper/`'s files into the outer repo was never finished, and the outer repo currently
cannot see them.

---

## 2026-09-20 — `CannaScraper/` still has its own `.git`; the outer repo cannot track its files

**Context.** Found while building the docs/ tier structure. `CannaScraper/` contains a nested
`.git` directory (remote `github.com/Ajw2003/CannaScraper`, branch `ApiFork`). The outer repo's
`git status` reports `CannaScraper/` as a single untracked entry, not as ~60 individual files —
which is what happens when a directory holds its own repo: git treats it as an opaque **gitlink**,
not file content.

`.github/workflows/_build-province.yml` checks out the outer repo's `main` with
`working-directory: CannaScraper` and runs `pip install -r requirements.txt` and
`python index_builder.py` directly against that checkout — which only works if `CannaScraper/*.py`
are real tracked blobs in the outer repo. They are not, yet. `actions/checkout@v4` does not fetch
submodule content by default, and there is no `.gitmodules` here — so a workflow run today would
find `CannaScraper/` empty and fail at `pip install -r requirements.txt`.

The outer repo's own [.gitignore](../.gitignore) already has `CannaScraper/history.db`,
`CannaScraper/.venv/`, etc. — patterns that are only meaningful if the outer repo is meant to
track the *rest* of `CannaScraper/`'s files directly, confirming the intended shape is "one repo,
real files," not a submodule.

**Decision.** Not made yet — this is a flagged, unresolved blocker, not a resolved decision.
Recorded here so it isn't rediscovered by accident when a workflow run fails mysteriously. See
[ProjectState.md](ProjectState.md) for the current-status framing.

**Why.** Documented rather than fixed silently, because removing `CannaScraper/`'s nested `.git`
is effectively deciding to fold its standalone history into the outer repo — `CannaScraper`'s own
GitHub history stays put either way (nothing here touches `github.com/Ajw2003/CannaScraper`), but
this working copy's relationship to it changes, and that's a call for whoever owns the repo, not
something to do quietly while writing documentation.

**Status.** Standing (open).

---

## 2026-08-31 — `observations` table split into `products` / `store_meta` / `obs`

**Context.** The original single `observations` table wrote every product and store fact onto
every row — a title stored ~55 times over, a store name ~1,300 times — taking `history.db` to
203 MB for 295,512 rows.

**Decision.** Split into three tables (`products`, `store_meta`, `obs`) joined by an
`observations` **view** that presents the same 34 columns in the same order, so every existing
`SELECT *` query keeps working unchanged. `thc`/`cbd`/`default_price` stayed on the
per-observation row deliberately — they are not stable product attributes (see the 2026-08-25
entry below).

**Why.** Took the file from 203 MB to 97 MB with zero call-site changes elsewhere in the
codebase, because the view is the compatibility layer. `db.write_rows()` is the only path allowed
to write, so the two lookup tables can't drift out of step with `obs`.

**Status.** Standing. Migration path for a pre-split database is `normalize_db.py` (backs up to
`history.db.pre-normalize`, verifies all 295,512 rows before dropping the old table).

---

## 2026-08-25 — THC/CBD potency shown as a consensus range, not `MAX()`

**Context.** `province_facts()` used `MAX(o.thc)` on the reasoning that THC is product-level and
`MAX()` "just picks a non-null value." Measured against live data that premise was false: 899 of
5,322 SKUs carry more than one distinct THC value (different stores hold differently-tested
batches), and the source data itself carries scattered 10x decimal errors (e.g. SKU 202013:
97.78 at 187 stores, 977.80 at 25 — clearly the same fact typed wrong, not two real potencies).

**Decision.** `db._span_aggs()` brackets values on each side of the 100 threshold separately, and
`db.pick_span()` keeps whichever side more stores agree on — a plain `MIN`/`MAX` across the whole
set would have produced nonsense ranges like "97.8–977.8". Rendered as a single figure when every
store agrees within 0.1, a range otherwise.

**Why.** A naive fix (discard anything over 100) would have been wrong just as often — SKU 113157
("GoodNight 1000 mg softgels") legitimately reports 1000, named in the product title. The
consensus-of-agreement rule handles both directions without knowing in advance which SKUs are
percent vs. milligram.

**Status.** Standing. The `n > 100` mg-vs-percent fallback in `server.potency()` is a related,
still-open gap — see [ProjectState.md](ProjectState.md) cross-cutting issues.

---

## 2026-08-25 — `product/search` chosen over `scan-multiple-items` for province indexing

**Context.** Needed a way to build a full-province stock snapshot. Two candidate endpoints:
paged `product/search` (what became `index_builder.py`) and per-SKU `scan-multiple-items` (what
the live per-product check already used).

**Decision.** Keep `product/search` for indexing; `scan-multiple-items` remains only for the
live, per-SKU check.

**Why.** Measured via `scan_ceiling_probe.py`: `scan-multiple-items` costs ~0.3 s per SKU
regardless of batching (ceiling between 500–1000 SKUs/call before HTTP 500), which projects to
~9 h for a full Alberta sweep even in the most optimistic case — worse than the ~1 h
`product/search` already achieves, and it regresses correctness (anything newly in stock since
the last run is invisible, since you can only scan SKUs you already know about). `product/search`
sends more bytes per call but is ~12x more time-efficient per product because its cost is
bandwidth, not server compute.

**Status.** Standing — investigated and rejected, recorded so the idea isn't re-proposed.

---

## 2026-08-25 — Egress pool added for province-index throughput, defaulting to empty

**Context.** A full province index (~2,300 requests at the measured 50/min pacer) takes ~46
minutes, and the rate-limit budget is counted once per client regardless of concurrency — so
threading the existing single-route fetcher could not speed it up.

**Decision.** Added `egress.py`, a pool of routes (each its own proxy, pacer, and health state),
with `index_builder.py` running one worker per healthy route. The pool ships **empty by default**.

**Why.** Nothing proved the rate-limit counter was IP-keyed before measuring it — it could just as
easily key on something every route sends, in which case a pool buys nothing. `egress.verdict_for()`
was built specifically to distinguish "independent budget" from "shared budget, coincidentally
close" after an earlier version of the same probe couldn't tell them apart (two truly independent
budgets both read near the top of their own window, so a one-request-per-route comparison can't
distinguish "shared, drawn down" from "independent, both near full"). See the README's
"Keeping the schedule alive indefinitely" / rate-limit sections for the measured numbers.

**Status.** Standing.

---

## 2026-08-25 — Durable SQLite work queue for province indexing, not Redis/Celery

**Context.** Multiple egress workers need to divide up a province's stores without duplicating
work, surviving a worker crash mid-store, and retrying transient failures.

**Decision.** `workqueue.py` — every store is a durable row in a `work_queue` table, claimed by
an atomic conditional `UPDATE` (`rowcount == 1` decides the winner), with a lease timeout for
redelivery and a max-attempts cap for retries.

**Why.** The textbook answer ("decouple workers behind a message broker") assumes a always-running
service; this app ships as an exe someone unzips and double-clicks. Requiring a broker daemon to
be installed and running before a stock index can build trades a working desktop tool for an ops
problem. SQLite already open in WAL mode gives the same four guarantees (survive a crash, no
duplicate claims, redelivery, retry) with no second moving part. `db.write_rows()` being
`INSERT OR REPLACE` keyed on `(run_id, store_id, sku)` is what makes redelivery safe — a store
indexed twice just overwrites itself.

**Status.** Standing. `claim()` is called out as the one function that would need to change if
this ever needed to span more than one machine.

---

## 2026-08-23 — Ship as a distributable desktop app: Cloudflare tunnel, open reads / password-gated writes

**Context.** The web UI only ran as `python server.py` from a source checkout, bound to
`0.0.0.0:8000` (LAN-only), with no distributable artifact. See the (now-executed)
[PLAN_app_distribution.md](../CannaScraper/PLAN_app_distribution.md) for the full plan.

**Decision.**
- Public URL via a bundled Cloudflare quick tunnel by default; a named tunnel (token in
  `settings.json`) for a stable link.
- Reading/searching stays open to anyone with the link; anything that causes outbound scraping
  (a live re-check, a province rebuild) requires the admin password.
- Playwright/browser fetcher dropped from the packaged build (stays available from source).
- `catalog.json` + `stores.json` ship bundled; the stock index builds on first run.

**Why.** A password-gated read experience would defeat the point of a link you hand to someone;
gating only the actions that hit `cannacabana.com` limits the blast radius of a leaked link to
"someone can trigger a scrape," not "someone can read your data" (which is already the read-only
posture once it's on GitHub Pages too — see the 2026-09-20 per-province plan). Dropping Playwright
cut ~80 MB+ from the packaged build; the API fetcher already outperforms the browser fetcher on
speed and is the documented default.

**Status.** Standing — executed. `app.py`, `server.py`, `auth.py`, `jobs.py`, `tunnel.py`,
`build.ps1`, and `CannaCabana.spec` all exist and match this plan; see
[docs/systems/](systems/) for how each works today.

---

## 2026-08-16 (approx.) — Browser is mandatory for per-store pricing; no `requests`+BeautifulSoup shortcut

**Context.** Early reconnaissance (documented in
[BUILD_INSTRUCTIONS.md](../CannaScraper/BUILD_INSTRUCTIONS.md)) tested whether a plain HTTP
fetch could read per-store pricing directly from a product page.

**Decision.** Use a real browser (Playwright/Chromium) for anything that needs per-store DOM
pricing; a plain `requests` fetch is never sufficient for that path.

**Why.** Requesting the same product page with different `?sID=` values returns **byte-identical
HTML** — per-store price is written into the DOM by client-side JavaScript after load. A
`requests`+BeautifulSoup fetch would silently return one store's price repeated for all stores,
with nothing about the response looking broken. This was confirmed directly (`curl` byte-length
comparison across two store IDs) before any scraper code was written.

**Why it doesn't block the API fetcher path added later:** the discovered `scan-single-item` /
`scan-multiple-items` / `product/search` endpoints are called by the page's own JavaScript and
return the real per-store answer directly — reading that response (which the browser fetcher
already had to intercept anyway) is what let the later API-only fetcher exist without a browser
at all. The mandatory-browser finding is about DOM scraping specifically, not about every possible
approach.

**Status.** Standing.

---

## 2026-08-16 (approx.) — Pickup mode required; delivery mode collapses ~36 Calgary stores into 2 hubs

**Context.** The page picks which store to price with `getEffectiveStoreId()`; in delivery mode,
every store carrying a `hub_id` (36 Calgary-area stores) is priced as one of two hubs (`district`
→ `3130`, `eastlake` → `3170`) instead of itself.

**Decision.** `config.AGE_GATE_STATE` sets `age_verification_delivery = "false"` (pickup mode) for
all scraping.

**Why.** In delivery mode, 17 stores report one identical price/stock and 19 report another —
the tool cannot tell which shop actually holds an item. In pickup mode every store reports its
own shelf, and `store_id_match` (comparing the store actually priced against the store requested)
becomes a reliable regression signal: it should be `1` for every row in pickup mode, and a `0`
means this flag has regressed.

**Status.** Standing.
