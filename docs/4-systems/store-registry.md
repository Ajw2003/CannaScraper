# Store registry

## What it owns

Parsing and caching the list of 225 Canna Cabana stores (`stores.py`), each with `store_id`,
address, coordinates and hours. Owns `stores.json` as its cache file.

## How it works

- The store list is not an API — it's embedded in `/pages/store-locator` as a sequence of
  JavaScript assignments: `currentStoreData = {...}`, `currentStoreData.has_delivery = ...`,
  `window.stores["<handle>"] = currentStoreData` (`stores.py:3-9`, `README.md:61-64`).
- Because the data lives in JS, not the DOM, `stores.py` does **not** use an HTML parser. It
  scans for the object literals with a regex (`_ASSIGN`, `stores.py:26`) and brace-matches them
  by hand, since a lazy regex would truncate on nested `address` / `hours_periods` objects
  (`stores.py:10-12`).
- `_TAIL` (`stores.py:27-29`) picks up the `has_delivery` / `has_pickup` flags and the
  `window.stores[...]` handle assignment following each object.
- Results cache to `stores.json` (`README.md:64`).
- `stores.nearest()` is used by the web server for "nearest N stores" queries
  (`server.py:12` imports `stores`; `README.md:331`).

## Invariants

- The brace-matcher must stay a brace-matcher, not a regex/HTML-parser shortcut — the object
  literals contain nested objects (`address`, `hours_periods`) that a naive parse would cut off
  mid-object (`stores.py:10-12`).

## Traps

- Assuming this is an HTML-parseable page. It explicitly is not (`stores.py:10`); any "simplify
  this with BeautifulSoup" instinct will silently truncate store records.
- The hub/delivery pricing quirk lives in the fetcher layer, not here (see
  `fetchers-and-rate-limiting.md`), but it depends on `hub_id` values this registry parses —
  changing how `hub_id` is captured here would break `store_id_match` detection downstream.
