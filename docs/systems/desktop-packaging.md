# Desktop packaging

## What it owns

Turning the source tree into a double-clickable Windows exe with correct data placement,
self-verification, staleness detection against its own source, and a public URL — everything
under `CannaScraper/dist/CannaCabana/`.

## How it works

**`paths.py`** is the foundation every other module (including `config.py`) can import
cycle-free: `APP_DIR` (read-only, bundled — `sys._MEIPASS` when frozen) vs. `DATA_DIR` (writable —
`%LOCALAPPDATA%\CannaCabana` when frozen, the source dir otherwise) (paths.py:1-16).
`paths.seed()` (paths.py:58) copies bundled `catalog.json` / `stores.json` / etc. into `DATA_DIR`
on first run so they become independently refreshable without touching the bundle.

**`app.py`** is the exe entry point. It parses `--no-tunnel` / `--set-password` / `--selftest`,
reconfigures stdout for utf-8 and line-buffering — necessary because Python block-buffers a
redirected stdout, which would otherwise silently hide all progress (app.py:24-37) — starts
uvicorn in a background thread, optionally starts a [tunnel](#tunnel), then polls
`server.rebuild_requested()` in its main loop to hand off to a self-initiated rebuild (see
[web-server-and-jobs.md](web-server-and-jobs.md)).

**Build stamping.** `buildinfo.py`'s `status()` (buildinfo.py:80) compares `buildinfo.json`'s
stamped commit/mtime against the newest mtime across the source checkout's `*.py` and `web/*`
files — **mtime, not git**, since git isn't guaranteed to be on the PATH of whoever eventually
runs the exe. It reports `rebuildable` by checking for `build.ps1`, `rebuild.ps1`, and
`.venv\Scripts\python.exe` in the recorded `source_dir`. `build.ps1` writes `buildinfo.json` as a
step before PyInstaller runs, so it gets bundled. As of 2026-09-01,
`CannaScraper/buildinfo.json` on disk records `built_at: 2026-09-01T04:59:28Z`,
`commit: 4c2cb87` — proof the stamping mechanism has actually run, though the interactive
rebuild-banner flow it feeds has not yet been exercised end to end; see
[ProjectState.md](../ProjectState.md#4-keep-the-exe-from-drifting-behind-source).

**`rebuild.ps1`** (source-only, deliberately not bundled — it operates on the checkout it lives
in) waits for the old process to exit, runs `build.ps1 -StopRunning`, and on success starts the
freshly-built exe; on failure it leaves the console open with the error so the old exe (untouched
by a failed build) is just a relaunch away.

### Tunnel

**`tunnel.py`**'s `Tunnel` class wraps `cloudflared.exe` as a child process, ties its lifetime to
the parent process via a Windows Job Object with `KILL_ON_JOB_CLOSE` (tunnel.py:47-113) so a hard
crash of the app can't orphan a public tunnel, and regexes the child's stdout for either a
`trycloudflare.com` quick-tunnel URL or a named-tunnel hostname announcement. Its output is
relayed line-by-line into the app's own console, not into a log file, so a failed connection is
visible immediately rather than requiring a log dig.

## Invariants

- `buildinfo.py` must be read via `paths.app()`, **never** `paths.seed()` — a copy surviving in
  `DATA_DIR` after the bundle it describes is gone would misreport staleness (buildinfo.py:13-14).
- `_SOURCE_GLOBS` deliberately excludes `catalog.json` / `stores.json` — those are
  runtime-refreshed data, not source, and including them would mark every build stale within a
  day of any catalog refresh (buildinfo.py:28-32). Note: `export_pages_json.py` (new, at the
  source root, part of the [Pages pipeline](pages-deployment.md)) *is* a `.py` file and would be
  picked up by this glob even though it has nothing to do with the desktop app — a cosmetic
  false-positive staleness trigger worth knowing about if the banner ever fires unexpectedly.
- `dirty: true` in `buildinfo.json` (as currently recorded) means the working tree had
  uncommitted changes at build time — expected during active development, worth checking before
  trusting a stamp for a "what exact commit is this" question.

## Traps

- **app.py:85-101** (`have_console`): `sys.stdin.isatty()` lies when run under Git Bash with
  stdin redirected from `/dev/null` — the real check calls `GetConsoleMode` directly on Windows.
- **app.py:104-121** (`prompt_password`): Windows' `getpass` reads the console device directly,
  not stdin, so with no real console it blocks forever instead of raising `EOFError`. Explicitly
  guarded against with the same console-detection as above.
- **app.py:200-203**: binds and prints `127.0.0.1` rather than `localhost` — Windows resolves
  `localhost` to `::1` first, costing a real ~2 second stall per request before falling back to
  IPv4.
- **`CannaCabana.spec` onedir, not onefile.** Onefile re-extracts the whole ~50 MB bundle to a
  temp directory on every single launch; onedir pays that cost once, at build time.
