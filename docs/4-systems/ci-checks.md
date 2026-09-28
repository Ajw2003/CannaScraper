# CI checks: the "Checks" workflow

## What it is

`.github/workflows/checks.yml` runs on every pull request and every push to `main`. It is the
repo's regression net -- separate from the hourly scrape (`scrape-all.yml`/`scrape-one.yml`,
see [hourly-trigger.md](hourly-trigger.md)) and from publishing the page (`publish-site.yml`).
No job in it scrapes or calls `cannacabana.com` / `app.cannacabana.com`: `parity` and
`page-additions` run against a fixture history DB already in the `scrape-history` release, and
the one live-scan endpoint (`POST app.cannacabana.com/.../scan-multiple-items`) is stubbed in
`ci/live_parity_check.py`, never reached for real.

Six jobs, each its own GitHub status check:

### workflows

Runs `actionlint` (pinned version, downloaded via the official script) over every workflow file.
Catches YAML/expression mistakes actionlint knows about -- see "invalid runner context" below.

### shell

`bash -n` on every `ci/*.sh` (parses without running), then `shellcheck -x -P ci ci/*.sh`
(shellcheck ships on `ubuntu-latest`; `-x -P ci` lets it follow `source "$HERE/chain_lib.sh"`),
then `bash ci/test_chain_scripts.sh` -- the behavioural tests for the scrape chain / watchdog
dispatch logic against a stubbed `gh`.

### syntax

`python -m compileall` over the repo (excluding `raw/` and `__pycache__`, which hold scrape
output and bytecode, not source), `node --check site/static-api.js`, then
`pip install -r requirements.txt && python selftest.py` -- selftest never touches the network
or the real data dir (see `selftest.py:1`).

### pages-in-step

Pull requests only. `ci/check_pages_in_step.sh origin/$GITHUB_BASE_REF` fails the job with an
`::error file=web/index.html::` if a PR changes `web/index.html` (the desktop page) without
also changing `site/index.html` (the Pages copy -- see
`docs/plans/restore-original-page.md`). Needs `fetch-depth: 0` so the base ref is reachable.

### parity

The regression guard against the static site (`site/`, answered by `site/static-api.js`)
drifting from the original server (`server.py`). `ci/run_parity.sh`:

1. Downloads `history-saskatchewan.db` from the `scrape-history` release (or takes
   `HISTORY_DB=<path>` to skip the download -- used for local runs where GitHub releases aren't
   reachable), and a copy of `catalog.json`/`stores.json`/`geocode.json` from the repo root, into
   a `CANNACABANA_DATA` dir.
2. Reads the DB's own most recent index run for the province (`db.index_runs`) and writes it as
   `run.json`, so `ci/export_province.py --run-summary` embeds the *real* run the old server's
   `/api/index/status` will also report -- a made-up run id mismatches on `last_run`,
   `last_run_stores` and `incomplete`.
3. Runs `ci/export_province.py`, `ci/export_catalog.py` (**no** `--refresh`: the catalogue was
   just copied so its mtime is fresh and `catalog.catalog_is_stale()` is false -- this must not
   reach the network) and `ci/build_manifest.py`, laying `site/` + the exports out as gh-pages
   would serve them.
4. Runs `ci/check_export_shape.py` against the export (see "export-shape" below).
5. Starts the old server (`CANNACABANA_DATA=... uvicorn server:app`) and
   `python -m http.server` over the served dir, waits for both to answer, runs
   `ci/parity_check.py` (diffs identical requests to both) and `ci/live_parity_check.py` (same,
   for the "Check live now" path, against a stubbed scan endpoint).
6. Always stops both servers (`trap ... EXIT INT TERM`), pass or fail.

#### export-shape

`ci/check_export_shape.py` is a fast, server-less check run as part of `run_parity.sh`: it
asserts the exported province JSON has the keys the page's code reads (`province`,
`generated_at`, `run`, `stores`, `products`, `stock`, `times`), that each stock row has at least
the 11 documented fields (`ci/export_province.py`'s row comment) with `available` a 0/1 and the
time index an int, that `run.failed_stores` is a list, and that `data/index.json` lists the
province.

### page-additions

Runs `ci/pages_ux_check.py` (via `ci/run_pages_ux.sh`) headlessly in Chromium against this
branch's `site/` files served over the **published** data from `gh-pages`
(`git fetch origin gh-pages` + `git archive origin/gh-pages`), laid out as
`ROOT/CannaScraper/...` the way the script expects.

Data-independent by design: `gh-pages`' data changes every hour (new stock, new failed stores,
sometimes different counts), so nothing in the script hard-codes a live-data fact any more.
Every expectation comes from the same API the page itself calls:

- default province: whatever `/api/provinces` reports as `default`, not a fixed `"Alberta"`.
- "blue dream" search count: compared to `/api/search`'s own `total` for that query (same
  params the page's `search()` sends), not a fixed `19`.
- "back restores list length": compared to the count captured before the card was opened, not a
  fixed `100`.

Everything else it checks (card shows a price, store rows have "view" links, no sideways scroll
at phone width, the failed-store notice, province remembered after reload) is about the page's
*code*, not the data, so those stayed as they were.

## How to run each locally

```
# actionlint (download once, pinned):
bash <(curl -sSL https://raw.githubusercontent.com/rhysd/actionlint/main/scripts/download-actionlint.bash) 1.7.7
./actionlint .github/workflows/*.yml

# shell:
bash -n ci/*.sh
pip install --user shellcheck-py   # shellcheck isn't preinstalled locally
~/.local/bin/shellcheck -x -P ci ci/*.sh
bash ci/test_chain_scripts.sh

# syntax:
python -m compileall -q -x '(^|/)(raw|__pycache__)/' .
node --check site/static-api.js
pip install -r requirements.txt && python selftest.py

# pages-in-step (either direction):
bash ci/check_pages_in_step.sh origin/main

# parity (needs Playwright's Chromium; HISTORY_DB skips the release download):
pip install -r requirements.txt playwright==1.62.0
python -m playwright install chromium   # or set CHROME_PATH to one you already have
HISTORY_DB=/path/to/history-saskatchewan.db PROVINCE=Saskatchewan bash ci/run_parity.sh /tmp/parity

# page-additions:
git fetch origin gh-pages
bash ci/run_pages_ux.sh /tmp/pages-ux
```

`CHROME_PATH` overrides which Chromium binary `ci/parity_check.py`, `ci/live_parity_check.py`
and `ci/pages_ux_check.py` launch. Unset, they fall back to
`/opt/pw-browsers/chromium-1194/chrome-linux/chrome` if that path exists (a sandbox with a
pre-fetched build), else Playwright's own bundled Chromium.

## Invariants

- No job reaches `cannacabana.com` or `app.cannacabana.com`. `parity`/`page-additions` work
  from a downloaded fixture DB and published `gh-pages` data; the one live-scan endpoint is
  always stubbed.
- `run_parity.sh` always stops the servers it starts, pass or fail (`trap ... EXIT INT TERM`).
- `page-additions`' expectations are derived from the API at run time, never a hard-coded
  live-data fact, so an hourly data change cannot turn this check red on its own.

## Traps

- `top-level permissions: contents: read` plus a job env block that referenced `github.job` (an
  earlier draft of this workflow) is invalid: `github.job` is a valid *expression* everywhere,
  but naming an env var after a job-context field some contexts don't carry at parse time can
  produce a workflow GitHub Actions silently refuses to run, or that only fails once triggered --
  actionlint is what catches it before either happens. Keep the `workflows` job first and treat
  any actionlint failure as blocking.
- `ci/export_catalog.py` refreshes the catalogue over the network whenever
  `catalog.catalog_is_stale()` is true, and `config.CATALOG_CACHE` is stamped fresh only because
  `run_parity.sh` copies `catalog.json` right before calling it. Reordering those two steps (or
  reusing an old `CANNACABANA_DATA` dir across runs without a fresh copy) reintroduces a real
  network call inside a job that is supposed to make none.
- `/api/index/status` parity depends on `run.json` naming the DB's *actual* latest index run
  (`db.index_runs`), not an arbitrary id -- see "parity" above. A stub run id reliably produces
  one mismatch on `last_run`/`last_run_stores`/`incomplete`.

## Background: the incidents this workflow guards against

- **2026-09-28, silent feature loss.** A restore of the original desktop page onto GitHub Pages
  (`docs/plans/restore-original-page.md`) turned up features and fixes that earlier edits had
  quietly dropped, with nothing marking that they were gone. `pages-in-step` and `parity` exist
  so a change to one half of the page (desktop vs. Pages) or a divergence between the old server
  and the static port cannot land unnoticed again.
- **Invalid runner context in a job's `env:` block.** An earlier draft of a workflow in this
  repo set a job-level `env:` value from a context field that isn't available at that point,
  producing a workflow that GitHub Actions would not run as intended. `actionlint` (`workflows`
  job) catches this class of mistake before a run is ever attempted.
- **Unreliable schedule/chain scripts.** The scrape chain and its watchdog
  ([hourly-trigger.md](hourly-trigger.md)) depend on `ci/chain_lib.sh`'s dispatch-with-retry and
  "is a run already active" logic being exactly right -- a bug there can silently stop the
  hourly scrape. `ci/test_chain_scripts.sh` (`shell` job) exercises that logic against a stubbed
  `gh` on every PR, not just when someone remembers to run it by hand.
- **Drift between `web/` and `site/`.** The Pages site is a hand-kept copy of the desktop page
  plus one added script; nothing stopped `web/index.html` and `site/index.html` from silently
  diverging again after the 2026-09-28 restore. `pages-in-step` makes that a failing check
  instead of something noticed later by inspection.
