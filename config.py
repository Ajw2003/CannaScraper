"""Single source of truth for the scraper.

Every URL, selector, and tunable lives here. When the site changes, this is
the only file you should need to edit.
"""

import paths

# --- Endpoints -------------------------------------------------------------
BASE = "https://cannacabana.com"
LOCATOR_URL = f"{BASE}/pages/store-locator"
PRODUCTS_JSON = f"{BASE}/products.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# --- Scope -----------------------------------------------------------------
PROVINCE = "Alberta"   # matches store["address"]["province"] exactly
MAX_STORES = None      # set to a small int while developing; None = all

# --- "Near me" -------------------------------------------------------------
# Your default location, so `--near` is optional. Either a place string
# ("Calgary, AB", "T2P 1J9") or a (lat, lng) tuple. None = must pass --near.
HOME = "Calgary, AB"
DEFAULT_TOP = 10       # how many nearest stores a lookup checks by default
GEOCODE_CACHE = paths.data("geocode.json")
# Nominatim asks that clients identify themselves. Free, no key required.
NOMINATIM_UA = "CannaCabanaScraper/1.0 (personal stock lookup)"

# How stale cached results may be before a plain run re-checks live.
#
# This was 12h when a 10-store lookup cost ~90 seconds via the browser and
# caching was the only thing making the tool usable. The API backend does the
# same lookup in ~12 seconds, so stale data now costs more than a re-check
# does -- and stock moves fast (one store went 1 -> 10 units within hours).
# Raise it if you sweep whole provinces often; --cached always forces reuse.
CACHE_FRESH_H = 1

# --- Fetch backend ---------------------------------------------------------
# "browser" = drive a real Chromium (~8s per store per product).
# "api"     = the site's own pricing endpoint: one call returns the whole
#             watchlist for a store, so cost scales with STORES only, not
#             stores x products.
#
# Default is "api" only because `--compare` showed zero disagreements with the
# browser across every field (price, member price, stock, carried, available).
# The browser backend stays fully working: set this to "browser", or pass
# --fetcher browser, if the endpoint ever changes shape.
FETCHER = "api"

# --- API backend -----------------------------------------------------------
API_BASE = "https://app.cannacabana.com/api"
API_SCAN = API_BASE + "/product/scan-multiple-items/{store_id}"

# Verified by probe: the endpoint answers unauthenticated. We therefore send
# NO credentials at all. The site's own page mints a token from a client_id /
# client_secret in its source, but never attaches it to this call -- so there
# is no reason for us to touch those credentials.
# If they ever start requiring auth, flip this on and fill in the token URL.
API_SEND_AUTH = False
API_TOKEN_URL = API_BASE + "/oauth/token"
API_CLIENT_ID = ""        # intentionally blank; see above
API_CLIENT_SECRET = ""

# Server advertises X-RateLimit-Limit: 60 (per minute) and 429s on short
# bursts, so API_RATE_PER_MIN stays under that as a ceiling on how fast we may
# start requests.
#
# API_CONCURRENCY was 1, on the reasoning that at ~0.7s latency the rate limit
# is the binding constraint and parallelism buys nothing. That stopped being
# true: scan-multiple-items now answers in ~10s, so sequential calls spend the
# whole minute waiting and use 6 of the allowed 60 requests. Measured over 8
# stores, one product:
#
#     concurrency 1    79.8s    9.97s/store    6 req/min   0x 429
#     concurrency 6    17.3s    2.16s/store   28 req/min   0x 429
#
# X-RateLimit-Remaining never fell below 54 in either trial. Six leaves about
# half the allowance unused, and the pacer above still caps the start rate, so
# if their latency ever recovers we throttle ourselves rather than them.
#
# This applies only to the live per-SKU check. index_builder.py uses the
# separate product/search endpoint, which still answers in ~1.2s and IS
# rate-limit-bound; it has its own sequential _Pacer and should keep it.
API_RATE_PER_MIN = 50      # deliberate headroom under the advertised 60
API_CONCURRENCY = 6
API_MAX_RETRIES = 3
API_TIMEOUT_S = 30

# Stores the scan endpoint will not serve.
#
# NOT a general block-list. These stores index perfectly well via
# product/search -- 528 has 827 clean rows from it -- so they stay in the
# registry and in the province counts. Only the per-SKU live check skips them.
#
# 528 (Gateway Village, St. Albert) has failed every scan attempt since
# 2026-08-17: 15x HTTP 500 and 2x 429, and it is the ONLY store that has ever
# produced an API error in 295k rows. Each 429 there costs 90s of backoff for
# a store that will not answer.
#
# A packaged copy can add its own without a rebuild, via a "scan_skip_stores"
# list in settings.json; the two are merged at runtime.
SCAN_SKIP_STORES = {"528"}

# Re-test a skipped store this often, so a store that gets fixed comes back on
# its own instead of staying dead until someone remembers to check. A failed
# re-test writes an error row, which resets the clock.
SCAN_SKIP_RETRY_DAYS = 7

# Measured wall-clock per store for a live check at the concurrency above.
# Used only to put an honest "this will take N minutes" on the button before
# someone starts a 100-store sweep. Re-measure if the endpoint's latency
# changes again; being wrong here costs nothing but a bad estimate.
LIVE_SECONDS_PER_STORE = 2.2

# --- Egress pool (index throughput) ----------------------------------------
# An index run is rate-limit-bound, not latency-bound: product/search answers
# in ~0.74s, under the 1.2s pacer interval, so ~2,300 requests at 50/min is
# ~46 minutes and threading a single route changes nothing. The probe in
# ratelimit_probe_summary.json shows why -- 1 unit per request, a FIXED ~60/min
# window, and ONE budget shared between search and scan.
#
# The only lever left is holding more than one budget. Each proxy below is a
# separate route with its own pacer, so four working routes turn ~46 minutes
# into ~12.
#
# EMPTY BY DEFAULT, and that default is deliberate. Whether the budget is
# actually keyed on source address is unverified -- if it keys on something
# every route also sends, the pool buys nothing and just spends the same 60/min
# faster. Prove it before relying on it:
#
#     python index_builder.py --probe-egress
#
# One request per route; it prints each route's X-RateLimit-Remaining and says
# plainly whether the counters are independent or shared.
#
# A packaged copy can add routes without a rebuild via "egress_proxies" in
# settings.json; the two lists are merged. Note that proxy URLs routinely carry
# credentials, and settings.json is a plain file in the data directory --
# egress.describe() strips them before anything is printed or served, but the
# file itself is as sensitive as the credentials in it.
EGRESS_PROXIES: list[str] = []      # e.g. ["http://user:pass@host:8080"]
EGRESS_INCLUDE_DIRECT = True        # this machine's own address is route 1
EGRESS_RATE_PER_MIN = 50            # PER ROUTE, with headroom under the 60
EGRESS_FAIL_LIMIT = 3               # consecutive failures before a cooldown
EGRESS_COOLDOWN_S = 120             # first cooldown; escalates per round
EGRESS_COOLDOWN_MAX_S = 900

# One-sided pacer jitter: the gap between requests is multiplied by
# 1 + uniform(0, EGRESS_JITTER), so it only ever grows. That keeps
# EGRESS_RATE_PER_MIN a hard ceiling (a symmetric jitter would let short
# bursts exceed it) at a cost of ~6% throughput, and stops workers that came
# out of a shared stall from re-aligning on the same instant. Retry backoff is
# separately jittered -- see egress.backoff().
#
# This is for de-synchronisation, not for disguise. Nothing here rotates user
# agents or shapes traffic to look human.
EGRESS_JITTER = 0.12

# Index workers. None = one per healthy route, which is the right answer:
# workers beyond that share a budget and only raise the odds of a 429.
INDEX_WORKERS = None

# --- Index work queue ------------------------------------------------------
# Store-level work items live in the `work_queue` table (workqueue.py), so a
# run survives the process dying and N workers can claim from it atomically.
WORK_LEASE_S = 300          # a claim held longer than this is redelivered
WORK_MAX_ATTEMPTS = 3       # tries per store before the run gives up on it
WORK_CLAIM_TRIES = 8        # candidates to race for before yielding
WORK_KEEP_RUNS = 20         # queue rows retained; observations are the record

# --- Browser ---------------------------------------------------------------
HEADLESS = True
NAV_TIMEOUT_MS = 45_000
DELAY_RANGE = (2.0, 5.0)   # random sleep between requests, seconds
MAX_RETRIES = 3

# --- Storage ---------------------------------------------------------------
# Absolute, resolved by paths.py. They used to be bare filenames, which meant
# the tool silently created an empty database whenever it was launched from
# anywhere but this directory -- and a packaged app is never launched from
# here. paths.seed() copies the bundled catalogue/store list into the data
# directory on first run; in a source checkout it is a no-op.
DB_PATH = paths.data("history.db")
CSV_PATH = paths.data("results.csv")
RAW_DIR = paths.data("raw")
STORES_CACHE = paths.seed("stores.json")
CATALOG_CACHE = paths.seed("catalog.json")
CATALOG_MAX_AGE_H = 24
WATCHLIST = paths.seed("watchlist.txt")

# --- Store / age-gate state ------------------------------------------------
# CONFIRMED by discover.py against the live site (Step 5 of the plan).
#
# Selecting a store writes these localStorage keys:
#     global_store_id      '8420'          <- the selected store
#     global_handle        'haxton'
#     global_store         {...full store JSON...}
#     global_province      'Alberta'
#     global_store_pickup  'Pickup'
#     global_store_status  'Open'
#
# Selecting a store ALSO sets cookies `global_store_id` and `global_province`.
# We set the cookies too, to match what a real session looks like -- but note
# the page HTML comes back byte-identical regardless, so the cookie alone buys
# you nothing. The pricing is applied client-side (see the selector notes
# below); the localStorage state is what the pricing JS actually reads.
STORE_ID_KEY = "global_store_id"
STORE_HANDLE_KEY = "global_handle"
STORE_OBJ_KEY = "global_store"
PROVINCE_KEY = "global_province"
STORE_PICKUP_KEY = "global_store_pickup"
STORE_STATUS_KEY = "global_store_status"

# age_verification_delivery MUST stay "false".
#
# The product page chooses its pricing store via getEffectiveStoreId():
#
#   const HUB_STORE_MAP = { 'district': '3130', 'eastlake': '3170' };
#   return isDeliverySelected && HUB_STORE_MAP[store.hub_id]
#     ? HUB_STORE_MAP[store.hub_id]   // the hub
#     : storeId;                      // the real store
#
# In DELIVERY mode every store carrying a `hub_id` (37 of them, all Calgary
# area) is priced as its hub instead of itself -- so all 17 "district" stores
# report one identical price and stock, as do all 20 "eastlake" stores.
# In PICKUP mode the real store_id is used and each store reports its own
# inventory, which is what "who actually has this on the shelf" means.
AGE_GATE_STATE = {
    "age_verification_pickup": "true",
    "age_verification_delivery": "false",
}
GEO_KEYS = ("latitude_ai", "longitude_ai")

# --- Selectors -------------------------------------------------------------
# CONFIRMED against rendered product pages.
#
# The raw HTML is byte-identical for every store -- per-store pricing is
# applied CLIENT-SIDE by JavaScript after load. That is why this project needs
# a real browser and cannot be done with plain HTTP requests. Do not "optimize"
# this into a requests/BeautifulSoup fetch; you will get one price for all
# 92 stores and never notice.
# Scope prices to the ProductInfo block: a second, unrelated price table also
# exists on the page and sits at "Loading" indefinitely.
SEL_PRODUCT_INFO = "[id^=ProductInfo]"
SEL_MARKET_PRICE = "[id^=ProductInfo] .js-market-table-price"  # e.g. "$18.99"
SEL_MEMBER_PRICE = "[id^=ProductInfo] .js-member-table-price"  # e.g. "$15.64"
SEL_ADD_BUTTON = "button[name=add], .product-form__submit"

# The site renders "N.A." as the market price when a product is not carried at
# the selected store (its own JS bails out when retail_price == 0). That is a
# real per-store signal, not a scrape failure -- record it, don't error on it.
NOT_CARRIED_TOKENS = ("n.a.", "n/a", "na")
PRICE_PLACEHOLDER = "loading"

# The header renders the active store as e.g. "Pickup | Haxton, Fort McMurray".
# This is what we assert against after every store switch.
SEL_STORE_LABEL = "[class*='store-name'], [class*='current-store'], .header__store"

# How long to let client-side pricing JS settle before reading the DOM.
PRICE_SETTLE_MS = 9_000
