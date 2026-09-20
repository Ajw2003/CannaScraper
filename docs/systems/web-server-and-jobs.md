# Web server, jobs & auth

## What it owns

The 18 HTTP routes serving the desktop app's UI and API
([server.py:262-838](../../CannaScraper/server.py)); `jobs.py`'s serialized background worker
that everything scrape-triggering runs through; `auth.py`'s password gate on writes; and
`web/index.html`, the single-file frontend that drives it all.

## How it works

**Reads are open, writes are gated.** `/`, `/api/provinces`, `/api/search`, `/api/categories`,
`/api/results`, `/api/job/{id}`, `/api/jobs`, `/api/index/status`, `/api/catalog/status`,
`/api/build/status`, and `/api/capabilities` need no auth. Anything that spends the shared
rate-limit budget or restarts the process — `/api/refresh` (server.py:425),
`/api/catalog/refresh` (server.py:667), `/api/build/rebuild` (server.py:724),
`/api/index/{province}` (server.py:763), `/api/index/{province}/cancel` (server.py:789) — is
gated with `Depends(auth.require_admin)`.

**`/api/results`** (server.py:369) is the instant-answer path: reads
`db.latest_observations()`, fills gaps with `main.fill_missing_stores()`
([main.py:280](../../CannaScraper/main.py)) so "checked and empty" reads differently from "never
checked," and computes THC/CBD as a span over the stores actually shown (`potency_span_of`) rather
than the first non-empty value — see
[Decisions.md](../Decisions.md#2026-08-25--thccbd-potency-shown-as-a-consensus-range-not-max).

**`/api/refresh`** (server.py:425) queues an async job via `jobs.submit_live()`, which fans stores
out concurrently with `asyncio.gather` (server.py:536) — the fetcher's own semaphore is the real
concurrency bound, not this call site. **All** site-contacting work funnels through `jobs.py`'s
single serialized worker thread (`_drain`, jobs.py:321): index and live-check jobs never run
concurrently, because they share one rate-limit budget (jobs.py:1-15) — see
[rate-limiting-and-egress.md](rate-limiting-and-egress.md).

**`auth.py`** hashes the admin password with scrypt (auth.py:71) and signs session tokens with
HMAC bound to the password hash, so changing the password invalidates every existing session
(auth.py:123-128) with no separate revocation list needed. It explicitly does not trust loopback
as "the owner" (auth.py:8-11) — the bundled Cloudflare tunnel always connects from `127.0.0.1`, so
treating `127.0.0.1` as trusted would hand the admin surface to the entire public internet.

**`web/index.html`** (851 lines, no build step, no framework) is served at `/` and is a thin
client over the routes above: search, per-SKU results with distance/price/stock, the admin login
modal, the index/build status panel with progress polling, and the stale-build banner (see
[desktop-packaging.md](desktop-packaging.md)).

## Invariants

- The `_FACTS` cache (server.py:71-72, 120s TTL) must be invalidated (`invalidate_facts`,
  server.py:89) whenever a job writes rows, or a finished index build stays invisible on the
  results page for up to 2 minutes (server.py:92-93).
- `_brief()` / job status output (server.py:578) never includes egress route credentials —
  `Egress.describe()` strips them before anything is printed or served (server.py:585-586,
  egress.py:177-186). The settings file itself remains as sensitive as what's in it.
- `/api/build/rebuild` cannot rebuild in-process — PyInstaller has to delete this same process's
  own `.pyd` files while it's running — so it launches a **detached** `rebuild.ps1` and sets
  `_rebuild_requested`, which `app.py`'s main loop polls to actually exit (server.py:703-707).

## Traps

- **server.py:696-701**: build-staleness detection (see
  [desktop-packaging.md](desktop-packaging.md)) exists specifically because "an exe built before a
  fix landed serves the old code with no visible sign of it" is exactly how the search-returns-nothing
  bug stayed live for days — see
  [docs/plans/fix-search-and-build-staleness.md](../plans/fix-search-and-build-staleness.md).
- **server.py:401-411** documents a past bug: headline potency used to be "the first non-empty
  value across all rows," which depended on row sort order and could show a figure no nearby store
  actually had. Now computed as a proper consensus span over the visible scope.
- **auth.py:148-153**: per-IP login throttling is meaningless behind the tunnel, since every
  request arrives from `cloudflared`'s own source address — `client_key()` trusts
  `CF-Connecting-IP` / `X-Forwarded-For` first, falling back to a single global counter only when
  neither header is present.
- **`site/app.js`'s "top N" dropdown and "📍 Near me" button don't re-render an already-open
  result table** — `showResults()` never marks the active product, so the change handlers that
  look for it silently no-op. This is the *static Pages mirror*'s frontend, not `web/index.html`;
  see [pages-deployment.md](pages-deployment.md) and
  [ProjectState.md](../ProjectState.md#cross-cutting-issues-that-belong-to-no-milestone).
