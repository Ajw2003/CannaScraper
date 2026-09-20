# Build Instructions — Canna Cabana Cross-Location Scraper

Step-by-step instructions to build this system end to end, for free, from an
empty folder. Written so either a person or an agent can follow it literally.

Every claim below was verified against the live site during the original
build. Where the original plan guessed wrong, the correction is called out —
those corrections are the most valuable part of this document.

**Total cost: $0.** Python, Playwright's bundled Chromium, SQLite, and
Task Scheduler are all free. No proxies, no paid APIs, no accounts, no keys.

---

## Step 0 — Reconnaissance (do this first, it changes everything)

Before writing scraper code, establish three things. Skipping this is how you
end up building the wrong tool.

**0a. What platform is it?**

```bash
curl -s "https://cannacabana.com/meta.json"
```

Returns Shopify shop metadata: `canna-cabana-high-tide.myshopify.com`,
7,363 published products. **It's Shopify** — which means a free public
catalog API exists.

**0b. Is the catalog public?**

```bash
curl -s "https://cannacabana.com/products.json?limit=3"
```

HTTP 200, no auth, no age gate. The entire catalog is free to read. You do
**not** need a browser for product names, brands, categories, SKUs, or
default prices.

**0c. Is per-store pricing in the HTML?** ← the decisive question

Fetch the same product page for two different stores and compare byte length:

```bash
curl -s "https://cannacabana.com/products/<handle>?sID=3154" | wc -c
curl -s "https://cannacabana.com/products/<handle>?sID=8160" | wc -c
```

**Both return identical HTML.** Per-store pricing is injected client-side by
JavaScript. This single test is what forces the browser into the design.

> [!IMPORTANT]
> If you skip 0c and assume `requests` + BeautifulSoup works, you will get one
> price repeated for all 92 stores, and nothing will appear broken. This is
> the highest-consequence failure mode in the project.

### Corrections to the original plan

| Original assumption | Reality |
|---|---|
| Scrape product grids with Playwright | Catalog is free JSON; browser only needed for per-store price |
| Age gate = click "I am 19+" | It's `localStorage`-driven; seed state instead of clicking |
| Store switching via UI clicks or a `store_id` cookie | Needs cookie **and** localStorage, seeded before page scripts run |
| `<store>.cannacabana.com` subdomains per store | Mostly **dead DNS**; they're vanity links. Do not use |
| Need `playwright-stealth`, UA rotation, proxies | Not needed. Plain Chromium + polite delays works fine |

---

## Step 1 — Environment

```bash
cd <project> && python -m venv .venv && .venv\Scripts\pip install playwright beautifulsoup4 pandas && .venv\Scripts\python -m playwright install chromium
```

`playwright install chromium` fetches a private ~150 MB browser. It does not
touch system Chrome. Python 3.10+; built and tested on 3.13.

---

## Step 2 — `config.py`

Put **every** URL, selector, and tunable here and nowhere else. When the site
changes you want exactly one file to edit. See the shipped `config.py`; the
values that matter are documented inline with why they are what they are.

---

## Step 3 — `stores.py`: the store registry

`/pages/store-locator` embeds all 225 stores as JavaScript:

```js
currentStoreData = {"address":{...},"handle":"...","store_id":"3801",...};
currentStoreData.has_delivery = false;
window.stores["100-ave"] = currentStoreData;
```

1. Fetch the page with a normal desktop User-Agent.
2. Find each `currentStoreData = {` and **brace-match** to its closing `}`,
   tracking string literals and escapes.
3. `json.loads` each object; filter by `address.province`.

> [!WARNING]
> Do not use an HTML parser — the data is in JS, not the DOM. And do not use a
> lazy regex like `\{.*?\}`; it truncates on the nested `address` and
> `hours_periods` objects.

**Checkpoint:** `python stores.py --refresh` should print 225 total —
Ontario 100, Alberta 92, Saskatchewan 13, Manitoba 12, BC 8.

---

## Step 4 — `catalog.py`: catalog + watchlist

Page `products.json?limit=250&page=N` until a page comes back empty
(~30 pages), flattening to **one row per variant** — the variant carries the
SKU and price. Cache 24h.

Resolve each `watchlist.txt` line by: exact SKU → exact handle (strip a URL to
its `/products/<handle>` segment) → case-insensitive title substring.

**Report unmatched lines loudly.** A silently dropped watchlist entry is an
easy way to get a quietly incomplete dataset.

**Checkpoint:** `python catalog.py` → 7,363 distinct products, 7,375 variants.

---

## Step 5 — Discover the store/age-gate state

Run `python discover.py`. It seeds the age gate, reloads, and diffs
localStorage + cookies. Confirmed keys:

```
global_store_id   '8420'      <- the selected store
global_handle     'haxton'
global_store      {...full store JSON...}
global_province   'Alberta'
age_verification_pickup / age_verification_delivery
latitude_ai / longitude_ai    <- geolocation, used for auto-select
```

`global_store_id` and `global_province` are set as **cookies as well as**
localStorage.

Use `python discover.py --manual` to pick a store by hand and capture the diff
if the site ever changes.

---

## Step 6 — `browser.py`: contexts and store switching

Two non-obvious requirements, both learned by failing first:

**Use a fresh context per store, not one long-lived context.**
The site re-derives a store from geolocation on navigation and *will overwrite
a store you set after page load*. Seed state with `add_init_script`, which
runs before any page script. A fresh context also cannot leak the previous
store's cookies into the next store's pages.

**Pin `latitude_ai`/`longitude_ai` to the store's own coordinates** so the
site's auto-select agrees with you instead of fighting you.

**Then verify the switch actually took, every time:**

```python
active = await page.evaluate("() => localStorage.getItem('global_store_id')")
if str(active) != str(store["store_id"]):
    raise RuntimeError(...)
```

> [!CAUTION]
> A silent no-op here labels one store's prices as 92 different stores. Assert
> and raise; never let it pass quietly. Cross-check the header too — it renders
> `Pickup | Airdrie, Airdrie`.

---

## Step 7 — `scrape.py`: extraction

Confirmed selectors, scoped to the product block:

| Field | Selector |
|---|---|
| Market price | `[id^=ProductInfo] .js-market-table-price` |
| Member price | `[id^=ProductInfo] .js-member-table-price` |
| Stock | `button[name=add]` text + `disabled` |

Three gotchas, all of which produced wrong data before being handled:

1. **Scope to `ProductInfo`.** A second price table exists elsewhere on the
   page and sits at `Loading` forever. An unscoped selector may grab it.
2. **`N.A.` means "not carried at this store"**, not an error. The site's own
   code returns empty when `retail_price == 0`. Record it as `carried=0` — it
   is one of the most useful signals in the dataset.
3. **Wait past the `Loading` placeholder.** Wait for text that is a number *or*
   `N.A.`, never merely non-empty.

Retry a variant up to `MAX_RETRIES` before recording an error — pricing JS
occasionally fails to settle on first load, and an unretried blip is
indistinguishable from real "no price" data.

Scrape stores **sequentially** with randomized 2–5s delays.

### Pickup mode is mandatory, or Calgary silently collapses ⚠️

The page picks its pricing store with `getEffectiveStoreId()`:

```js
const HUB_STORE_MAP = { 'district': '3130', 'eastlake': '3170' };
return isDeliverySelected && HUB_STORE_MAP[store.hub_id]
  ? HUB_STORE_MAP[store.hub_id]   // the hub
  : storeId;                      // the real store
```

Seed `age_verification_delivery = "false"` (pickup). In **delivery** mode all 36
Calgary-area stores carrying a `hub_id` are priced as one of two hubs, so they
report identical stock and you cannot tell which shop actually holds the item:

| Store | Delivery mode | Pickup mode |
|---|---|---|
| Bowness | 3 units (as 3130) | **13 units** |
| Southland | 3 units (as 3130) | **5 units** |
| Brentwood | 3 units (as 3130) | **1 unit** |

Same product, same minute. Delivery mode is not wrong — it answers a
delivery-zone question — but it cannot answer "who has it on the shelf."

### Verify which store was actually priced

Attach a passive response listener to
`POST app.cannacabana.com/api/product/scan-single-item/<id>` and compare that
`<id>` to the store you selected. Record it as `api_store_id` /
`store_id_match`.

In pickup mode these should always agree. Keep the check anyway — it is what
caught the hub substitution in the first place, and a `store_id_match=0` is
now a precise signal that the delivery flag has regressed.

> [!NOTE]
> This only *observes* a call the page makes anyway. It does not call the API.

### Read the response body too, not just the URL

That same reply carries more than the DOM ever renders. Capture it with an
async handler (`asyncio.create_task` to read the body while it is retained):

```
{"scanned-items":{"203012":"3,0.00,35.99,each,98.10,0.60,4.00,588:203012"},
 "elitePrices":{"203012":"29.52"}}
```

Positional CSV: `[0]` stock qty, `[1]` member price, `[2]` retail price,
`[6]` gram equivalence. Fields 4–5 unidentified — keep the raw string.

This yields **exact stock counts** (17 / 7 / 3 units at three stores that all
render an identical "In Stock") and the ELITE price, which never appears in
the DOM. Zero extra requests.

It also resolves an ambiguity the DOM cannot: `api_price == 0` proves "not
carried", whereas an empty price cell might just be one still loading. Prefer
the API answer for the carried/not-carried decision.

> [!WARNING]
> Never record `carried=1` with a null price. That combination means the price
> cell was still on its placeholder — error and retry instead, or you will
> bank blanks as though they were real observations.

---

## Step 8 — `db.py`: SQLite + CSV

One append-only `observations` table, primary key `(run_id, store_id, sku)`.
That key + `INSERT OR REPLACE` is what makes re-running a partial sweep
idempotent, and therefore makes `--resume` safe.

**Write after every store**, not at the end — a crash at store 80 of 92 must
not cost the first 79.

---

## Step 9 — `main.py`: orchestration

Flags: `--province --limit --store --refresh-stores --refresh-catalog
--headed --resume`. Wrap each store in try/except so one bad store cannot kill
a 92-store sweep.

---

## Step 10 — Verification (in this order)

1. `python stores.py --refresh` → 225 stores, 92 in Alberta.
2. `python catalog.py` → 7,363 products; every watchlist line resolves.
3. `python main.py --limit 3` → rows with prices, no errors.
4. **The decisive test.** Scrape one SKU at two distant stores and confirm the
   header label differs and the data differs:

   ```bash
   .venv\Scripts\python main.py --store 8230 --store 3412 --csv test_results.csv
   ```

   Verified result: SKU 114205 member price **$15.44 in Calgary vs $15.24 in
   Medicine Hat**. Market price is often uniform chain-wide — *stock* and
   *member price* are where stores actually differ.

   > If every value is identical across distant stores, do not assume success —
   > confirm in a normal browser that the store switch isn't silently
   > no-opping.

5. Interrupt a run with Ctrl-C, restart with `--resume <run_id>`, confirm it
   continues rather than restarting.
6. Full province sweep.

---

## Step 11 — Schedule (optional)

```bash
schtasks /create /tn "CannaCabanaScraper" /tr "C:\Users\aj\Desktop\CannaCabanaScraper\.venv\Scripts\python.exe C:\Users\aj\Desktop\CannaCabanaScraper\main.py" /sc daily /st 07:00
```

Daily runs + the SQLite history give you price-change tracking for free.

---

## The fast backend

`POST app.cannacabana.com/api/product/scan-multiple-items/<store_id>` with
`{"skus":[{"<sku>": <variant_id>}]}` returns the same positional CSV the
browser path already decodes, for **every SKU in one call**. Cost scales with
stores, not stores × products.

Probe before building — these were the answers that shaped the design:

| Question | Answer |
|---|---|
| Auth required? | **No.** Answers unauthenticated; use no credentials |
| Rate limit? | `X-RateLimit-Limit: 60`/min, and 429s on short bursts |
| Bad store id? | HTTP 500 with a PHP stack trace — handle non-200 explicitly |
| Per-store data? | Yes, distinct and matching the browser exactly |

So: pace **sequentially at ~50/min**. Concurrency buys nothing — at ~0.7 s
latency the rate limit is the binding constraint, and bursts just earn 429s.

`getEffectiveStoreId` does not exist on this path: we pass the real store_id,
so the Calgary hub collapse is structurally impossible here.

> [!IMPORTANT]
> Build `--compare` alongside it, and run it before trusting the fast path. It
> runs both backends over the same stores and diffs every field. A backend
> returning stale data or another store's numbers produces output that looks
> perfectly normal — this is the only thing that catches it. Exclude rows where
> either backend errored, or you will blame the wrong one.

Ethically this is a judgement call the project owner should make deliberately:
it is an undocumented internal endpoint, and though no credentials or employee
access are involved and the data is the same public pricing the page shows,
it can be rotated or restricted without notice. Keep the browser backend
working so that is a config change, not a rewrite.
