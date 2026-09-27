# Packaging & build

## What it owns

Turning the source checkout into a distributable `CannaCabana.exe` and stamping the build with
what source it came from: `build.ps1` (13,390 bytes / several hundred lines), `CannaCabana.spec`
(PyInstaller spec), `buildinfo.py` (134 lines).

## How it works

- `build.ps1` is a numbered, pass/fail pipeline: every step prints PASS or FAIL and the script
  stops at the first failure, so a green run means the exe in `dist\CannaCabana\` actually
  started and passed its own self-test — not merely that PyInstaller exited zero
  (`build.ps1:8-10`). Flags: `-SkipCloudflared`, `-Clean`, `-StopRunning` (`build.ps1:3-6`).
- Packaging is **onedir, not onefile** — onefile re-extracts the whole bundle to a temp folder on
  every launch, which for ~60 MB is a visible startup delay (`CannaCabana.spec:8-9`).
- Bundled data (`CannaCabana.spec:19-24`): `web/`, `catalog.json`, `stores.json`,
  `watchlist.txt` — read-only files that travel with the app; `paths.seed()` copies the data
  ones into `%LOCALAPPDATA%\CannaCabana` on first run so they become writable
  (`CannaCabana.spec:17-19`).
- `cloudflared.exe` is bundled if present (fetched by `build.ps1`) so the app can open a public
  tunnel with no separate install; without it the app still serves local+LAN and says why the
  public URL is unavailable (`CannaCabana.spec:26-30`).
- Modules only reached dynamically (via `--fetcher`/`__main__`/string-based uvicorn dispatch) are
  listed explicitly as `hiddenimports`, since PyInstaller's static analysis misses them:
  `index_builder`, `egress`, `workqueue`, `selftest`, `tunnel`, `jobs`, plus several
  `uvicorn.*` submodules (`CannaCabana.spec:41-58`).
- **Build stamping** (`buildinfo.py:1-16`): `build.ps1` writes `buildinfo.json` into the source
  directory right before the PyInstaller run, and `CannaCabana.spec` bundles it
  (`CannaCabana.spec:33-39`). `buildinfo.status()` compares that stamp against the checkout it
  names — an exe built before a fix landed used to serve the old code silently and look
  identical from the outside; this is the fix for that class of bug
  (`buildinfo.py:5-9`).
- The staleness check watches `*.py` and `web/*` mtimes (`_SOURCE_GLOBS`, `buildinfo.py:29`).
  It deliberately does **not** watch `catalog.json` / `stores.json`, since those are data the
  running app refreshes on its own — treating them as source would mark every build stale within
  a day of itself (`buildinfo.py:26-29`).
- `buildinfo.read()` uses `utf-8-sig`, not `utf-8`: `build.ps1` writes the stamp with PowerShell
  5.1's `Set-Content -Encoding utf8`, which prepends a BOM; plain `utf-8` would leave that byte
  in the string and make `json.load` fail silently from the caller's point of view
  (`buildinfo.py:31-35`).
- Read through `paths.app()`, never `paths.seed()` — the stamp describes the bundle, and a copy
  surviving in the data dir after the bundle it describes is gone would be a lie
  (`buildinfo.py:13-14`).
- `--selftest` (in `app.py`) runs the built-in checks and exits, touching nothing — used both
  as a CLI flag and as a build-pipeline gate (`README.md:255`, `build.ps1:9-10`).

## Invariants

- A `buildinfo.json` describing a bundle must not persist in the data directory after that
  bundle is gone (`buildinfo.py:13-14`).
- `catalog.json`/`stores.json` must never be added to the staleness-source globs — they are
  runtime-refreshed data, not source (`buildinfo.py:26-29`).
- `build.ps1` must stop at the first failing step — a partially-succeeded build must never be
  reported as passing (`build.ps1:8-10`).

## Traps

- Reading `buildinfo.json` with plain `utf-8` — the BOM PowerShell 5.1 writes will break
  `json.load` (`buildinfo.py:31-35`).
- Running `pyinstaller CannaCabana.spec` directly (skipping `build.ps1`) works, but skips the
  PASS/FAIL gate and the fresh `buildinfo.json` stamp; the spec treats a missing stamp as
  "nothing to compare, not stale" rather than failing (`CannaCabana.spec:33-39`).
- Forgetting a dynamically-imported module in `hiddenimports` — PyInstaller's static analysis
  will not find `index_builder`, `egress`, `workqueue`, etc. on its own
  (`CannaCabana.spec:41-58`).
