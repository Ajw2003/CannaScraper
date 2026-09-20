# Catalog & stores registry

## What it owns

`catalog.py` — the product metadata cache (from the public, unauthenticated `/products.json`
endpoint) and watchlist/search term resolution. `stores.py` — the 225-store registry scraped from
the store-locator page, plus nearest-store geometry.

## How it works

`catalog.load_catalog()` ([catalog.py:96](../../CannaScraper/catalog.py)) **never** makes a
network call and is never staleness-gated at read time — search has to answer on every keystroke
regardless of cache age. Staleness is only *surfaced* via `catalog_is_stale()` /
`catalog_age_hours()`, for the UI or a background refresh to act on separately.
`refresh_catalog()` (catalog.py:128) writes through a `.tmp` file and `os.replace()`s atomically,
behind a lock so two concurrent refreshes can't race. A missing or corrupt cache re-seeds from the
bundled copy via `paths.seed()` (catalog.py:114-125).

`search()` (catalog.py:183) ranks results — title-exact > title-starts-with > phrase-in-title >
all-words-somewhere-in-title > matched-elsewhere — not alphabetically. `site/app.js:42-70` is a
direct JS port of this same ranking for the static Pages mirror.

`stores.py` hand-parses `currentStoreData = {...}` JavaScript object literals out of the
store-locator page's raw HTML with a brace-matcher (`_match_object`, stores.py:40), because the
data lives in JS, not the DOM — an HTML parser or a lazy regex both fail here (see
[BUILD_INSTRUCTIONS.md](../../CannaScraper/BUILD_INSTRUCTIONS.md) Step 3). `nearest()`
(stores.py:156) computes plain great-circle distance (`distance_km`, stores.py:145) with no
external geo dependency; `site/app.js:73-92` ports the same formula for the static site.
`geocode()` (stores.py:169) resolves a free-text place to coordinates via Nominatim, with a disk
cache.

## Invariants

- Catalog-level `price` / `available` fields are shop-wide **defaults**, never per-store facts
  (catalog.py:7-9) — a caller that needs a real per-store number must go through
  `db.latest_observations()`, not the catalog.
- Both `load_catalog()` and `get_stores()` are disk-cache-first; a stale cache never blocks a read
  (catalog.py:154-163 explicitly documents removing an earlier "refresh inline when stale"
  behavior — see Traps).
- `hub_id` stays in the store registry specifically so affected (Calgary-area, delivery-hub)
  stores can be identified if the delivery/pickup handling regresses (stores.py:112-116) — see
  [scraping-and-fetching.md](scraping-and-fetching.md)'s pickup-mode invariant.

## Traps

- **catalog.py:154-163** documents a past regression: staleness used to trigger a *blocking*
  download inline on the read path, which could 500 a search entirely if the machine was offline
  and the bundled catalogue had already gone stale by ship day. Staleness detection and refreshing
  are now fully decoupled — don't reintroduce a synchronous refresh into the read path.
- **stores.py:112-116** (`hub_id`): in delivery mode, Calgary-area stores carrying a `hub_id` are
  priced as one of two hubs rather than themselves — this is a site behavior, not a bug in this
  codebase, but it's the reason `config.AGE_GATE_STATE` forces pickup mode everywhere. See
  [scraping-and-fetching.md](scraping-and-fetching.md).
- Do not use an HTML parser or a lazy `\{.*?\}`-style regex on the store-locator page — the
  regex truncates on the nested `address` / `hours_periods` objects inside each store record
  (see BUILD_INSTRUCTIONS.md Step 3).
