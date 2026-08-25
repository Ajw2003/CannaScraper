# Canna Cabana Cross-Location Scraper

Canna Cabana's site only shows one store at a time. This tool sweeps many
stores automatically and produces a comparable price/stock dataset.

Everything here is **free**: Python, Playwright's bundled Chromium, SQLite,
and Windows Task Scheduler. No paid APIs, no proxies, no accounts.

---

## Quick start

**Just want to use it?** Build the app once and run that — no Python, no
setup, and it can serve a public URL:

```bash
.\build.ps1
```

That produces `dist\CannaCabana\`. Run `CannaCabana.exe`, and it prints a
local URL, a LAN URL, a public `trycloudflare.com` URL, and an admin password.
Zip the folder to hand it to someone else. See **The app** below.

**Working on the code?**

```bash
python -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt && .venv\Scripts\python -m playwright install chromium
```

(`requirements.txt` is the three packages the app itself needs;
`requirements-dev.txt` adds Playwright for the browser backend and PyInstaller
for the build. Playwright is only needed for the `--fetcher browser` path.)

Then edit `watchlist.txt`, and:

```bash
.venv\Scripts\python main.py --limit 3
```

That scrapes your watchlist at 3 Alberta stores and writes `results.csv`.
When it looks right, drop `--limit` for the full province.

Check everything still works at any time:

```bash
.venv\Scripts\python selftest.py
```

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

### How it is stored

`observations` is a **view**, not a table. Every query in this README works
against it exactly as written; this section only matters if you are adding a
column or writing directly to the database.

| Table | Rows | Holds |
|---|---|---|
| `products` | 5,322 | `handle`, `title`, `brand`, `category`, `size`, `image` |
| `store_meta` | 225 | `store_name`, `city`, `province` |
| `obs` | 295,512 | everything that varies per observation |

The old single table wrote every product and store fact onto every row, so a
title was stored ~55 times over and a store name ~1,300 times. Splitting it
took the file from **203 MB to 97 MB**. The view rejoins the three tables and
presents the same 34 columns in the same order, so `SELECT *` is unchanged.

`url` is not stored at all — it is rebuilt from `handle` + `store_id`, which
was verified to reproduce all 295,512 stored urls exactly.

Three things deliberately stayed per-observation, because they are **not**
product attributes despite looking like them:

- **`thc` / `cbd`** — per *lot*, not per product. 899 SKUs carry more than one
  THC value, because different stores hold differently-tested batches.
- **`default_price`** — a price, and prices are the thing this database exists
  to track over time.

Writes must go through `db.write_rows()`, which keeps the two lookup tables in
step with `obs`. Writing to `obs` directly will let them drift apart.

A database created before the split is refused with instructions rather than
migrated silently:

```bash
python normalize_db.py
```

It backs up to `history.db.pre-normalize`, verifies all 34 columns of all
295,512 rows against the original, and only then drops the old table. Rollback
is deleting the new file and renaming the backup.

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
| `db.py` | SQLite history + CSV export; owns the schema and the `observations` view |
| `normalize_db.py` | One-time migration of a pre-split database (backup + verify + swap) |
| `db_bench.py` | Times the read queries, for judging a schema change |
| `payload_probe.py` | Measures how much of the search response we keep, and whether the server will send less |
| `scan_ceiling_probe.py` | Finds the SKU-per-call ceiling on `scan-multiple-items` |
| `verify_scan_fix.py` | Checks the scan backend reads a "Bag Changed" response as data, not failure |
| `main.py` | Orchestration and CLI |
| `discover.py` | Re-derives the store/age-gate state keys if the site changes |
| `app.py` | The application: banner, web server, tunnel. What the exe runs |
| `paths.py` | Where data lives — the project folder, or `%LOCALAPPDATA%` when packaged |
| `jobs.py` | One worker thread, one queue: index builds and live checks, never at once |
| `auth.py` | `settings.json`, the admin password, session cookies |
| `tunnel.py` | Runs `cloudflared`, finds the public URL, dies with the app |
| `server.py` | FastAPI routes behind the web UI |
| `selftest.py` | 19 checks against a throwaway data dir; no network |
| `ratelimit.py` | Records how close each run came to the request budget |
| `ratelimit_probe.py` | Dev tool: measures the real rate limit. Not bundled |
| `build.ps1` | Numbered, pass/fail build into `dist\CannaCabana\` |

## The app

`build.ps1` packages everything into `dist\CannaCabana\` — a folder you can
zip and give to anyone with Windows. No Python, no venv, no Playwright.

```
==============================================================
  Canna Cabana stock
==============================================================
  This computer :  http://127.0.0.1:8000
  Phone / LAN   :  http://192.168.x.x:8000
  Public        :  https://<random-words>.trycloudflare.com
  Admin password:  2z2m-pzer-eexb    <-- write this down
  Data          :  C:\Users\<you>\AppData\Local\CannaCabana
--------------------------------------------------------------
  Reading is open to anyone with the link.
  Refreshing a province needs the password.
```

It stays in the foreground and streams job progress, because an index build
takes between 3 and 49 minutes and that belongs where you can watch it.

| Flag | Effect |
|---|---|
| `--no-tunnel` | Local and LAN only, no public URL |
| `--port N` | Override the saved port |
| `--no-browser` | Do not open a browser window on start |
| `--set-password PW` | Change the admin password (signs out existing sessions) |
| `--selftest` | Run the built-in checks and exit |

**Public URL.** Bundled `cloudflared` opens a quick tunnel: no account, no
domain, no port forwarding, works behind CGNAT. The hostname is random and
changes on every start, and quick tunnels are best-effort with no SLA. For a
link that keeps working, create a named tunnel in the Cloudflare dashboard and
put its token in `settings.json`:

```json
{ "tunnel": "named", "tunnel_token": "eyJhIjoi..." }
```

Because the tunnel is HTTPS, the "📍 Near me" button works on phones — browsers
refuse geolocation over plain http, so the LAN URL cannot offer it.

**Who can do what.** Searching and viewing stock are open to anyone with the
link. Anything that makes your machine talk to cannacabana.com — a live
re-check, a province rebuild — needs the admin password. That password is
printed once on first run and only its hash is stored.

If you miss it, **double-click `Set password.bat`** next to the exe: it
prompts for a new one, twice, without echoing. Safe to run while the app is
up — the server re-reads the password on every login attempt, so the new one
works immediately without a restart. Everyone signed in gets signed out,
because session cookies are signed with the password hash.

`--set-password "value"` still works for scripting. Note that `--set-password`
with no value needs a real console; run from a pipe it says so and exits
rather than hanging.

**Settings** live in `%LOCALAPPDATA%\CannaCabana\settings.json`, alongside
`history.db` and the catalogue. Deleting that folder resets the app.

### Running from source instead

```bash
.venv\Scripts\python app.py          # the same app, tunnel and all
.venv\Scripts\python server.py       # local + LAN only, no tunnel
```

In a source checkout the data directory is the project folder, so `history.db`
and the caches stay exactly where they always were.

Type a product, pick it from the thumbnails, and get every store ranked by
units on hand. **Check live now** re-queries the stores with a progress bar
(1–2 s per store) rather than blocking the page.

It has the same reach as `find.bat`:

| Control | Equivalent CLI |
|---|---|
| Province dropdown (with store counts) | `--province Ontario` |
| `5 / 10 / 25 nearest` or `Whole province` | `--top N` / `--all` |
| City or postal code box | `--near "Lethbridge, AB"` |
| 📍 button | device geolocation (no CLI equivalent) |
| `Live: fast API` / `Live: real browser` | `--fetcher api` / `--fetcher browser` |
| Page loads from the index | `--cached` |
| **Check live now** | `--refresh` |

Typing a place overrides the 📍 location, and picking *Whole province* hides
the location row since it no longer applies.

| Route | Does |
|---|---|
| `GET /api/provinces` | provinces + store counts, from the registry |
| `GET /api/search?q=` | catalogue search — instant, no network |
| `GET /api/results?sku=&lat=&lng=&near=&top=&province=&all_stores=` | index/cache answer — instant |
| `POST /api/refresh?...&fetcher=` | starts a live re-check, returns a `job` id |
| `GET /api/job/{id}` | `{done, total, store, finished}` for the progress bar |

> [!NOTE]
> Browsers only permit geolocation on `https` or `localhost`, so 📍 will be
> refused when you load the page from a phone over plain `http://<lan-ip>`.
> The UI says so and falls back to the city box, which works everywhere.

The server adds no scraping logic — it reuses `catalog.search`,
`stores.nearest`, `db.latest_observations`, and the same fetchers the CLI uses.

### Product images

Images come from **Shopify's CDN, referenced directly** — never downloaded or
re-hosted. Two reasons it's free:

- The index's `search` response already includes image URLs, so capturing them
  costs no extra requests.
- Shopify resizes on demand: `?width=200` returns **36 KB** where the original
  is **811 KB** — a 22× saving that matters for a grid of results.

`config`-free helper `server.thumb(url, w)` appends the width parameter.

> [!NOTE]
> Hotlinking their CDN is exactly what a browser does when viewing the site, and
> is fine for personal use. If this ever ships commercially, image hosting and
> rights are worth settling alongside the data question.

## The stock index

Easiest way: open the app and use the **Catalogue index** panel at the top of
the page. Each province shows its coverage and how old it is, with a Refresh
button, live progress, a Cancel button, and Resume when a previous run stopped
early. Because the state lives on the server, a build you start on the desktop
is visible from your phone.

Only one build runs at a time — the site's rate limit is a single global
budget, so a second concurrent build would produce 429s rather than finish any
faster. Cancel takes effect at the next store boundary, up to ~29 seconds.

The command line does the same thing and is what a scheduled task should call:

```bash
.venv\Scripts\python index_builder.py                    REM Alberta, ~45 min
.venv\Scripts\python index_builder.py --province Ontario
.venv\Scripts\python index_builder.py --limit 5          REM try a few first
```

Indexes **every in-stock product at every store in a province** into the same
SQLite history the rest of the tool reads. Once built, lookups are **instant** —
no scraping, no waiting.

It uses `/api/product/search?title=a&storeId=<id>`, which returns a store's
entire in-stock catalogue 50 per page (~25 calls per store) with retail, member
and elite price, **exact quantity**, gram equivalence, and **THC/CBD levels**.

| Approach | Full Alberta index |
|---|---|
| Per-SKU `scan-multiple-items` | ~30 hours |
| **Paged `product/search`** | **~45 min** |

Roughly 1,000–1,250 products per store; ~110,000 rows for Alberta.

### The HTML report

Every store you checked appears — the ones with stock first, then the rest
marked **not in stock**, collapsed behind a toggle:

```
[x] Show 88 stores without it
Freeze Dried Rosin Rings Gummies · in stock at 4 of 92 checked
```

Two distinctions the page is careful about:

- **"not in stock"** vs **"not checked"** — the index only holds in-stock items,
  so a store with no row genuinely doesn't have the product. A store outside
  the index says "not checked" instead, so a coverage gap never poses as an
  answer.
- The header counts **stores checked**, not rows returned. "4 of 92" is the
  honest figure; counting rows would have said "4 of 4".

The toggle is **pure CSS** (a hidden checkbox plus a sibling selector), so the
report stays one self-contained file with no JavaScript — it opens from disk,
works offline, and can be emailed as-is.

### Pricing tiers — ELITE vs Member

Every product sits in exactly **one** discount tier. The site's own logic:

```js
if (member_price > 0 && is_elite === false)  → show "Member"
if (elite_price  > 0 && is_elite === true)   → show "Elite"
```

So a product is **either** Member-priced (free Cabana Club) **or** ELITE-priced
(paid tier) — never both. There is no elite-vs-member difference to show per
product, because only one ever applies. The meaningful comparison is
**tier price vs market price**, which is what gets displayed:

```
  units  store          km   market   tier  you pay          save
  Pufferz Grape Gas Disposable Vape   *** ELITE members only ***
     10  Kensington    2.2   $35.99  ELITE   $28.79  -$7.20 (20%)
      6  East Village  0.4   $35.99  ELITE   $28.44  -$7.55 (21%)
```

`is_elite` is stored per row. **16.3% of the Alberta index is ELITE-tier.**
Verified against both endpoints, which agree exactly (SKU 203012: retail 35.99 /
member 0 / elite 29.52 / is_elite true).

Worth noting: **market price is flat chain-wide, but the discounted tier price
varies by store** — the same vape is $28.44 in East Village and $29.90 in
Beltline. That spread is invisible on their site and only shows up here.

### Two things that were verified before trusting it

**It is exhaustive.** Eleven different search terms (`z`, `q`, `kush`, `og`,
`gsc`, `c4`, …) surfaced *zero* variants beyond what `title='a'` returned. Then
148 catalogue variants absent from the result were independently checked via
`scan-multiple-items`: **none were in stock** (61 explicitly not carried, the
rest carried-but-sold-out). The `title` parameter is required but does not
meaningfully filter.

**It only returns in-stock items.** A sold-out product simply vanishes from the
response rather than appearing with qty 0. So `index_builder._close_out()`
zeroes anything that was in stock at a store last run and is absent now.
Without it, `db.latest_observations()` would keep serving yesterday's in-stock
row as the freshest fact and send you to a store that has none — tested by
planting a stale row and confirming a re-index closes it out.

A consequence worth knowing: the index cannot distinguish *"never carried"*
from *"carried but sold out"*. For that, `--fetcher api` on a specific product
still gives the fuller answer.

### Keeping it warm

```bash
schtasks /create /tn "CannaIndex" /tr "C:\Users\aj\Desktop\CannaCabanaScraper\CannaScraper\.venv\Scripts\python.exe C:\Users\aj\Desktop\CannaCabanaScraper\CannaScraper\index_builder.py" /sc daily /st 05:00
```

Nightly index for breadth, `--refresh` for the one product in hand.

## Two backends

| | Browser | **API** (default) |
|---|---|---|
| 10 nearest stores, 1 product | ~85 s | **~22 s** |
| 92 stores, 1 product | ~18 min | **~3.4 min** |
| 92 stores, 5 products | ~60 min | **~3.4 min** |

The API backend calls the same endpoint the site's own page uses, and **one
call carries the whole watchlist for a store** — so cost scales with the number
of stores, not stores × products. That's why five products cost the same as one.

**No credentials are used.** The endpoint answers unauthenticated; the site
mints a token but never attaches it to this call, so neither do we.

**On pacing.** The endpoint advertises `X-RateLimit-Limit: 60`/min and 429s on
short bursts, so `API_RATE_PER_MIN = 50` caps how fast we may *start* requests.

Concurrency is a separate question, and the answer changed. This backend ran
strictly sequentially on the reasoning that at ~0.7 s latency the rate limit
binds and parallelism buys nothing. `scan-multiple-items` now answers in ~10 s,
so sequential calls spend the whole minute waiting and use 6 of the 60 requests
allowed. Measured over 8 stores:

| concurrency | wall clock | per store | requests/min | 429s |
|---|---|---|---|---|
| 1 | 79.8 s | 9.97 s | 6 | 0 |
| **6** | **17.3 s** | **2.16 s** | **28** | **0** |

`X-RateLimit-Remaining` never fell below 54 in either trial, so `API_CONCURRENCY`
is 6 — roughly half the allowance, with the pacer still capping the start rate.

This applies only to the live check. `index_builder.py` uses `product/search`,
which answers in **0.74 s** — under the 1.2 s pacer interval, so that endpoint
genuinely is rate-limit-bound and keeps its own sequential pacer. Concurrency
there would not raise throughput, only the odds of a 429. A larger page size
would be the real win, but anything above `limit=50` returns an empty set.

### Measured headroom

Every response carries `X-RateLimit-Limit` and `X-RateLimit-Remaining`, and
`ratelimit.py` records the low-water mark per run — no extra requests, just
reading headers we already receive. It prints at the end of an index or live
check, and the index panel shows the running picture:

```
[live]  rate limit: got within 52 of 60 at the tightest point over 8 calls
[index] rate limit: got within 14 of 60 at the tightest point over 38 calls
```

That gap is the whole story. A live check barely touches the budget, which is
why concurrency was free there. An index run at 50/min gets down to **14 of 60
remaining** — so the pacer is not being cautious for the sake of it, and
raising `API_RATE_PER_MIN` really would start drawing 429s.

History accumulates in `ratelimit.json` in the data directory. A 429 that
arrives with budget to spare is recorded separately and attributed to the store
that produced it, so a broken store can never masquerade as rate pressure.

### The limit, measured

`ratelimit_probe.py` answers this empirically instead of trusting the header.
It ran 297 requests on 2026-08-25 and drew exactly one 429:

| Question | Answer |
|---|---|
| Cost per request | **exactly 1 unit** (drops were 1,1,1,1,1,1) |
| Window | **fixed, 60 s** — the allowance snaps back at a wall-clock boundary rather than trickling |
| Shared across endpoints? | **yes** — 10 calls on `product/search` moved the *scan* endpoint's remaining from 59 to 46 |
| Where it refuses | **request 61 in a window**, with `Retry-After: 9` |

So the advertised 60 is exact and enforced. Two consequences worth knowing:

- **The shared budget is why `jobs.py` runs one job at a time.** An index at
  50/min plus a concurrent live check would breach one 60/min allowance, not
  two. That design was a guess when it was written; it is now evidence-based.
- **50/min leaves 10 spare per window**, and a clean 60/min run reached 4
  remaining. There is real headroom but not much, which is why
  `API_RATE_PER_MIN` stays where it is.

```bash
.venv\Scripts\python ratelimit_probe.py                 # safe phases, no 429
.venv\Scripts\python ratelimit_probe.py --find-ceiling  # ramps until refused
.venv\Scripts\python ratelimit_probe.py --dry-run       # plan only
```

Per-request data appends to `ratelimit_probe.csv`, conclusions to
`ratelimit_probe_summary.json` — separate from `ratelimit.json` so an
experiment never contaminates the operational telemetry.

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
**3.5 minutes** via the web UI; add `--limit 10` to sample quickly first.

(The CLI sweep in `main.py` still walks stores one at a time, so it takes
longer — roughly 15 minutes for the same 92 stores. Only the web UI's live
check was changed to issue them concurrently.)

`--product` accepts a SKU, handle, product URL, or title text, and can be
repeated. It overrides `watchlist.txt` for that run without editing anything.

### Checking every store in a province

`--top` limits to the nearest N. To sweep them all, use `--all`:

```bash
.venv\Scripts\python main.py --product 203012 --all
.venv\Scripts\python main.py --product 203012 --all --province Ontario
```

One product across a whole province, via the web UI's live check (measured at
~2.2 s per store; the CLI's sequential sweep is roughly 4.5× these figures):

| Province | Stores | Time (API) |
|---|---|---|
| Alberta (default) | 92 | ~3.4 min |
| Ontario | 100 | ~3.7 min |
| Saskatchewan | 13 | ~30 s |
| Manitoba | 12 | ~26 s |
| British Columbia | 8 | ~18 s |

`find.bat` also takes arguments, so you can skip the prompts:

```bash
find.bat "grape gas"                    REM nearest 10
find.bat "grape gas" 25                 REM nearest 25
find.bat "grape gas" 25 Calgary         REM nearest 25 to Calgary
find.bat "grape gas" all                REM every Alberta store
find.bat "grape gas" all Ontario        REM every Ontario store
```

### The launcher loops

Double-click `find.bat` and it keeps asking until you quit — search, read the
answer, search again, no reopening. `Q` (or Enter at an empty product prompt)
exits.

Passing arguments runs **once and exits**, so it stays scriptable and testable:

```bash
find.bat "grape gas" 5 Calgary live
```

### Three sources, one launcher

`find.bat` asks where the answer should come from, and every run prints which
one served it (`[source: STOCK INDEX — 0.5h old]`, `[source: LIVE via API]`, …).

| Choice | Source | 5 stores | Use when |
|---|---|---|---|
| **Index** (Enter) | nightly stock index | **1 s** | Normal use |
| **L** — Live | fast API, checked now | 8 s | Stock matters *right now* |
| **W** — Web | real browser | 22 s | API changed / verifying |

From the command line, add the keyword last:

```bash
find.bat "grape gas" 5 Calgary index    REM instant
find.bat "grape gas" 5 Calgary live     REM check now
find.bat "grape gas" 5 Calgary web      REM browser fallback
```

Or with `main.py` directly: `--cached`, `--fetcher api --refresh`,
`--fetcher browser --refresh`.

All three agree — the same query returned Kensington 10, Roxboro 7, East
Village 6 units by every path, which is a useful cross-check whenever the site
changes.

### Fresh vs cached

Interactively, `find.bat` asks — **Enter does a live check**, `C` reuses recent
results. From the command line, add `refresh` (or `r`) as the last argument:

```bash
find.bat "grape gas" 10 Calgary refresh
find.bat "grape gas" all "" refresh
```

A plain `main.py` run reuses cached results only if they're under
`config.CACHE_FRESH_H` (**1 hour**) old, then re-checks live. `--refresh`
always checks; `--cached` never does.

That window is deliberately short: a live 10-store lookup costs ~12 s now, and
stock moves fast enough that hours-old numbers will send you to a store that
just sold out. Raise it if you sweep whole provinces frequently.

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
