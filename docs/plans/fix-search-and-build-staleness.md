# Fix search in the packaged app, and stop the exe drifting behind the source

## Context

Typing in the search box produces nothing — no product cards, no images, no
names, no error message. It used to work.

**The cause is not a code bug on `ApiFork`. It is that the exe was never
rebuilt.** Diagnosed live against the running process:

| Evidence | Result |
|---|---|
| What is listening on :8000 | PID 11356, `dist\CannaCabana\CannaCabana.exe`, built **Aug 25** |
| Fix commit `4c2cb87` "Fix search returning nothing in the packaged app" | **Aug 29** — four days *after* the exe |
| `GET /api/catalog/status` (endpoint added by the fix) | **404** — the fix is absent from the bundle |
| `grep -c catnote dist\...\_internal\web\index.html` | **0** — bundle ships the pre-fix frontend |
| `GET /api/search?q=resin&stocked_only=false` | HTTP 200 with 226 real products — **after 38.6 seconds** |

The 38.6 seconds is the whole story. The bundled `catalog.py` is the old
age-gated `get_catalog()`: past `CATALOG_MAX_AGE_H` (24h) it refuses to read
the cache and runs `fetch_catalog()` inline — a multi-page walk of
`/products.json` with a one-second sleep between pages — from inside
`/api/search`. `%LOCALAPPDATA%\CannaCabana\catalog.json` was last written Aug
24, so **every keystroke** re-downloads the entire catalogue. The bundled
frontend debounces but has no in-flight guard and no `r.ok`/`try` around the
fetch, so nothing ever renders. It "used to work" because on Aug 25 the
catalogue was under 24h old and the gate never fired.

`ApiFork` HEAD already fixes all of this. The exe just predates it.

So: rebuild — and then make this specific failure impossible to repeat, since
"the running exe silently predates the source" produced no signal at all.

## Approach

Four pieces, in order. Piece 1 fixes the reported symptom today; 2–4 are the
prevention the user asked for.

### 1. Rebuild the exe from `ApiFork` HEAD

No code change. `build.ps1 -StopRunning` already handles the running copy
locking `dist\`, and step 8 runs `CannaCabana.exe --selftest` against the
built bundle, so a green run is real evidence.

### 2. `build.ps1` — refresh the catalogue before bundling

New step between the current step 5 (*no running copy locking dist\*) and step
6 (*PyInstaller bundle*):

```
Step-Start "catalogue is current"
& $Py -c "import catalog; catalog.refresh_catalog(verbose=False)"
```

In a source checkout `config.CATALOG_CACHE` resolves to the project's own
`catalog.json` (`paths.seed()` → `DATA_DIR` → `_HERE` when not frozen), which
is exactly the file `CannaCabana.spec` bundles. So this refreshes the shipped
catalogue in place.

**Warn, do not fail, on network error** — with the fix in place a stale
bundled catalogue is harmless (search reads it regardless; the startup hook
tops it up). A build should not be blocked by the site being down.

### 3. Build stamp + staleness detection

**`buildinfo.py`** (new, ~30 lines). Reads `paths.app("buildinfo.json")` —
`paths.app`, **not** `paths.seed`: it describes the bundle and must never be
copied into `DATA_DIR`.

```
{"built_at": "<iso8601>", "commit": "<sha>", "branch": "ApiFork",
 "dirty": false, "source_dir": "C:\\Users\\aj\\Desktop\\...\\CannaScraper"}
```

Written by a new `build.ps1` step *before* PyInstaller (so it gets collected),
and added to `datas` in `CannaCabana.spec` guarded by `os.path.isfile` so a
bare `pyinstaller CannaCabana.spec` still works.

`buildinfo.status()` returns `{frozen, built_at, commit, branch, source_dir,
source_exists, source_newest, stale, rebuildable}`:

- `source_newest` — newest mtime across `*.py` and `web\*` in `source_dir`.
- `stale` = `source_newest > built_at`. **mtime, not git** — git is not
  guaranteed on the PATH of whoever runs the exe, and mtime catches both a new
  commit and uncommitted edits. The recorded `commit`/`branch` are captured at
  build time (where git *is* available) and shown for context only.
- `rebuildable` = `source_dir` exists and contains `build.ps1` and
  `.venv\Scripts\python.exe`.
- Not frozen, or no `buildinfo.json` → `stale: false`. Running from source is
  never out of date with itself.

**`server.py`** — `GET /api/build/status`, open like `/api/index/status`
(read-only). Alongside the existing `/api/catalog/status`.

**`web/index.html`** — a banner above `#catnote`, shown only when `stale`,
following the existing `#catnote` pattern (same `loadCatalog()` shape, same
`post()` helper for the admin-gated call, same `fmtAge()`):

> This app was built 6 days ago from `4c2cb87`. The source has changed since.
> **[Rebuild and restart]**

**`app.py`** — one line in the startup banner next to the existing
`row("Data", ...)`, so it is visible without opening the page:

```
  Build         :  6 days old — source has changed, rebuild available
```

### 4. The rebuild prompt itself

`POST /api/build/rebuild`, admin-gated via `Depends(auth.require_admin)` —
same gate as `/api/refresh` and `/api/catalog/refresh`, since it runs a build
script and restarts the server.

It cannot rebuild in-process: PyInstaller has to delete `_internal\*.pyd`,
which the running exe holds open. So it launches a **detached** helper and
then asks itself to exit.

**`rebuild.ps1`** (new, in the source dir — source-only, deliberately *not*
bundled; it operates on the checkout it lives in):

1. Wait for the given PID to exit (bounded, ~60s, then hard-stop).
2. `.\build.ps1 -StopRunning` in its own directory.
3. On success, start `dist\CannaCabana\CannaCabana.exe`. On failure, leave the
   console open with the error — the old exe is untouched by a failed build,
   so the fallback is simply relaunching it.

The endpoint spawns it with `subprocess.Popen([... "powershell",
"-ExecutionPolicy", "Bypass", "-File", rebuild.ps1, "-WaitForPid", str(
os.getpid())], creationflags=CREATE_NEW_CONSOLE | DETACHED_PROCESS)`, returns
`{"rebuilding": true}`, then sets `srv.should_exit`. The browser shows
"Rebuilding — this takes a few minutes; the page will come back" and polls
`/api/build/status` until it answers again.

A new console window is on purpose and consistent with the rest of the app:
the build prints PASS/FAIL per step and the self-test result, and per `app.py`
progress that takes minutes belongs somewhere watchable.

### Files touched

| File | Change |
|---|---|
| `buildinfo.py` | new — read the stamp, compute staleness |
| `rebuild.ps1` | new — wait for exit, build, relaunch |
| `build.ps1` | +2 steps: refresh catalogue, write `buildinfo.json` |
| `CannaCabana.spec` | bundle `buildinfo.json` (guarded by `isfile`) |
| `server.py` | `GET /api/build/status`, `POST /api/build/rebuild` |
| `web/index.html` | stale-build banner + rebuild button + repoll |
| `app.py` | one banner row when the build is stale |
| `selftest.py` | see below |

### Reuse

Nothing new is invented where something exists:

- `paths.app()` / `paths.FROZEN` — [paths.py:53](CannaScraper/paths.py:53)
- `auth.require_admin` — the gate already used by `api_catalog_refresh`,
  [server.py:663](CannaScraper/server.py:663)
- `fmtAge()`, `post()`, `esc()`, `$()` — [web/index.html:629](CannaScraper/web/index.html:629),
  [620](CannaScraper/web/index.html:620)
- `loadCatalog()`'s poll-while-busy pattern — [web/index.html:305](CannaScraper/web/index.html:305)
- `Step-Start` / `Step-Pass` / `Step-Fail` — [build.ps1:24](CannaScraper/build.ps1:24)

## Verification

Every command below runs in **PowerShell 5.1**, working directory
`C:\Users\aj\Desktop\CannaCabanaScraper\CannaScraper`.

**Step 1 — rebuild (this alone fixes the reported symptom).** UNTESTED:

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -StopRunning
```

Expect: numbered steps each printing `PASS`, ending with the packaged app's
self-test at `39/39` (37 existing + the 2 new build-stamp checks) and a size
line. It stops at the first `FAIL`.

**Step 2 — the actual symptom, measured.** The pre-rebuild baseline recorded
above is 38.6s; the fix must land well under a second. UNTESTED:

```bash
powershell -NoProfile -Command "$sw=[Diagnostics.Stopwatch]::StartNew(); $r=Invoke-WebRequest 'http://127.0.0.1:8000/api/search?q=resin&stocked_only=false' -UseBasicParsing; \"HTTP $($r.StatusCode) in $([Math]::Round($sw.Elapsed.TotalSeconds,2))s\"; (Invoke-WebRequest 'http://127.0.0.1:8000/api/catalog/status' -UseBasicParsing).Content"
```

Expect: `HTTP 200 in 0.0Xs`, then a catalogue-status body (not a 404).

**Step 3 — the browser, which is where the complaint came from.** Drive
`http://127.0.0.1:8000` with the Browser pane tools: type `resin`, then
`read_page` to confirm product cards render with titles and `<img>` sources,
`read_console_messages` to confirm no page errors, and a screenshot as proof.
This is the check that "visuals, names and all the correct accompanying data"
are back — the API returning 200 is necessary but not sufficient.

**Step 4 — the staleness guard actually fires.** Touch a source file
(`(Get-Item .\server.py).LastWriteTime = Get-Date`), reload the page, and
confirm the banner appears and `/api/build/status` reports `stale: true`.
Then click **Rebuild and restart** and confirm the app exits, the build
console opens, and the page comes back on its own.

**New self-tests** in `selftest.py`, next to the existing catalogue checks at
[selftest.py:751](CannaScraper/selftest.py:751):

- *build stamp is present and readable in a packaged build* — `buildinfo.status()`
  returns a parseable `built_at` when frozen, and `stale: false` from source.
- *a source file newer than the build marks it stale* — the regression this
  whole piece exists to catch.
