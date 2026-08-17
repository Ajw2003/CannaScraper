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

### The `api_*` columns

The page prices itself by calling `.../scan-single-item/<store_id>` on every
load. We don't call that endpoint — we just read the reply instead of throwing
it away. No extra requests, no credentials, strictly more data:

| Column | What it adds |
|---|---|
| `api_stock` | **Exact unit count.** The DOM only ever says "In Stock" |
| `api_member_price` | Member price, straight from the source |
| `api_price` | Authoritative retail price |
| `api_elite_price` | ELITE price — never rendered in the DOM at all |
| `api_equiv_g` | Grams counted toward the 30 g purchase limit |
| `api_raw` | Full positional CSV, for the fields not yet decoded |

`api_stock` is the most useful addition. One product across three stores:

```
Medicine Hat    17 units
Fort McMurray    7 units
Brentwood        3 units
```

All three render as an identical "In Stock" on the page.

The response is a positional CSV — `0,15.44,18.99,each,...` is
`[0]` stock, `[1]` member price, `[2]` retail price, `[6]` gram equivalence.
Fields 4 and 5 are still unidentified, which is why `api_raw` is kept.

These also **cross-check the DOM**: where both exist, `api_price` and
`api_member_price` have matched the rendered values exactly in every row
observed. A disagreement would be worth investigating.

### The `store_id_match` warning

The page prices itself by calling `.../scan-single-item/<store_id>`, choosing
that ID with `getEffectiveStoreId()`:

```js
const HUB_STORE_MAP = { 'district': '3130', 'eastlake': '3170' };
return isDeliverySelected && HUB_STORE_MAP[store.hub_id]
  ? HUB_STORE_MAP[store.hub_id]   // the hub
  : storeId;                      // the real store
```

**In delivery mode**, each of the 36 Calgary-area stores carrying a `hub_id` is
priced as its hub — so 17 stores report one identical price and stock, and 19
report another. That is why `config.AGE_GATE_STATE` sets
`age_verification_delivery = "false"`: in **pickup** mode the real `store_id` is
used and every store reports its own shelf.

With pickup mode set, `store_id_match=0` should never appear. If it does, the
delivery flag has regressed — check `config.py` first. Filter suspect rows with:

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

## Two backends

| | Browser | **API** (default) |
|---|---|---|
| 10 nearest stores, 1 product | ~85 s | **~10 s** |
| 92 stores, 1 product | ~18 min | **~2 min** |
| 92 stores, 5 products | ~60 min | **~3.6 min** |

The API backend calls the same endpoint the site's own page uses, and **one
call carries the whole watchlist for a store** — so cost scales with the number
of stores, not stores × products. That's why five products cost barely more
than one.

**No credentials are used.** The endpoint answers unauthenticated; the site
mints a token but never attaches it to this call, so neither do we.

It is rate-limited (`X-RateLimit-Limit: 60`/min, and it 429s on short bursts),
so requests are paced sequentially at 50/min with headroom. Raising
`config.API_RATE_PER_MIN` will get you 429s, not speed.

Switch back any time with `--fetcher browser` or `config.FETCHER`.

### Trust but verify

```bash
.venv\Scripts\python main.py --product 203012 --top 8 --compare
```

Runs **both** backends over the same stores and diffs price, member price,
stock, carried, and available. Nothing is written to the database. Run this
after any site change before trusting the fast path — a backend silently
returning stale or another store's numbers would look completely normal in
normal output.

Known: store **528 (Gateway Village)** returns HTTP 500 from the API and is
reported as an error rather than guessed at. Every other Alberta store works.

## Finding which store has a product in stock

The common case, in two commands. No file editing.

**1. Find the product** (any part of the name):

```bash
.venv\Scripts\python catalog.py --find "grape gas"
```

```
     SKU  brand         size     category   title
  292114  Claybourne    1.5 g    Pre-Rolls  Frosted Flyers Grape Gasolina Infused PR
  203012  Spinach       1 g      Vapes      Pufferz Grape Gas Disposable Vape
```

**2. Hunt it across every store**, using the SKU from step 1:

```bash
.venv\Scripts\python main.py --product 203012
```

```
IN STOCK — 7 store(s):

  units  store                 city           price    member
     24  Banff                 Banff         $35.99         -
      9  Bonnyville            Bonnyville    $35.99         -
      8  Beaumont              Beaumont      $35.99         -
      1  Blackfalds            Blackfalds    $35.99         -
```

Sorted by units on hand, so the top row is where it's most likely to still be
there when you arrive. One product across all 92 Alberta stores takes about
**12 minutes**; add `--limit 10` to sample quickly first.

`--product` accepts a SKU, handle, product URL, or title text, and can be
repeated. It overrides `watchlist.txt` for that run without editing anything.

### Checking every store in a province

`--top` limits to the nearest N. To sweep them all, use `--all`:

```bash
.venv\Scripts\python main.py --product 203012 --all
.venv\Scripts\python main.py --product 203012 --all --province Ontario
```

| Province | Stores | Time (API) |
|---|---|---|
| Alberta (default) | 92 | ~2 min |
| Ontario | 100 | ~2.2 min |
| Saskatchewan | 13 | ~20 s |
| Manitoba | 12 | ~20 s |
| British Columbia | 8 | ~12 s |

`find.bat` also takes arguments, so you can skip the prompts:

```bash
find.bat "grape gas"                    REM nearest 10
find.bat "grape gas" 25                 REM nearest 25
find.bat "grape gas" all                REM every Alberta store
find.bat "grape gas" all Ontario        REM every Ontario store
```

Stores flagged `store_id_match=0` are **excluded** from this list — their stock
figure belongs to whichever store the site actually answered for.

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
