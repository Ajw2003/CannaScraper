# Scraping & fetching

## What it owns

Getting one store's price/stock for a set of SKUs off the live `cannacabana.com` site, by
whichever of two interchangeable backends is available, and turning the site's answer into rows
the rest of the system can trust.

## How it works

Two backends sit behind a common interface, `fetchers.get_fetcher()`
([fetchers/\_\_init\_\_.py:115](../../CannaScraper/fetchers/__init__.py)), both returning the same
row shape as `scrape.scrape_variant()`:

- **API backend** (`ApiFetcher`, [fetchers/api_fetcher.py:56](../../CannaScraper/fetchers/api_fetcher.py))
  — calls `POST /api/product/scan-multiple-items/<store_id>` with the whole watchlist in one
  request ([`_call`](../../CannaScraper/fetchers/api_fetcher.py) at api_fetcher.py:92). Cost scales
  with stores, not stores × products, and it paces itself (`paces_itself = True`, api_fetcher.py:60)
  — callers must not add their own inter-store delay on top of it.
- **Browser backend** (`browser.py` + `scrape.py`) — drives real Chromium via Playwright. A fresh
  `browser.store_context()` per store ([browser.py:75](../../CannaScraper/browser.py)) is seeded
  with localStorage/cookies ([`_store_state`](../../CannaScraper/browser.py) at browser.py:25) so
  the site's own JS treats that store as selected, then `scrape.py` reads the DOM after
  [`_wait_for_price()`](../../CannaScraper/scrape.py) settles (scrape.py:110) and passively
  captures the same `scan-multiple-items` XHR the page fires anyway
  ([`scrape_store._on_response`](../../CannaScraper/scrape.py) at scrape.py:310).

`discover.py` is the one-off tool that originally reverse-engineered which localStorage/cookie
keys hold store selection and the age-gate flags — still runnable (`--manual`) to re-derive them
if the site's storage scheme changes.

## Invariants

- **A store switch must be verified, never assumed.** `browser.assert_store()`
  ([browser.py:120](../../CannaScraper/browser.py)) raises rather than silently mislabeling one
  store's prices as another's. `store_id_match` (scrape.py:198,
  [fetchers/api_fetcher.py:236](../../CannaScraper/fetchers/api_fetcher.py)) records whether the
  site actually priced against the store requested — `0` means don't trust the row.
- `carried`, `available`, and `status` are three distinct facts. Not-carried is a legitimate
  answer, not an error (scrape.py:220-260; the `success: false` / "Bag Changed" handling at
  [fetchers/api_fetcher.py:106-124](../../CannaScraper/fetchers/api_fetcher.py)).
- `missingItems` (the API's not-carried list) is keyed by **variant ID**; `scanned-items` is keyed
  by **SKU**. They must be translated through the variant list before comparing
  (api_fetcher.py:184-189) — comparing them directly makes the not-carried test unconditionally
  false.
- **Pickup mode, not delivery mode, is mandatory.** The site's `getEffectiveStoreId()` prices
  every Calgary-area store carrying a `hub_id` as one of two hubs in delivery mode, collapsing 36
  stores into 2 distinct prices — see [Decisions.md](../Decisions.md#2026-08-16-approx--pickup-mode-required-delivery-mode-collapses-36-calgary-stores-into-2-hubs).
  `config.AGE_GATE_STATE` sets `age_verification_delivery = "false"` for exactly this reason; the
  API path sidesteps the issue structurally by always sending the real `store_id`
  ([fetchers/api_fetcher.py:21-23](../../CannaScraper/fetchers/api_fetcher.py)).
- A browser context is per-store, never reused across stores — the site's own JS re-derives store
  from geolocation and will overwrite a store set after page load if the context is shared
  (browser.py:8-13).

## Traps

- [scrape.py:21-26](../../CannaScraper/scrape.py) documents an observed live case where selecting
  store 8230 got priced as store 3130 — the actual incident `store_id_match` exists to catch.
- [fetchers/api_fetcher.py:106-121](../../CannaScraper/fetchers/api_fetcher.py) is a documented
  postmortem: `success: false` was originally treated as a hard failure, burning all three retries
  on the ordinary case of "nothing in this batch is carried here." `verify_scan_fix.py` exists
  specifically to regression-check this one bug.
- The same postmortem covers a second, coupled bug at api_fetcher.py:184-189: testing SKUs against
  a variant-ID set made the "is this SKU covered" check unconditionally false. See
  [Decisions.md](../Decisions.md) for the full incident writeup (originally
  `CannaScraper/PLAN_followups.md` item 1).
- Do not "optimize" the browser path into a plain `requests` + BeautifulSoup fetch — every store
  ID returns byte-identical HTML for a product page; per-store price is written into the DOM by
  client-side JS after load. This was the founding reconnaissance finding for the whole project
  (see [BUILD_INSTRUCTIONS.md](../../CannaScraper/BUILD_INSTRUCTIONS.md), Step 0c, and
  [Decisions.md](../Decisions.md#2026-08-16-approx--browser-is-mandatory-for-per-store-pricing-no-requestsbeautifulsoup-shortcut)).
