"""Single source of truth for the scraper.

Every URL, selector, and tunable lives here. When the site changes, this is
the only file you should need to edit.
"""

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
GEOCODE_CACHE = "geocode.json"
# Nominatim asks that clients identify themselves. Free, no key required.
NOMINATIM_UA = "CannaCabanaScraper/1.0 (personal stock lookup)"

# How stale cached results may be before we suggest a refresh, in hours.
CACHE_FRESH_H = 12

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
# bursts. We pace below that and stay sequential -- at ~0.7s latency the rate
# limit, not concurrency, is the binding constraint, so parallelism buys
# nothing and only risks a ban.
API_RATE_PER_MIN = 50      # deliberate headroom under the advertised 60
API_CONCURRENCY = 1
API_MAX_RETRIES = 3
API_TIMEOUT_S = 30

# --- Browser ---------------------------------------------------------------
HEADLESS = True
NAV_TIMEOUT_MS = 45_000
DELAY_RANGE = (2.0, 5.0)   # random sleep between requests, seconds
MAX_RETRIES = 3

# --- Storage ---------------------------------------------------------------
DB_PATH = "history.db"
CSV_PATH = "results.csv"
RAW_DIR = "raw"
STORES_CACHE = "stores.json"
CATALOG_CACHE = "catalog.json"
CATALOG_MAX_AGE_H = 24
WATCHLIST = "watchlist.txt"

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
