# Restore the original page on GitHub Pages

Requested 2026-09-28: undo every change and removal the user didn't ask for, so the Pages site is
as close to the original desktop page as a static host allows. UI/UX improvements are proposed to
the user first and not built until approved. Background: `docs/generated/pages-audit/index.html`
and `docs/plans/house-rules-guard-silent-feature-loss.md`.

## Approach: the original page, with its server answered from files

`site/index.html` becomes a copy of `web/index.html` with the smallest possible edits, plus one
added script, `site/static-api.js`, that answers the page's `fetch('/api/…')` calls from published
JSON using line-for-line ports of the server code that produced those answers. The page code stays
the original's, so parity can be checked by diffing the two HTML files and by feeding identical
requests to the old server and to `static-api.js` and comparing the JSON they return.

## Parity inventory (from the code, not memory)

Legend: **Same** = restored exactly. **Closest** = can't run without a server; nearest static
equivalent, listed so nothing changes silently.

### Search and browsing

| Original feature | Source | Status |
|---|---|---|
| Search over the whole catalogue: title, brand, category, size; every word required; ranked exact → starts-with → phrase → all words in title → brand/category; shorter titles first | `catalog.py:183-223`, `server.py:280-351` | Same (port of `catalog.search`) |
| Needs 2+ characters; 180 ms debounce; 50 per page, "Show N more", "Showing X of Y", "All N shown" | `web/index.html` search() | Same (page code unchanged) |
| "Only products in stock somewhere in this province" switch; hidden count ("27 hidden"); "none in province" label; "No matches in stock … N matched but are out everywhere" | `server.py:api_search` | Same (needs the catalogue published; see Data) |
| Category dropdown from the province's stocked products, with counts, biggest first; category filter compares the indexed category, falling back to the catalogue's | `server.py:api_categories`, `api_search` | Same |
| Province dropdown with store counts, biggest first; default province `Alberta` | `server.py:api_provinces`, `config.PROVINCE` | Same |
| THC/CBD on cards: consensus range across stores, split at 100 (`_span_aggs`, `pick_span`), formatted by `potency_span` (Python `:g`) | `db.py:538-632`, `server.py:122-178` | Same (fixes the 0.46% → 0.5% bug) |
| Thumbnails at width 160 (cards) and 320 (product) | `server.py:thumb` | Same |
| Catalogue line: "N products · catalogue updated X ago" | `server.py:api_catalog_status` | Same, from the published catalogue's timestamp |
| "Refresh catalogue" button | `server.py:api_catalog_refresh` (admin) | **Closest:** hidden; the catalogue refreshes on a schedule instead (see Data) |

### A product's stores

| Original feature | Source | Status |
|---|---|---|
| "N of M stores have it · <scope>"; `indexed` / `partly indexed` / `not indexed yet` status with index age; "this product last checked X ago" / "not stocked at any of them" | `server.py:api_results`, `db.index_coverage`, `db.cache_age_hours` | Same |
| Scope: 5 / 10 / 25 nearest or whole province; location from device, typed city/postal code, or `config.HOME` ("Calgary, AB") when neither | `server.py:_scope`, `stores.nearest`, `stores.resolve_location` | Same, geocoding via `geocode.json` then OpenStreetMap Nominatim from the browser (the service the server used) |
| Sort: closest first / most stock first; out-of-stock rows last | `server.py:_pack` | Same |
| Every store in scope listed: in stock (units pill, amber under 5), "not in stock" (indexed, no row), "not checked" (never indexed); "Show N without it" toggle | `main.fill_missing_stores`, `web/index.html` render() | Same |
| Price: tier price (ELITE or member) bold, market struck through, "save $X (Y%)"; "Elite only" badge | `main.tier_price`, `server.py:_pack` | Same |
| "Reload from index" button | `web/index.html` | Same |
| Source dropdown "Live: fast API" / "Live: real browser" and "Check live now (N stores, ~T)" with progress bar | `server.py:api_refresh`, `/api/job` | **Closest:** both live options removed, as the original already did for "real browser" when Playwright was missing (`caps()`); only "Index (instant)" remains |

### Catalogue index panel

| Original feature | Source | Status |
|---|---|---|
| Panel summary "N of 5 provinces fully indexed"; per-province "stores · indexed x/y · age · about N min"; "last run stopped at x/y" | `server.py:api_index_status` | Same, from published run summaries |
| Refresh / Resume / Start over / Cancel buttons, progress bars, queue state | `server.py:api_index_*`, `jobs.py` | **Closest:** buttons not shown (nothing on the page can start a scrape); rebuilds happen hourly via `scrape-all.yml` |
| Request-budget line ("got within N of 60 …") | `ratelimit.history()` | **Closest:** shown only if the run summary carries it; otherwise empty, as the original renders when there's no history |
| Admin password box | `auth.py` | **Closest:** never appears, because nothing asks for it |
| Note text "…rebuilding needs the password." | `web/index.html` | **Closest:** reworded to say rebuilds run automatically every hour; nothing else in the note changes |
| Stale-build banner + "Rebuild and restart" | `server.py:api_build_status` | Same behaviour as a non-packaged run: hidden (`frozen: false`) |

## Verified: what a page on github.io can actually do (2026-09-28)

The **Closest** rows above were first written as assumptions. They were then tested from a GitHub
Actions runner in two ways: `curl` with `Origin: https://ajw2003.github.io` (headers), and real
Chromium loaded on the live site running `fetch()` as page script (run 36376135472, job
108782246147; the probe workflow was temporary and has been removed).

| Request | Headers | Real browser on the Pages origin | Conclusion |
|---|---|---|---|
| Stock API `GET /api/product/search` | `Access-Control-Allow-Origin: *` | HTTP 200, readable | Allowed |
| Stock API `POST /api/product/scan-multiple-items/<store>` (the original live check) | Preflight 204, allows POST + `content-type` | HTTP 200, readable | **Live check can come back**, run from the visitor's browser |
| Catalogue `GET cannacabana.com/products.json` | `access-control-allow-origin: *` | HTTP 200, readable | **Catalogue refresh can come back** in the browser (refreshes that visitor's copy) |
| OpenStreetMap Nominatim search | `access-control-allow-origin: *` | HTTP 200, readable | City/postal lookup works as planned |
| GitHub API `POST …/actions/workflows/scrape-one.yml/dispatches` | Preflight 204, allows `Authorization` | HTTP 401 "Bad credentials", readable | **Rebuild / resume / cancel can come back with a GitHub token** entered on the page; without one, not |
| `cannacabana.com` in an iframe | `x-frame-options: DENY`, `frame-ancestors 'none'` | Frame shows `chrome-error://chromewebdata/` | **"Live: real browser" cannot come back** from the page |

Corrections to the inventory above:

- **Live check ("Live: fast API")** — was listed as needing a server. It doesn't: the same request
  the server made works from the visitor's browser. It runs from the visitor's IP address and
  against their own 60-requests-a-minute allowance, instead of the host machine's. Restoring it is
  on-script (it's the original feature).
- **"Live: real browser"** — confirmed impossible from a page (framing refused; a page can't drive
  another site). It could only run as a GitHub Actions job.
- **"Refresh catalogue"** — possible in the browser, but it would refresh only that visitor's copy;
  refreshing the published catalogue for everyone needs the token route below.
- **Rebuild / Resume / Start over / Cancel, progress, and the admin password** — possible if the
  person using them pastes a GitHub token (fine-grained, Actions read/write on this repo only) into
  the page, which then starts and watches `scrape-one.yml` runs through GitHub's API. That's a
  different mechanism from the original password and stores a token in the browser, so it's a
  **proposal for the user**, not part of the restore.
- **Stale-build banner** — not applicable: there's no packaged build to go stale.

## Data the page needs (all published to `gh-pages/data/`)

- `index.json` — province manifest (exists), plus each province's store count and last-run summary.
- `<slug>.json` — per province: stores (with coordinates and per-store scrape time) and the newest
  row per (sku, store), **including sold-out rows** (needed for facts, "not in stock" vs "not
  checked" and potency spans). Rows carry qty, market price, tier label/price, THC, CBD, available.
  Today's export drops sold-out rows; the exporter changes to keep them.
- `catalog.json` — slim catalogue (sku, title, brand, size, category, handle, image) with its
  timestamp. Produced from `catalog.py`; refreshed by the scrape when older than
  `CATALOG_MAX_AGE_H` (24 h), as the original app refreshed a stale catalogue on start.
- `stores.json`, `geocode.json` — copies of the committed files, for store counts and the location
  cache.

`ci/build_manifest.py` must skip the non-province files.

## Workflow items restored

From the restructure check (`docs/plans/house-rules-guard-silent-feature-loss.md`): the per-province
verify step fetches the page itself again; each run's JSON is uploaded as a 3-day Actions artifact
again; the publish log says again which data it kept and when it makes the first publish.

## Changes from the original the user approved

- **A failed store in a live check is reported** (2026-09-28). The original swallowed a store's
  scan failure (it became an ignored error row) and ended the job "Done."; the static page
  reports it on the job, so the page shows "Some stores failed: <store>: <reason>". The failed
  store still keeps its previous data, as before. Test: `ci/live_parity_check.py` expects this.

### Additions from the first static site, brought back (approved 2026-09-28)

The user asked for the price on the search cards and every other addition the first static site
(`df8a133:site/index.html`) had made to come back. Each is marked "added on the Pages site" in
the code and sits on top of the original page without changing what the original does:

| Addition | Where | Test |
|---|---|---|
| Card line "N stores · from $X": stores in the province that have it, lowest shelf (market) price among them, as the first static site showed it | `static-api.js:provinceFacts`, `apiSearch`; `index.html:card` | `ux_test`: card shows count and price |
| "<Province> stock updated X ago · rebuilt every hour" under the heading, before any product is opened | `static-api.js:apiProvinceSummary`; `index.html:loadSummary` | follows province change |
| Notice naming the stores that failed on the last run ("showing their older data instead"); the export now records their names (`run.failed_stores`, from `workqueue.failures`) | `ci/export_province.py`; `index.html:loadSummary` | export of a DB with two failed stores names both; page shows them |
| Browse without typing: an empty box lists the province's products A to Z through the same filters, 50 at a time; one letter still does nothing | `static-api.js:apiSearch`; `index.html` input handler, `rerun` | 50 cards on load |
| "view" link on each store row to that store's page on cannacabana.com (the URL the original server already built but never showed) | `index.html:render` | links present |
| The chosen province is remembered on the device | `index.html` provinces(), `#prov` change | kept after reload |
| "← Back to search" returns to the same list, scrolled to the card that was opened; opening a product brings it into view | `index.html:render`, `load` | list length and card position kept |
| Cards wrap at phone width instead of pushing the page sideways (the original overflowed by 113 px at 390 px wide with results showing) | `index.html` CSS | no sideways scroll at 390 px |

Parity after these: `ci/parity_check.py` 67 of 67 requests identical to the old server (the two
added search fields, `stores` and `price_from`, are excluded by name).

### Added at the user's request (2026-09-28)

The user asked for the member price and Elite price (where there is one) on the search cards,
and for searched items to be orderable by price. Marked "added on the Pages site" in the code.

| Addition | Where | Test |
|---|---|---|
| Card line also shows " · member $Y" and " · Elite $Z" (lowest member / ELITE tier price among in-stock rows; each only when the product has one) | `static-api.js:provinceFacts` (`member_from`, `elite_from`), `apiSearch`; `index.html:card` | `ux_test`: member shown iff API `member_from`, Elite iff `elite_from` |
| "Order of the products" select after the category: Best match / Price: low to high / high to low. Sorts the whole filtered list by `price_from` before paging; no price always last; ties keep the old order; remembered on the device | `static-api.js:apiSearch` (`sort` param); `index.html` `#psort`, `search`, provinces() | `ux_test`: cards non-decreasing / non-increasing, order kept after reload |

Parity: `member_from` and `elite_from` are excluded by name like `stores` and `price_from`
(`ci/parity_check.py:normalize`); without `sort` the answer is unchanged. Screenshots:
`docs/generated/price-sort/`.

## Proposed changes — NOT built, awaiting the user

Changes to, or removals from, the original. Each needs a yes or a scrap.

1. **Card price matches the product page.** The card's "from $X" is the shelf price; the product
   page leads with the member/ELITE price (e.g. card from $5.99, page $2.99). Show the lowest
   price the page would show in bold, with the shelf price struck through, as the page does.
2. **Distance measured from the chosen province.** With no location typed, the original measures
   from Calgary, AB whatever the province, so Saskatchewan shows "10 nearest to Calgary" at
   520 km. Use a central city of the chosen province instead (Regina, Winnipeg, Toronto,
   Vancouver, Calgary).
3. **Remember the typed location too**, like the province.
4. **A labelled location button**: "📍 Use my location" rather than the bare pin, as the first
   static site had.
5. **Products stocked but missing from the catalogue file** (e.g. Homestead Bandwagon Sativa)
   become searchable; the original can't show them.
6. **Index panel: link each province to its "Scrape one province" run page** on GitHub, so a
   rebuild is one click for anyone signed in with access.
7. **Rebuild buttons with a GitHub token** entered on the page (see "Verified" above; issue #7).

Removals the first static site made that I recommend **not** repeating, because the original is
better for the reader: the scope selector (5/10/25/whole province), typing a city or postal code,
the catalogue index panel, "not in stock" and "not checked" rows, and search ranking.

## Verification

- Diff `web/index.html` against `site/index.html`: only the edits listed as **Closest** above.
- Run the old server and `static-api.js` on the same data; send identical requests (search terms
  from the audit, categories, provinces, results for several SKUs, scopes, sorts, locations) and
  compare the JSON field by field.
- Screenshot both pages through the same steps.
