# Canna Cabana Cross-Location Scraper

Canna Cabana's site only shows one store at a time. This tool sweeps many
stores automatically and produces a comparable price/stock dataset.

Everything here is **free**: Python, Playwright's bundled Chromium, SQLite,
and Windows Task Scheduler. No paid APIs, no proxies, no accounts.

---

## Quick start

```bash
python -m venv .venv && .venv\Scripts\pip install playwright beautifulsoup4 pandas && .venv\Scripts\python -m playwright install chromium
```

Then edit `watchlist.txt`, and:

```bash
.venv\Scripts\python main.py --limit 3
```

That scrapes your watchlist at 3 Alberta stores and writes `results.csv`.
When it looks right, drop `--limit` for the full province.

---

## How it works

Three facts about the site drive the whole design. All were verified against
production, and each one rules out a simpler approach:

**1. The catalog is free and public.**
`https://cannacabana.com/products.json?limit=250&page=N` returns all 7,363
products with brand, category, SKUs and default prices. No auth, no age gate.
`catalog.py` pages through it in ~30 requests and caches for 24h.

**2. The store registry is embedded in a page, not an API.**
`/pages/store-locator` contains `window.stores["<handle>"] = {...}` JS
assignments — 225 stores with `store_id`, address, coordinates and hours.
`stores.py` brace-matches those object literals. It is *not* an HTML parse.

**3. Per-store pricing is applied client-side, so a browser is mandatory.**
This is the important one. Requesting a product page with `?sID=<store_id>`
returns **byte-identical HTML for every store** — the per-store price is
written into the DOM by JavaScript after load.

> [!WARNING]
> Do not "optimize" this into a `requests` + BeautifulSoup fetch. You will get
> one price repeated for all 92 stores and nothing will look broken.

### Reading the output

| Value | Meaning |
|---|---|
| `carried=1, available=1` | Stocked and buyable at that store |
| `carried=1, available=0` | Carried but sold out |
| `carried=0` | Store does not carry it — site renders `N.A.` |
| `status=error` | Scrape genuinely failed; HTML dumped to `raw/` |
| `store_id_match=0` | **Suspect** — the site priced this row as a *different* store |

### The `store_id_match` warning

The page prices itself by calling `.../scan-single-item/<store_id>`. That ID is
normally the store we selected — but not always. Observed: selecting **8230
(Brentwood)** priced against **3130 (District)**.

We don't call that API; we just watch which store it was asked about and
compare. Any row where they disagree gets `store_id_match=0` and is listed at
the end of the run. Filter those out for trustworthy analysis:

```sql
SELECT * FROM observations
WHERE run_id = (SELECT MAX(run_id) FROM observations)
  AND status = 'ok' AND COALESCE(store_id_match, 1) = 1;
```

This is invisible without the check — which is exactly why it's there.

`carried=0` is real data, not a failure. The site's own JS renders nothing
when a store's `retail_price` is 0, and we record that distinctly.

Note that **market price is usually uniform chain-wide while the member
(Cabana Club) price varies by store** — e.g. SKU 114205 was $15.24 in Medicine
Hat and $15.44 in Calgary. Stock availability varies far more than price.

---

## Files

| File | Role |
|---|---|
| `config.py` | All URLs, selectors, and tunables. **Change site-specific things only here.** |
| `watchlist.txt` | What to track: SKU, handle, product URL, or title substring |
| `stores.py` | Parses the store registry; caches to `stores.json` |
| `catalog.py` | Pulls the public catalog; resolves the watchlist |
| `browser.py` | Chromium contexts, age gate, store switching + verification |
| `scrape.py` | Per-store extraction with retries |
| `db.py` | SQLite history + CSV export |
| `main.py` | Orchestration and CLI |
| `discover.py` | Re-derives the store/age-gate state keys if the site changes |

## Usage

```bash
.venv\Scripts\python main.py --limit 5          # first 5 stores
.venv\Scripts\python main.py --store 8230 --store 3412   # specific stores
.venv\Scripts\python main.py --province Ontario # a different province
.venv\Scripts\python main.py --headed           # watch it work
.venv\Scripts\python main.py --resume 20260815T230533Z   # continue a run
```

Rows are written to SQLite **after each store**, so an interrupted 92-store
sweep never loses completed work — restart it with `--resume <run_id>`.

## Useful queries

```bash
.venv\Scripts\python -c "import db; c=db.connect(); [print(r) for r in c.execute('SELECT store_name, city, price, member_price, stock_text FROM observations WHERE sku=\"114205\" AND run_id=(SELECT MAX(run_id) FROM observations) ORDER BY member_price')]"
```

Cheapest store per SKU, latest run:

```sql
SELECT sku, title, store_name, city, price, member_price
FROM observations
WHERE run_id = (SELECT MAX(run_id) FROM observations)
  AND status = 'ok' AND carried = 1
ORDER BY sku, member_price;
```

Price changes over time for one SKU:

```sql
SELECT scraped_at, store_name, price, member_price
FROM observations WHERE sku = '114205' ORDER BY scraped_at DESC;
```

## Scheduling (optional)

```bash
schtasks /create /tn "CannaCabanaScraper" /tr "C:\Users\aj\Desktop\CannaCabanaScraper\.venv\Scripts\python.exe C:\Users\aj\Desktop\CannaCabanaScraper\main.py" /sc daily /st 07:00
```

---

## When the site changes

1. Run `.venv\Scripts\python discover.py --manual`, pick a store by hand, and
   read the localStorage/cookie diff it prints.
2. Update the `global_*` keys in `config.py`.
3. If prices stop parsing, re-inspect the selectors in `config.py`
   (`SEL_MARKET_PRICE` / `SEL_MEMBER_PRICE`) against a live product page.

Failed pages are dumped to `raw/<store_id>__<handle>.html` for exactly this.

## Being a good citizen

Stores are scraped sequentially with randomized 2–5s delays. This is a modest
request rate against a real retailer. Please don't crank up the parallelism.

The site ships hardcoded API credentials in its page source for an internal
per-store inventory endpoint that would be far faster. This project
deliberately does not use them: they are not intended for third-party use and
can be revoked without notice.
