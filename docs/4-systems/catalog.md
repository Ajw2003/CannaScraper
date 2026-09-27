# Catalog & watchlist

## What it owns

Pulling Canna Cabana's public product catalog and resolving the watchlist (or a `--product`
argument) into concrete SKUs to track. Owns `catalog.py`. Does **not** own per-store price or
stock — those fields on a catalog row are shop-level defaults, explicitly not location-specific
(`catalog.py:7-9`).

## How it works

- `GET /products.json?limit=250&page=N` is open — no auth, no age gate — and returns the whole
  catalog 250 products a page, in ~30 requests (`README.md:56-59`, `catalog.py:1-9`).
- The catalog is flattened to one row **per variant** (not per product), because the variant is
  the unit carrying a SKU and a price (`catalog.py:4-5`).
- Results are cached; the README states a 24h cache (`README.md:59`). TODO: confirm exact TTL
  constant and cache file location by reading the rest of `catalog.py` (only the first 30 lines
  were reviewed for this doc).
- `catalog.search()` powers the web UI's instant, no-network search
  (`README.md:320`, `server.py:12` imports `catalog`).
- `catalog.py --find "<text>"` is the CLI entry point for looking a product up by name
  (`README.md:683-690`).

## Invariants

- Catalog fields (`price`, `available`) must never be reported as per-store — that data comes
  only from the browser/API scrape path (`catalog.py:7-9`). Conflating the two would silently
  reintroduce the "one price for every store" bug the whole project exists to avoid
  (`README.md:71-73`).

## Traps

- Treating `products.json`'s `price`/`available` as authoritative for a specific store. It is
  explicitly documented as wrong (`catalog.py:7-9`); the authoritative per-store numbers come
  from `scrape.py` / the API fetcher, not here.
- TODO: verified catalog size (README says 7,363 products, 5,322 SKUs in the products table —
  `README.md:57`, `README.md:162`) may drift as the site's own catalog changes; nothing here
  currently detects that drift automatically. Not verified against a live run in writing this doc.
