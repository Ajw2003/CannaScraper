# Ship CannaScraper as a distributable, publicly-servable app

> Implementation plan. Branch `ApiFork`. Written 2026-08-23.
> Working copy of the approved plan, kept in the repo so it outlives the chat session.

## Context

Today the web UI only exists as a command: `cd CannaScraper && .venv\Scripts\python server.py`.
That requires a Python 3.13 venv, a hand-assembled dependency list (there is no
`requirements.txt` — the install instructions in the README predate FastAPI being added), and
a working directory of exactly `CannaScraper\` because every data path in `config.py` is
CWD-relative. It binds `0.0.0.0:8000`, so it reaches phones on the same wifi and nothing else.

Three gaps to close:

1. **Distributable** — a `.exe` that anyone can run with no Python and no setup.
2. **Refresh per province from the UI** — the 45-minute province index (`index_builder.py`) is
   CLI-only today. `server.py` can only do per-SKU live re-checks. The UI even tells you to
   "run index_builder" (`web/index.html:303`).
3. **Public, not just LAN** — served to anyone with a link, still hosted on whatever PC is
   running the app.

## Decisions taken

| Question | Decision |
|---|---|
| Public URL | Cloudflare tunnel — quick tunnel by default, named tunnel when a token is configured |
| Auth | Open browsing; password on anything that causes outbound scraping |
| Playwright | Dropped from the packaged build; stays working in the source checkout |
| Shipped data | `catalog.json` + `stores.json` bundled; index builds on first run |

## Before starting

`catalog.json` is modified and uncommitted on branch `ApiFork`. Commit or stash it before
touching anything — this plan rewrites `config.py`, `index_builder.py`, `server.py`, and
`web/index.html`.

```
git add -A && git commit -m "wip: catalog refresh before packaging work"
```

---

## 1. Path resolution — new `paths.py`, rewire `config.py`

The single change that makes everything else possible. Every module already reads
`config.DB_PATH` / `config.CATALOG_CACHE` / etc., so making those values **absolute** fixes all
call sites at once without touching any of them.

New `CannaScraper/paths.py`:

- `APP_DIR` — where read-only bundled seeds live. `sys._MEIPASS` when frozen, else the source dir.
- `DATA_DIR` — writable state. `%LOCALAPPDATA%\CannaCabana\` when frozen, else the source dir
  (so a source checkout behaves exactly as it does today).
- `seed(name)` — copy a bundled seed into `DATA_DIR` on first run if absent.

`config.py:80-87` and `:26` change from bare filenames to `str(paths.DATA_DIR / "...")`.
`RAW_DIR` too. `paths.py` imports nothing from the project, so there is no import cycle.

`server.py:33` `HERE / "web" / "index.html"` becomes `paths.APP_DIR / "web" / "index.html"`.

## 2. Serialized job worker — new `jobs.py`

`server.py:34`'s `JOBS` dict is in-memory, unbounded, never pruned, and its jobs run on the
uvicorn event loop. A 45-minute synchronous index build cannot go there.

`jobs.py` provides one **worker thread** consuming one **queue**:

- Both job kinds — `index` (a province) and `live` (a per-SKU re-check) — go through it.
  They must be serialized: `config.API_RATE_PER_MIN = 50` is one global budget against one
  upstream host, and two concurrent jobs would produce 429s, not speed.
- The live path is async; the worker calls `asyncio.run(...)` for it inside its own thread.
  That also gets the long index build off the event loop, so search stays instant during one.
- Job record: `id, kind, province|sku, run_id, total, done, current, started, finished,
  cancelled, error, rows`. History bounded to the last 50 finished jobs.
- `cancel(job_id)` sets a flag the builder checks between stores.
- Queue admission: at most one queued-or-running job per province, so a public link cannot
  stack ten Ontario builds.
- On an index job finishing, clear `server._FACTS[province]` (`server.py:53`) so the new data
  shows immediately instead of after the 120 s TTL.

## 3. `index_builder.py` — extract a callable core

Split `main()` (`index_builder.py:192`) into:

```python
def build_index(province, *, limit=None, resume=None, db_path=None,
                on_progress=None, should_stop=None) -> dict
```

`on_progress(done, total, store_name, eta_min)` replaces the `print()` calls at lines 238-245;
`should_stop()` is checked at the top of each store iteration. `main()` becomes a thin CLI
wrapper passing a printing `on_progress`, so `python index_builder.py --province Ontario` and
`--resume` behave exactly as the README documents.

`index_store()`, `_close_out()`, `_Pacer`, and the `index-{Province}-{timestamp}` run_id format
are unchanged — that `index-%` prefix is load-bearing in `db.index_coverage` (`db.py:232`) and
`db.indexed_store_ids` (`db.py:266`).

Cancel granularity is one store (~29 s), because a store's paged fetch is not interruptible
mid-flight. The UI should say "stopping after this store" rather than pretend it is instant.

## 4. Auth — new `auth.py`

Open reads, password on writes.

- Settings at `DATA_DIR/settings.json`: `password_hash`, `password_salt`, `session_secret`,
  `tunnel_token`, `port`, `open_browser`.
- First run generates a random password and **prints it in the console**, then stores only a
  `hashlib.scrypt` hash. Changeable by editing settings or a `--set-password` flag.
- `POST /api/login` → HMAC-signed expiring token in an `HttpOnly; SameSite=Lax` cookie
  (`Secure` when reached over the tunnel). `hmac.compare_digest` for the check.
- `require_admin` dependency on `/api/refresh` and all `/api/index/*`. Everything else stays open.
- Simple in-memory backoff on failed logins — this endpoint is on the public internet.

All stdlib (`hashlib`, `hmac`, `secrets`) — no new dependency.

## 5. Server routes — `server.py`

Existing routes keep their shapes. Added:

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/index/status` | open | Per province: store count, indexed count, index age, running/queued job, last run_id and whether it completed |
| `POST /api/index/{province}` | admin | Enqueue a build; `?resume=<run_id>` or `?limit=N` |
| `POST /api/index/{province}/cancel` | admin | Set the cancel flag |
| `GET /api/jobs` | open | All active + recent jobs |
| `POST /api/login` | — | Set the session cookie |
| `GET /api/capabilities` | open | `{playwright: bool}` so the UI hides "Live: real browser" in packaged builds |

`GET /api/job/{job_id}` (`server.py:365`) stays, now backed by `jobs.py`, so the existing
refresh-polling JS keeps working unchanged.

`/api/index/status` builds from existing helpers: `S.get_stores(province=...)` for the store
list and `db.index_coverage(conn, ids)` for count + age. A small new
`db.index_runs(conn, province)` returns recent `index-{Province}-%` run_ids with their done-store
counts, to drive the Resume affordance (reuses `db.done_store_ids`, `db.py:117`).

## 6. Tunnel — new `tunnel.py`

Bundles `cloudflared.exe` alongside the app.

- Token in settings → `cloudflared tunnel run --token <token>` (your stable hostname).
- Otherwise → `cloudflared tunnel --url http://127.0.0.1:<port> --no-autoupdate`, scraping the
  `https://*.trycloudflare.com` URL out of its output.
- **Its output is relayed line-by-line into the app's own console**, not into a log file — you
  watch the tunnel connect, and you see it if it doesn't.
- Terminated cleanly on Ctrl-C.
- If it fails or the binary is missing, the app still serves local + LAN and prints exactly why.

Worth knowing: quick tunnels get a fresh random hostname each start and are best-effort with no
SLA from Cloudflare. The named-tunnel path is the one to use for a link you hand out.

Side benefit: the tunnel is HTTPS, so the geolocation button starts working on phones —
browsers refuse it over plain http, which `web/index.html:234` already comments on.

## 7. Launcher — new `app.py` (the exe entry point)

A **foreground console app**. It prints a banner and then streams live progress; nothing runs
where you cannot see it.

```
============================================================
  Canna Cabana stock
============================================================
  This computer :  http://localhost:8000
  Phone / LAN   :  http://192.168.1.42:8000
  Public        :  https://<name>.trycloudflare.com
  Admin password:  set  (see settings.json to change)
  Data          :  C:\Users\aj\AppData\Local\CannaCabana
------------------------------------------------------------
  Ctrl-C to stop.
```

Reuses `_lan_ip()` (`server.py:373`). Opens the local URL in the default browser once, unless
`open_browser: false`. Runs uvicorn on `0.0.0.0:<port>` in the main thread; tunnel and job
worker are child process / daemon thread, both surfacing into this same console.

## 8. Packaging

- **New `requirements.txt`** — `fastapi`, `uvicorn[standard]`, `pydantic`. That is all the
  packaged app needs. **New `requirements-dev.txt`** — adds `playwright` for the browser
  fetcher in a source checkout.
- **Drop pandas.** `db.export_csv` (`db.py:131`) is its only use, one `read_sql_query` +
  `to_csv`. Rewrite with the stdlib `csv` module; that removes numpy/pandas/tzdata/dateutil
  (~80 MB) from the build. `beautifulsoup4` and `python-dotenv` are installed but unused —
  neither goes in `requirements.txt`.
- **Guard the browser path.** `fetchers.get_fetcher("browser")` raises a clear
  "not available in the packaged app — use the API source" when Playwright is absent, and
  `/api/capabilities` lets the UI hide the option rather than offer a button that errors.
  The browser fetcher, `browser.py`, `scrape.py`'s DOM path, and `discover.py` all stay intact
  in the repo for when the API endpoint changes shape.
- **`CannaCabana.spec`** — PyInstaller onedir. `datas`: `web/`, `catalog.json`, `stores.json`,
  `cloudflared.exe`. `hiddenimports`: `uvicorn.logging`, `uvicorn.loops.auto`,
  `uvicorn.protocols.http.auto`, `uvicorn.protocols.websockets.auto`, `uvicorn.lifespan.on`.
  Excludes: `pandas`, `numpy`, `playwright`, `bs4`.
  Onedir rather than onefile — onefile re-extracts ~50 MB to temp on every launch.
- **`build.ps1`** — committed, numbered steps, explicit PASS/FAIL per step, printed result:
  verify venv → install requirements → download `cloudflared.exe` if absent → run PyInstaller →
  smoke-test `dist\CannaCabana\CannaCabana.exe --selftest` → print the output path and size.
  Note: `.venv\pyvenv.cfg` records a stale home path from before the venv was moved into
  `CannaScraper\`; step 1 verifies the venv actually runs rather than assuming.
- Distribute `dist\CannaCabana\` zipped. An Inno Setup script for a single installer .exe is a
  reasonable follow-on, not part of this pass.
- `history.db`, `raw/`, `results.csv`, `report.html`, and the `audit_*`/`stock_*` CSVs are
  excluded from the bundle.

## 9. UI — `web/index.html`

- New collapsible **Catalogue index** panel above the search box. One row per province:
  name, store count, `indexed 92/92 · 5.2 h ago` or `never indexed`, a **Refresh** button, and
  when a build is running a progress bar + `[41/92] Haxton, Fort McMurray · ETA 24m` + **Cancel**.
  A **Resume** button appears when the newest run for that province is incomplete.
- Polls `/api/index/status` every 2 s while anything is active, every 30 s otherwise. Because
  the state lives server-side, a build started from the desktop is visible on your phone.
- On a 401 from any write, an inline password prompt posts to `/api/login` and retries.
- Hide the `Live: real browser` `<option>` (line 116) when `/api/capabilities` says no Playwright.
- Replace the "not indexed yet — run index_builder" text (line 303) with a link that opens the
  panel and starts the build for that province.

Existing CSS variables and the `.bar`/`.pill`/`.card` classes cover the panel; no new styling
system.

---

## Verification

Run in order. British Columbia (8 stores, ~3 min) is the cheap end-to-end index test — do not
start with Ontario.

**Source mode, before packaging**

1. `.venv\Scripts\python server.py` → panel lists all 5 provinces with real coverage from the
   existing 172 MB `history.db`.
2. Click Refresh on **British Columbia**. Expect progress within ~30 s, completion in ~3 min,
   and the age flipping to "just now". Search stays responsive throughout — that proves the
   build is off the event loop.
3. Click Refresh again, then Cancel. Expect it to stop after the current store, and the run to
   show as incomplete with a Resume button. Resume and confirm it skips finished stores.
4. `.venv\Scripts\python index_builder.py --province Manitoba --limit 2` → CLI output unchanged.
   This is the regression check on the `main()` refactor.

**Packaged**

5. `.\build.ps1` → `dist\CannaCabana\`.
6. Run the exe **from a different directory** (e.g. `C:\`). Confirm it uses
   `%LOCALAPPDATA%\CannaCabana`, seeds `catalog.json`/`stores.json` there, starts with an empty
   `history.db`, and search returns products immediately while every province reads
   "never indexed".
7. Confirm the "Live: real browser" option is absent.

**Public**

8. Open the printed `trycloudflare.com` URL **from a phone on cellular data with wifi off** —
   that, not the LAN URL, is what proves it is actually public.
9. Confirm geolocation now works over the tunnel.
10. In an incognito window on the public URL: search and results work; clicking Refresh or a
    province build returns 401 and prompts; the password unlocks it; a wrong password is
    rejected and backs off.

**Fresh machine**

11. Copy `dist\CannaCabana\` to another PC or a clean Windows user profile with no Python
    installed and run it. This is the actual distribution claim, and the only step that tests it.
