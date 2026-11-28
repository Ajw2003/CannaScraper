// static-api.js -- answers web/index.html's fetch('/api/...') calls from the
// published JSON files (data/<slug>.json, data/catalog.json, data/stores.json,
// data/geocode.json) instead of a running server.py. The page code is
// otherwise untouched, so every route here is a line-for-line port of the
// server function that used to answer it; each is commented with its source.
//
// The page fetches absolute paths like '/api/search?...'. GitHub Pages serves
// this site under a subpath (e.g. /CannaScraper/), so that absolute path does
// NOT point at this page's own directory -- but since we intercept
// window.fetch before the browser ever sends the request, that mismatch never
// matters: we only look for the '/api/' segment in the URL, not the exact
// path, and everything else (data/...) is fetched relative to this page.
//
// Known, documented differences from server.py -- all data-shape limits of a
// static export, not logic bugs:
//   - province_facts' `category` is MAX(o.category) across per-store rows in
//     the live DB; the static export keeps one category per SKU (the first
//     row seen), so a SKU whose category differs by store (not observed in
//     practice) would not get the lexicographically-greatest one here.
//   - api_results' per-request `age_hours` (this product last checked) is
//     approximated from the STORE's newest scrape time for stores that have a
//     real (non-synthetic) row for this SKU, rather than a true per-row
//     scraped_at (not carried in the export -- see ci/export_province.py).
//   - `carried` is not exported (the front end never reads it).

(function () {
  'use strict';

  const originalFetch = window.fetch.bind(window);

  // --- config (mirrors config.py) ------------------------------------------
  const DEFAULT_PROVINCE = 'Alberta';
  const HOME = 'Calgary, AB';
  const DEFAULT_TOP = 10;
  const CATALOG_MAX_AGE_H = 24;
  const API_BASE = 'https://app.cannacabana.com/api';
  const API_RATE_PER_MIN = 50;     // config.py:API_RATE_PER_MIN
  const API_MAX_RETRIES = 3;       // config.py:API_MAX_RETRIES
  const SCAN_SKIP_RETRY_DAYS = 7;  // config.py:SCAN_SKIP_RETRY_DAYS
  const API_CONCURRENCY = 6;       // config.py:API_CONCURRENCY

  // Live-check results for this visitor only (never persisted -- the original
  // server wrote them to the shared history DB; a static page has nowhere
  // shared to write them, so they only patch what THIS browser then sees):
  // LIVE_OVERRIDES[province][sku][store_id] = a stock-row-shaped object.
  const LIVE_OVERRIDES = {};

  // server.py:113-119
  const MG_CATEGORIES = new Set([
    'edibles', 'gummies', 'beverages', 'chocolates', 'oils & capsules',
    'capsules', 'oils', 'topicals', 'soft chews', 'mints', 'baked goods',
    'capsules & soft gels', 'oils & caps',
  ]);

  // --- data loading, cached per file (paths.py CATALOG_CACHE etc, but read
  // from the published data/ directory, relative to this page) -------------
  const _cache = new Map();
  function loadJSON(path) {
    if (!_cache.has(path)) {
      _cache.set(path, originalFetch(path).then(r => {
        if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
        return r.json();
      }));
    }
    return _cache.get(path);
  }

  function slugify(province) {
    // ci/export_province.py:slugify
    return province.trim().toLowerCase().split(' ').join('-');
  }

  function loadProvince(province) {
    return loadJSON(`data/${slugify(province)}.json`);
  }
  function loadCatalog() {
    return loadJSON('data/catalog.json');
  }
  function loadStores() {
    return loadJSON('data/stores.json');
  }
  function loadGeocodeCache() {
    // Best-effort: a site with no geocode.json published yet still works,
    // just with an empty starting cache (matches stores.geocode()'s own
    // "file may not exist" handling).
    return loadJSON('data/geocode.json').catch(() => ({}));
  }

  // --- potency formatting: server.py:122-178 --------------------------------

  // Python's f"{n:g}" (6 significant digits, trailing zeros stripped). Every
  // value this project ever formats is a plain percentage or mg figure
  // (0 < n < ~3000), which %g always renders in fixed notation (never
  // scientific) at that precision -- so this only needs the fixed-notation
  // branch of %g, not the general algorithm.
  // Python's format(x, '.Nf'). JavaScript's toFixed rounds an exact half up
  // (31.25 -> "31.3"); Python rounds it to even (31.25 -> "31.2"). Both work
  // from the exact binary value, so they only disagree when that value is
  // exactly halfway -- which a real THC reading of 31.25 is (CI parity,
  // 2026-09-28: the card said "31.2-31.3%" where the original said "31.2%").
  function pyFixed(x, dp) {
    const exact = x.toFixed(Math.min(100, dp + 30));
    const cut = exact.indexOf('.') + (dp > 0 ? dp + 1 : 0);
    const kept = exact.slice(0, dp > 0 ? cut : exact.indexOf('.'));
    const rest = exact.slice(exact.indexOf('.') + dp + 1);
    const isHalf = /^50*$/.test(rest);
    const lastDigit = Number(kept.replace('.', '').slice(-1));
    if (isHalf && lastDigit % 2 === 0) return kept;
    return x.toFixed(dp);
  }

  function pyG(n) {
    if (n === 0) return '0';
    const exp = Math.floor(Math.log10(Math.abs(n)) + 1e-9);
    const dp = Math.max(0, 5 - exp);
    let s = pyFixed(n, dp);
    if (s.indexOf('.') !== -1) s = s.replace(/0+$/, '').replace(/\.$/, '');
    return s;
  }

  function potency(value, category) {
    const n = parseFloat(value);
    if (!isFinite(n)) return '';
    if (n <= 0) return '';
    const cat = (category || '').trim().toLowerCase();
    if (MG_CATEGORIES.has(cat) || n > 100) return `${pyG(n)} mg`;
    return `${pyG(n)}%`;
  }

  function potencySpan(span, category) {
    if (!span) return '';
    const [lo, hi] = span;
    if (lo === null || lo === undefined || hi === null || hi === undefined) return '';
    const dp = hi >= 1 ? 1 : 2;
    if (Number(pyFixed(lo, dp)) === Number(pyFixed(hi, dp))) return potency(lo, category);
    const cat = (category || '').trim().toLowerCase();
    const unit = (MG_CATEGORIES.has(cat) || lo > 100) ? ' mg' : '%';
    return `${pyFixed(lo, dp)}–${pyFixed(hi, dp)}${unit}`;
  }

  // db.py:574-586
  function pickSpan(loN, loMin, loMax, hiN, hiMin, hiMax) {
    if (hiN > loN && hiMin !== null) return [hiMin, hiMax];
    if (loMin !== null) return [loMin, loMax];
    if (hiMin !== null) return [hiMin, hiMax];
    return null;
  }

  function spanFromValues(values) {
    // db.py:_span_aggs, as applied by province_facts / potency_span_of.
    let loN = 0, loMin = null, loMax = null, hiN = 0, hiMin = null, hiMax = null;
    for (const v of values) {
      const n = parseFloat(v);
      if (!isFinite(n) || n <= 0) continue;
      if (n > 100) {
        hiN++; hiMin = hiMin === null ? n : Math.min(hiMin, n);
        hiMax = hiMax === null ? n : Math.max(hiMax, n);
      } else {
        loN++; loMin = loMin === null ? n : Math.min(loMin, n);
        loMax = loMax === null ? n : Math.max(loMax, n);
      }
    }
    return pickSpan(loN, loMin, loMax, hiN, hiMin, hiMax);
  }

  function potencySpanOf(values, category) {
    return potencySpan(spanFromValues(values), category);
  }

  // Python's round() (banker's rounding: ties go to the nearest even digit),
  // vs JS Math.round() which always rounds .5 up. Every *_min/_km/pct figure
  // server.py produces uses the Python one, so a store exactly 6.5 km out or
  // an estimate of exactly 6.5 minutes must round the same way here.
  function pyRound(x, ndigits) {
    const m = Math.pow(10, ndigits || 0);
    const v = x * m;
    const f = Math.floor(v);
    const diff = v - f;
    let r;
    if (diff > 0.5) r = f + 1;
    else if (diff < 0.5) r = f;
    else r = f % 2 === 0 ? f : f + 1;
    return r / m;
  }

  function thumb(url, w) {
    // server.py:thumb
    if (!url) return '';
    return `${url}${url.includes('?') ? '&' : '?'}width=${w}`;
  }

  // --- tier_price: main.py:326-342 --------------------------------------------

  function tierPrice(row) {
    const elite = row.api_elite_price;
    const member = row.member_price || row.api_member_price;
    if (row.is_elite && elite) return ['ELITE', elite];
    if (member) return ['member', member];
    if (elite) return ['ELITE', elite];   // is_elite unknown (older rows)
    return ['-', null];
  }

  // --- live check: scrape.py:_parse_scan (CSV decode) + fetchers/api_fetcher.py
  // (payload shape, response shape, row assembly) + server.py:api_refresh /
  // _run_refresh (scope, job bookkeeping). Called straight from the browser
  // against app.cannacabana.com, which allows CORS for this endpoint (see
  // docs/plans/restore-original-page.md, "Verified"). The sandbox this port
  // was tested in has no network route there; see the parity report for how
  // this path was actually tested (a stub standing in for the real endpoint).

  function parseScan(blob) {
    // scrape.py:_parse_scan -- positional CSV: [0] stock [1] member price
    // [2] retail price [6] gram equivalence.
    const parts = String(blob).split(',').map(p => p.trim());
    const num = i => {
      const n = parseFloat(parts[i]);
      return isFinite(n) ? n : null;
    };
    const stock = num(0);
    return {
      api_raw: String(blob),
      api_stock: stock !== null ? Math.trunc(stock) : null,
      api_member_price: num(1) || null,
      api_price: num(2),
      api_equiv_g: num(6),
    };
  }

  async function scanStore(storeId, sku, variantId, product) {
    // fetchers/api_fetcher.py:ApiFetcher._call + _row, for one SKU.
    const url = `${API_BASE}/product/scan-multiple-items/${storeId}`;
    // api_fetcher._call's retry loop (config.API_MAX_RETRIES). Only a 200
    // carrying a `data` object is an answer; anything else ends as an error,
    // which there becomes an error row that latest_observations ignores, so
    // the store keeps its previous data -- throwing here does the same.
    let body = null, last = '';
    for (let attempt = 1; attempt <= API_MAX_RETRIES; attempt++) {
      let r;
      try {
        r = await originalFetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ skus: [{ [sku]: variantId }] }),
        });
      } catch (e) {
        throw new Error(`network error: ${e && e.message || e}`);
      }
      const parsed = await r.json().catch(() => null);
      if (r.status === 200 && parsed && typeof parsed.data === 'object' && parsed.data !== null) {
        body = parsed; break;
      }
      if (r.status === 429) {
        // ratelimit.classify_429 treats a 429 without X-RateLimit-Remaining as
        // genuine rate limiting. The API doesn't expose that header (or
        // Retry-After) to pages, so this is always that branch: wait
        // 15 s x attempt, as api_fetcher does when Retry-After is absent.
        last = `429 rate limited (waited ${15 * attempt}s)`;
        await new Promise(res => setTimeout(res, 15000 * attempt));
        continue;
      }
      if (r.status >= 500 && r.status < 600) {
        last = `HTTP ${r.status}`;
        await new Promise(res => setTimeout(res, 2000 * attempt));
        continue;
      }
      throw new Error(`HTTP ${r.status}`);
    }
    if (!body) throw new Error(`gave up after ${API_MAX_RETRIES} attempts: ${last}`);
    const data = body.data;
    const items = data['scanned-items'] || {};
    const elite = data.elitePrices || {};
    const missingVariants = new Set((data.missingItems || []).map(String));

    // api_fetcher._row: every row is stamped with the check's time, and THC/CBD
    // come from the catalogue (published as product.thc / product.cbd).
    const row = { api_stock: null, price: null, tier_label: '-', tier_amt: null,
                  available: false, carried: null, stock_text: '',
                  thc: (product && product.thc) || '', cbd: (product && product.cbd) || '',
                  scraped_at: new Date().toISOString().replace(/\.\d{3}Z$/, '+00:00') };
    const blob = items[sku];
    if (blob === undefined || blob === null) {
      row.available = false; row.carried = 0; row.stock_text = 'Not carried';
      return row;
    }
    const parsed = parseScan(blob);
    let ep = null;
    try { ep = parseFloat(elite[sku]) || null; } catch (e) { ep = null; }
    const isElite = ep && !parsed.api_member_price ? 1 : 0;
    const carried = !!(parsed.api_price && parsed.api_price > 0);
    let priceRow = { api_elite_price: ep, member_price: null,
                      api_member_price: parsed.api_member_price, is_elite: isElite };
    row.carried = carried ? 1 : 0;
    if (carried) {
      const qty = parsed.api_stock || 0;
      row.api_stock = qty;
      row.price = parsed.api_price;
      row.available = qty > 0;
      row.stock_text = qty > 0 ? 'In Stock' : 'Sold Out';
      const [label, amt] = tierPrice({ ...priceRow, member_price: parsed.api_member_price });
      row.tier_label = label; row.tier_amt = amt;
    } else {
      row.available = false; row.stock_text = 'Not carried';
    }
    return row;
  }

  const JOBS = new Map();
  let _jobSeq = 0;

  async function runLiveJob(job, storeList, sku, variantId, province, product) {
    const started = Date.now();
    let idx = 0;
    const interval = 60000 / API_RATE_PER_MIN;
    let lastStart = 0;

    async function next() {
      const i = idx++;
      if (i >= storeList.length) return;
      const st = storeList[i];
      const wait = Math.max(0, interval - (Date.now() - lastStart));
      if (wait) await new Promise(res => setTimeout(res, wait));
      lastStart = Date.now();
      try {
        // A failed scan leaves the store's previous data in place, as the
        // original's ignored error row did. Unlike the original, which ended
        // such a job "Done." (api_fetcher.fetch swallowed the error), the
        // failure is reported on the job so the page says "Some stores
        // failed: ..." -- a change the user approved on 2026-09-28.
        let row = null;
        try { row = await scanStore(st.store_id, sku, variantId, product); }
        catch (scanErr) {
          row = null;
          job.error = `${st.name}: ${scanErr && scanErr.message || scanErr}`;
        }
        if (row) {
          (LIVE_OVERRIDES[province] ??= {})[sku] ??= {};
          LIVE_OVERRIDES[province][sku][st.store_id] = row;
        }
      } catch (e) {
        job.error = `${st.name}: ${e && e.message || e}`;
      }
      // db.last_scan_attempt counts every scan row, success or error, as an
      // attempt; each one restarts the skip-list clock for that store.
      try { localStorage.setItem(`scanAttempt:${st.store_id}`, new Date().toISOString()); } catch (e2) {}
      job.done++;
      job.store = job.current = `${st.name}, ${st.city}`;
      if (job.done) job.eta_min = ((Date.now() - started) / job.done)
        * (job.total - job.done) / 60000;
      await next();
    }

    const workers = [];
    for (let w = 0; w < Math.min(API_CONCURRENCY, storeList.length); w++) workers.push(next());
    await Promise.all(workers);
    job.finished = true; job.state = 'done';
  }

  async function apiRefreshPost(params) {
    // server.py:api_refresh (the password gate is dropped: a static page has
    // no session to gate on).
    const sku = params.get('sku');
    const catalog = (await loadCatalog()).products;
    const v = catalog.find(p => String(p.sku) === String(sku));
    if (!v) return { __status: 404, error: `unknown sku ${sku}` };

    const fetcher = params.get('fetcher') || 'api';
    if (fetcher !== 'api') {
      return { __status: 400, error: 'only the "api" fetcher runs from a static page' };
    }
    if (!v.variant_id) {
      return { __status: 400, error: 'no variant id for this SKU in the published catalogue' };
    }

    const top = params.get('top') ? parseInt(params.get('top'), 10) : DEFAULT_TOP;
    const province = params.get('province') || DEFAULT_PROVINCE;
    const allStores = (params.get('all_stores') || 'false') === 'true';
    const [storeList] = await scope({
      lat: params.get('lat'), lng: params.get('lng'), near: params.get('near'),
      top, province, allStores,
    });

    // fetchers.partition_scannable: a store on the skip list is contacted only
    // if its last scan attempt is more than SCAN_SKIP_RETRY_DAYS old, or it has
    // never been tried. The original remembered attempts in the shared history
    // DB; a static page can only remember them in this visitor's browser.
    const skipIds = new Set(((await loadCatalog()).scan_skip_stores || []).map(String));
    const keep = [], skipped = [];
    for (const s of storeList) {
      if (!skipIds.has(String(s.store_id))) { keep.push(s); continue; }
      let stamp = null;
      try { stamp = localStorage.getItem(`scanAttempt:${s.store_id}`); } catch (e) { stamp = null; }
      const due = !stamp || (Date.now() - Date.parse(stamp)) >= SCAN_SKIP_RETRY_DAYS * 86400000;
      (due ? keep : skipped).push(s);
    }
    if (!keep.length) {
      return { __status: 409, error: 'every store in scope is on the scan skip list' };
    }
    storeList.length = 0; storeList.push(...keep);

    const id = `live-${++_jobSeq}`;
    const job = { id, total: storeList.length, done: 0, state: 'running',
                  current: null, store: null, eta_min: null, finished: false,
                  error: null };
    JOBS.set(id, job);
    runLiveJob(job, storeList, sku, v.variant_id, province, v);
    return { job: id, total: storeList.length, skipped: skipped.map(s => s.name) };
  }

  function apiJobGet(id) {
    const job = JOBS.get(id);
    if (!job) return { __status: 404, error: 'no such job' };
    return job;
  }

  // --- province facts: db.py:province_facts ---------------------------------
  const _factsCache = new Map();

  function stockRow(arr, times) {
    // ci/export_province.py's appended stock row: the original 7 fields plus
    // `available`/`stock_text`. A province file exported before that change
    // is 7 fields long and available-only, so a missing `available` means
    // available=1 (per the plan).
    return {
      store_id: String(arr[0]), api_stock: arr[1], price: arr[2],
      tier_label: arr[3], tier_amt: arr[4], thc: arr[5], cbd: arr[6],
      available: arr.length > 7 ? !!arr[7] : true,
      stock_text: arr.length > 8 ? arr[8] : null,
      // The row's own scrape time and `carried`, exported from 2026-09-28;
      // older files fall back to the store's time and null.
      scraped_at: arr.length > 9 && times ? (times[arr[9]] || null) : null,
      carried: arr.length > 10 ? arr[10] : null,
    };
  }

  async function provinceFacts(province) {
    const hit = _factsCache.get(province);
    if (hit) return hit;
    let data;
    try { data = await loadProvince(province); } catch (e) { return {}; }
    const facts = {};
    const storeNames = new Map((data.stores || []).map(st => [String(st.id), st.name]));
    for (const [sku, rawRows] of Object.entries(data.stock || {})) {
      const rows = rawRows.map(a => stockRow(a, data.times));
      const available = rows.some(r => r.available);
      const category = (data.products[sku] && data.products[sku].category) || '';
      const thc = spanFromValues(rows.map(r => r.thc));
      const cbd = spanFromValues(rows.map(r => r.cbd));
      // Added on the Pages site (approved 2026-09-28): how many stores in
      // the province have it, for the search cards. Not part of the
      // original server's answer.
      // Changed 2026-09-28, at the user's request: the card used to show the
      // province's lowest shelf, member and Elite prices separately, and the
      // member figure came from a far store that the product page (nearest
      // first) did not lead with, so it never matched a local listing. Now
      // ONE number: the lowest price actually paid at any in-stock store,
      // computed as the product page's bold price (tier_amt, else shelf
      // price), with the store's name and which tier it is. Ties go to the
      // store that comes first in the data.
      const inStock = rows.filter(r => r.available);
      let best = null;
      for (const r of inStock) {
        const paid = r.tier_amt ?? r.price;
        if (paid === null || paid === undefined) continue;
        if (best === null || paid < best.paid) best = { paid, row: r };
      }
      facts[sku] = { available, category, thc, cbd,
                     stores: inStock.length,
                     lowest: best ? best.paid : null,
                     lowest_store: best ? (storeNames.get(best.row.store_id) ?? null) : null,
                     lowest_tier: best ? best.row.tier_label : null };
    }
    _factsCache.set(province, facts);
    return facts;
  }

  // --- geolocation: stores.py:distance_km/nearest/geocode/resolve_location --

  function distanceKm(lat1, lng1, lat2, lng2) {
    const r = 6371.0, toRad = x => (x * Math.PI) / 180;
    const p1 = toRad(lat1), p2 = toRad(lat2);
    const dp = toRad(lat2 - lat1), dl = toRad(lng2 - lng1);
    const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    return 2 * r * Math.asin(Math.sqrt(a));
  }

  function nearest(storeList, lat, lng, n) {
    const out = [];
    for (const s of storeList) {
      if (s.latitude === null || s.latitude === undefined
          || s.longitude === null || s.longitude === undefined) continue;
      out.push({ ...s, distance_km: pyRound(distanceKm(lat, lng, s.latitude, s.longitude), 1) });
    }
    out.sort((a, b) => a.distance_km - b.distance_km);
    return n ? out.slice(0, n) : out;
  }

  async function geocode(place) {
    place = (place || '').trim();
    if (!place) return null;
    const m = place.match(/^\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*$/);
    if (m) return [parseFloat(m[1]), parseFloat(m[2])];

    const cache = await loadGeocodeCache();
    const key = place.toLowerCase();
    if (cache[key]) return cache[key];

    // Same service the server used (stores.py:geocode), called from the
    // browser now instead of from the host machine. The sandbox this port was
    // tested in has no network route to Nominatim; a real deploy does.
    try {
      const q = new URLSearchParams({ q: place, format: 'json', limit: '1', countrycodes: 'ca' });
      const r = await originalFetch(`https://nominatim.openstreetmap.org/search?${q}`);
      const hits = await r.json();
      if (!hits || !hits.length) return null;
      return [parseFloat(hits[0].lat), parseFloat(hits[0].lon)];
    } catch (e) {
      return null;
    }
  }

  async function resolveLocation(place) {
    if (place) return geocode(place);
    if (HOME) return geocode(HOME);
    return null;
  }

  // --- scope: server.py:_scope -----------------------------------------------

  async function scope({ lat, lng, near, top, province, allStores }) {
    const prov = province || DEFAULT_PROVINCE;
    let provinceData;
    try { provinceData = await loadProvince(prov); } catch (e) { provinceData = { stores: [] }; }
    const storeList = (provinceData.stores || []).map(s => ({
      store_id: String(s.id), name: s.name, city: s.city,
      latitude: s.lat, longitude: s.lng, scraped_at: s.scraped_at,
    }));

    let loc, where;
    if (lat !== null && lat !== undefined && lng !== null && lng !== undefined) {
      loc = [parseFloat(lat), parseFloat(lng)]; where = 'your location';
    } else {
      loc = near ? await resolveLocation(near) : await resolveLocation(null);
      where = near || HOME;
    }

    if (!loc) {
      if (allStores) return [storeList, `all ${storeList.length} stores in ${prov}`];
      return [storeList.slice(0, top || DEFAULT_TOP), `${prov} (unlocated)`];
    }

    const limit = allStores ? null : (top || DEFAULT_TOP);
    const found = nearest(storeList, loc[0], loc[1], limit);
    const label = allStores
      ? `all ${found.length} stores in ${prov}, nearest first`
      : `${found.length} nearest`;
    return [found, `${label} to ${where}`];
  }

  // --- fill_missing_stores: main.py:280-323 -----------------------------------

  function fillMissingStores(rows, storeList, sku, indexedIds) {
    const have = new Set(rows.map(r => r.store_id));
    const out = rows.slice();
    for (const s of storeList) {
      if (have.has(s.store_id)) continue;
      const known = indexedIds.has(s.store_id);
      out.push({
        store_id: s.store_id, api_stock: known ? 0 : null, price: null,
        tier_label: '-', tier_amt: null, thc: null, cbd: null,
        available: false, carried: known ? 0 : null, filled: true,
        stock_text: known ? 'Not in stock' : 'Not checked',
      });
    }
    return out;
  }

  // --- pack: server.py:_pack --------------------------------------------------

  function pack(rows, storeList, sort, handle) {
    const byId = new Map(storeList.map(s => [s.store_id, s]));
    const out = rows.map(r => {
      const st = byId.get(r.store_id) || {};
      const market = r.price;
      const deal = r.tier_amt;
      let save = null;
      if (market && deal && deal < market) {
        save = { amount: pyRound(market - deal, 2),
                 pct: pyRound((100 * (market - deal)) / market) };
      }
      return {
        store: st.name, city: st.city, store_id: r.store_id,
        distance_km: st.distance_km !== undefined ? st.distance_km : null,
        qty: r.api_stock === undefined ? null : r.api_stock,
        available: !!r.available, carried: r.carried === undefined ? null : r.carried,
        stock_text: r.stock_text,
        market: market === undefined ? null : market,
        tier: r.tier_label, tier_price: deal === undefined ? null : deal, save,
        is_elite: r.tier_label === 'ELITE',
        // The observations view's url: config.BASE/products/<handle>?sID=<store>.
        // Filled-in stores have no url: main.fill_missing_stores sets none.
        url: r.filled ? null : `https://cannacabana.com/products/${handle || ''}?sID=${r.store_id}`,
      };
    });
    const far = 9e9;
    if (sort === 'stock') {
      out.sort((a, b) => {
        const ka = [a.available ? 0 : 1, -(a.qty || 0), a.distance_km ?? far];
        const kb = [b.available ? 0 : 1, -(b.qty || 0), b.distance_km ?? far];
        for (let i = 0; i < 3; i++) if (ka[i] !== kb[i]) return ka[i] - kb[i];
        return 0;
      });
    } else {
      out.sort((a, b) => {
        const ka = [a.available ? 0 : 1, a.distance_km ?? far, -(a.qty || 0)];
        const kb = [b.available ? 0 : 1, b.distance_km ?? far, -(b.qty || 0)];
        for (let i = 0; i < 3; i++) if (ka[i] !== kb[i]) return ka[i] - kb[i];
        return 0;
      });
    }
    return out;
  }

  // --- catalog.search: catalog.py:183-223 -------------------------------------

  function catalogSearch(terms, catalog) {
    const q = terms.toLowerCase().trim();
    const words = q.split(/\s+/).filter(Boolean);
    if (!words.length) return [];
    const scored = [];
    for (const v of catalog) {
      const title = (v.title || '').toLowerCase();
      const hay = `${title} ${v.brand || ''} ${v.category || ''} ${v.size || ''}`.toLowerCase();
      if (!words.every(w => hay.includes(w))) continue;
      let rank;
      if (title === q) rank = 0;
      else if (title.startsWith(q)) rank = 1;
      else if (title.includes(q)) rank = 2;
      else if (words.every(w => title.includes(w))) rank = 3;
      else rank = 4;
      scored.push([rank, title.length, v.brand || '', title, v]);
    }
    scored.sort((a, b) => {
      for (let i = 0; i < 4; i++) {
        if (a[i] < b[i]) return -1;
        if (a[i] > b[i]) return 1;
      }
      return 0;
    });
    return scored.map(t => t[4]);
  }

  // --- routes ------------------------------------------------------------------

  async function apiProvinces() {
    // server.py:api_provinces
    const all = await loadStores();
    const counts = {};
    for (const s of all) if (s.province) counts[s.province] = (counts[s.province] || 0) + 1;
    const provinces = Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .map(([name, stores]) => ({ name, stores }));
    return { provinces, default: DEFAULT_PROVINCE, home: HOME };
  }

  async function apiSearch(params) {
    // server.py:api_search
    const q = params.get('q') || '';
    const limit = parseInt(params.get('limit') || '50', 10);
    const offset = parseInt(params.get('offset') || '0', 10);
    const province = params.get('province');
    const stockedOnly = (params.get('stocked_only') ?? 'true') !== 'false';
    const category = params.get('category');

    const catalog = (await loadCatalog()).products;
    // Added on the Pages site (approved 2026-09-28): an empty search box
    // browses the whole catalogue, A to Z, through the same province and
    // category filters. The original server answered an empty search with
    // nothing.
    let hits = q.trim() ? catalogSearch(q, catalog)
      : [...catalog].sort((a, b) => (a.title || '').localeCompare(b.title || ''));

    const seen = new Set(), uniq = [];
    for (const v of hits) {
      if (seen.has(v.sku)) continue;
      seen.add(v.sku); uniq.push(v);
    }

    const prov = province || DEFAULT_PROVINCE;
    const facts = await provinceFacts(prov);
    const canFilter = Object.keys(facts).length > 0;
    let filtered = uniq, hidden = 0;

    if (category) {
      const want = category.trim().toLowerCase();
      filtered = filtered.filter(v =>
        ((facts[v.sku] && facts[v.sku].category) || v.category || '').toLowerCase() === want);
    }

    if (stockedOnly && canFilter) {
      const before = filtered.length;
      filtered = filtered.filter(v => facts[v.sku] && facts[v.sku].available);
      hidden = before - filtered.length;
    }

    // Added on the Pages site (user's request, 2026-09-28): order the whole
    // filtered list by `lowest` (the lowest price paid at an in-stock store,
    // the number the card shows) before paging. No price goes last either
    // way; ties keep the earlier order (Array.sort is stable). No `sort`
    // param leaves the original order untouched.
    const sort = params.get('sort');
    if (sort === 'price_asc' || sort === 'price_desc') {
      const dir = sort === 'price_asc' ? 1 : -1;
      const pf = v => { const f = facts[v.sku]; return f ? f.lowest : null; };
      filtered = [...filtered].sort((a, b) => {
        const x = pf(a), y = pf(b);
        const nx = x === null || x === undefined, ny = y === null || y === undefined;
        if (nx || ny) return nx === ny ? 0 : (nx ? 1 : -1);
        return (x - y) * dir;
      });
    }

    const page = filtered.slice(offset, offset + limit);
    return {
      total: filtered.length, offset, hidden,
      filtered: !!(stockedOnly && canFilter), can_filter: canFilter, province: prov,
      products: page.map(v => {
        const f = facts[v.sku] || {};
        return {
          sku: v.sku, title: v.title, brand: v.brand, size: v.size,
          category: f.category || v.category,
          in_stock: !!f.available,
          stores: f.stores || 0,
          lowest: f.lowest === undefined ? null : f.lowest,
          lowest_store: f.lowest_store === undefined ? null : f.lowest_store,
          lowest_tier: f.lowest_tier === undefined ? null : f.lowest_tier,
          thc: potencySpan(f.thc, f.category || ''),
          cbd: potencySpan(f.cbd, f.category || ''),
          image: thumb(v.image, 160),
        };
      }),
    };
  }

  async function apiCategories(params) {
    // server.py:api_categories
    const province = params.get('province');
    const stockedOnly = (params.get('stocked_only') ?? 'true') !== 'false';
    const facts = await provinceFacts(province || DEFAULT_PROVINCE);
    const counts = {};
    for (const f of Object.values(facts)) {
      if (stockedOnly && !f.available) continue;
      const cat = (f.category || '').trim();
      if (cat) counts[cat] = (counts[cat] || 0) + 1;
    }
    const categories = Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .map(([name, skus]) => ({ name, skus }));
    return { categories };
  }

  async function apiResults(params) {
    // server.py:api_results
    const sku = params.get('sku');
    const catalog = (await loadCatalog()).products;
    const v = catalog.find(p => String(p.sku) === String(sku));
    if (!v) return { __status: 404, error: `unknown sku ${sku}` };

    const lat = params.get('lat'), lng = params.get('lng');
    const near = params.get('near');
    const top = params.get('top') ? parseInt(params.get('top'), 10) : DEFAULT_TOP;
    const province = params.get('province');
    const allStores = (params.get('all_stores') || 'false') === 'true';
    const sort = params.get('sort') || 'distance';

    const [storeList, scopeLabel] = await scope({ lat, lng, near, top, province, allStores });
    const storeIds = new Set(storeList.map(s => s.store_id));
    const prov = province || DEFAULT_PROVINCE;

    let provinceData;
    try { provinceData = await loadProvince(prov); } catch (e) { provinceData = { stock: {}, products: {} }; }
    let rawAll = (provinceData.stock[sku] || []).map(a => stockRow(a, provinceData.times));
    // A live check just run in this browser (see apiRefreshPost) replaces
    // that store's row for this visitor only -- the original wrote it to the
    // shared history DB; a static page has nowhere shared to write it.
    const overrides = (LIVE_OVERRIDES[prov] || {})[sku];
    if (overrides) {
      const have = new Set(rawAll.map(r => r.store_id));
      for (const [storeId, o] of Object.entries(overrides)) {
        const row = { store_id: storeId, ...o };
        const i = rawAll.findIndex(r => r.store_id === storeId);
        if (i === -1) rawAll.push(row); else rawAll[i] = row;
        have.add(storeId);
      }
    }
    const raw = rawAll.filter(r => storeIds.has(r.store_id));

    // cache_age_hours(rows): db.py:664 -- newest scraped_at among the actual
    // (non-synthetic) rows. Approximated from the STORE's scraped_at, since
    // the export does not carry a per-(sku,store) timestamp -- see the file
    // header for why.
    const storeByIdAll = new Map(storeList.map(s => [s.store_id, s]));
    let age = null;
    for (const r of raw) {
      const st = storeByIdAll.get(r.store_id);
      const t = r.scraped_at || (st && st.scraped_at);
      if (t) {
        const h = (Date.now() - Date.parse(t)) / 3600000;
        if (age === null || h < age) age = h;
      }
    }

    // index_coverage(store_ids): db.py:405 -- how many of the scope's stores
    // have ever been indexed (approximated as "has a scraped_at"), and how
    // old the newest of those is.
    let indexed = 0, newestIndexed = null;
    for (const s of storeList) {
      if (s.scraped_at) {
        indexed++;
        if (!newestIndexed || s.scraped_at > newestIndexed) newestIndexed = s.scraped_at;
      }
    }
    const indexAge = newestIndexed ? (Date.now() - Date.parse(newestIndexed)) / 3600000 : null;

    const indexedIds = new Set(storeList.filter(s => s.scraped_at).map(s => s.store_id));
    const filled = fillMissingStores(raw, storeList, sku, indexedIds);
    const packed = pack(filled, storeList, sort,
      (provinceData.products[sku] && provinceData.products[sku].handle) || v.handle);

    const category = (provinceData.products[sku] && provinceData.products[sku].category) || v.category || '';
    return {
      product: {
        sku, title: v.title, brand: v.brand, size: v.size,
        image: thumb(v.image, 320), category,
        thc: potencySpanOf(filled.map(r => r.thc), category),
        cbd: potencySpanOf(filled.map(r => r.cbd), category),
      },
      scope: scopeLabel, checked: storeList.length,
      in_stock: packed.filter(r => r.available).length,
      age_hours: age, indexed_stores: indexed, index_age_hours: indexAge,
      results: packed,
    };
  }

  // Also published on window before the page's own script runs, so code that
  // draws before /api/capabilities answers (the index panel, the catalogue
  // line) never mistakes "not loaded yet" for "allowed".
  const STATIC_CAPS = {
    admin: false, playwright: false, live: true, rebuild: false,
    catalog_refresh: false, live_seconds_per_store: 2.2,
  };
  window.STATIC_CAPS = STATIC_CAPS;

  async function apiCapabilities() {
    // app.cannacabana.com's scan endpoint allows CORS (verified live; see
    // docs/plans/restore-original-page.md), so "Live: fast API" runs straight
    // from the browser and stays offered. "Live: real browser" still cannot
    // run here (no Playwright on a static page), and admin-gated actions
    // (catalogue refresh, index rebuilds) stay off pending a decision on
    // whether to offer them at all -- see the plan's "Proposed improvements".
    return STATIC_CAPS;
  }

  async function apiCatalogStatus() {
    // server.py:api_catalog_status
    let cat;
    try { cat = await loadCatalog(); } catch (e) {
      return { age_hours: null, stale: true, max_age_hours: CATALOG_MAX_AGE_H,
               variants: 0, ok: false, job: null, last_error: null };
    }
    const ageHours = cat.generated_at
      ? (Date.now() - Date.parse(cat.generated_at)) / 3600000 : null;
    return {
      age_hours: ageHours === null ? null : pyRound(ageHours, 1),
      stale: ageHours === null || ageHours >= CATALOG_MAX_AGE_H,
      max_age_hours: CATALOG_MAX_AGE_H,
      variants: (cat.products || []).length,
      ok: true, job: null, last_error: null,
    };
  }

  async function apiBuildStatus() {
    return { frozen: false, stale: false };
  }

  async function apiIndexStatus() {
    // server.py:api_index_status
    const all = await loadStores();
    const byProv = {};
    for (const s of all) if (s.province) (byProv[s.province] ||= []).push(s);

    const entries = Object.entries(byProv).sort((a, b) => b[1].length - a[1].length);
    const provinces = [];
    for (const [prov, sts] of entries) {
      let data = null;
      try { data = await loadProvince(prov); } catch (e) { /* not scraped yet */ }
      const stores = (data && data.stores) || [];
      let indexed = 0, newest = null;
      for (const s of stores) {
        if (s.scraped_at) {
          indexed++;
          if (!newest || s.scraped_at > newest) newest = s.scraped_at;
        }
      }
      const ageHours = newest ? (Date.now() - Date.parse(newest)) / 3600000 : null;
      const run = data && data.run;
      const lastRunStores = run
        ? (run.failed !== undefined && run.failed !== null ? (run.stores || 0) - run.failed : run.stores || 0)
        : 0;
      provinces.push({
        province: prov, stores: sts.length, indexed, age_hours: ageHours,
        last_run: run ? run.run_id : null,
        last_run_stores: lastRunStores,
        incomplete: !!(run && lastRunStores < sts.length),
        estimate_min: pyRound((sts.length * 25 * 60) / 50 / 60),
        job: null,
      });
    }
    return { provinces, busy: false, active: [], running: null, rate: {}, egress: [] };
  }

  // Added on the Pages site (approved 2026-09-28): when the province's data
  // was published and which stores failed on its last run, for the line
  // under the heading. The original server has no such route; on it the
  // page simply shows nothing there.
  async function apiProvinceSummary(params) {
    const prov = params.get('province') || DEFAULT_PROVINCE;
    let data;
    try { data = await loadProvince(prov); }
    catch (e) { return { __status: 404, error: `No data published for ${prov} yet.` }; }
    const run = data.run || {};
    return {
      province: prov, generated_at: data.generated_at || null,
      age_hours: data.generated_at ? (Date.now() - Date.parse(data.generated_at)) / 3600000 : null,
      failed: run.failed || 0, failed_stores: run.failed_stores || [],
    };
  }

  function notAvailable() {
    return { __status: 404, error: 'Not available on the static site.' };
  }

  // --- dispatch ------------------------------------------------------------

  // Path segment after the LAST '/api/' in the URL, whatever the URL's host
  // or base path (the page's own '/api/...' fetches do not point at this
  // site's own directory once Pages serves it under a subpath -- see the
  // file header). A leading path like 'index/Alberta/cancel' is kept whole so
  // the blanket 404 rules below can match by prefix.
  function apiRoute(url) {
    const i = url.pathname.lastIndexOf('/api/');
    if (i === -1) return null;
    return url.pathname.slice(i + 5).replace(/\/+$/, '');
  }

  async function handle(url, method, params) {
    const route = apiRoute(url);
    if (route === null) return null;

    // GET /api/results?...&fetcher=... POSTs to /api/refresh, then polls
    // GET /api/job/{id}: both run for real (a client-side live check against
    // app.cannacabana.com's CORS-enabled scan endpoint -- see apiRefreshPost).
    // Everything else server.py could not answer without a running server
    // (auth, index rebuilds, the admin-gated catalogue refresh, the packaged
    // app's self-rebuild) is not available here.
    if (method === 'POST' && route === 'refresh') return apiRefreshPost(params);
    if (method === 'GET' && route.startsWith('job/')) return apiJobGet(route.slice(4));

    const blocked = method !== 'GET'
      || route === 'jobs' || route === 'login' || route === 'logout'
      || (route.startsWith('index/') && route !== 'index/status')
      || route === 'catalog/refresh' || route === 'build/rebuild';
    if (blocked) return notAvailable();

    switch (route) {
      case 'provinces': return apiProvinces();
      case 'search': return apiSearch(params);
      case 'categories': return apiCategories(params);
      case 'results': return apiResults(params);
      case 'capabilities': return apiCapabilities();
      case 'catalog/status': return apiCatalogStatus();
      case 'build/status': return apiBuildStatus();
      case 'index/status': return apiIndexStatus();
      case 'province/summary': return apiProvinceSummary(params);
      default: return notAvailable();
    }
  }

  window.fetch = async function (input, init) {
    const req = input instanceof Request ? input : null;
    const rawUrl = req ? req.url : String(input);
    const method = (init && init.method) || (req && req.method) || 'GET';

    let url;
    try { url = new URL(rawUrl, document.baseURI); } catch (e) { return originalFetch(input, init); }

    if (!url.pathname.includes('/api/')) return originalFetch(input, init);

    let result;
    try {
      result = await handle(url, method.toUpperCase(), url.searchParams);
    } catch (e) {
      return new Response(JSON.stringify({ error: String(e && e.message || e) }),
        { status: 500, headers: { 'Content-Type': 'application/json' } });
    }
    if (result === null) return originalFetch(input, init);

    const status = result.__status || 200;
    if (result.__status) delete result.__status;
    return new Response(JSON.stringify(result),
      { status, headers: { 'Content-Type': 'application/json' } });
  };
})();
