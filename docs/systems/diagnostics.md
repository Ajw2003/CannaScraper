# Diagnostics & one-off investigation scripts

## What it owns

Proving the rest of the system still works (`selftest.py`), and, separately, the historical record
of a handful of one-off live-endpoint experiments that each answered a specific question about
the source site once and were never meant to run again on a schedule.

## How it works

**`selftest.py`** (908 lines) is a genuine, ongoing regression-test harness — not a diagnostic
one-off. It runs a real `uvicorn` instance against a sandboxed, throwaway data directory seeded
from the bundled catalogue (it never touches real data or the live network; index/live-check paths
use stand-in jobs), drives it with plain `urllib` rather than an in-process test client
specifically "so it exercises the actual server," and reports PASS/FAIL per step with a
failure-count exit code (`check()`, selftest.py:46) that `build.ps1` gates a green build on.
Invoked as `CannaCabana.exe --selftest`; `app.py`'s `run_selftest()` re-execs into a clean child
process specifically so it can pick its own sandbox directory before anything else imports `paths`.

**`report.py`** is the HTML report generator used by `main.py`'s CLI output
(`report.write_and_open`, main.py:469) — part of the CLI's normal output layer, not a diagnostic.

**`normalize_db.py`** is a real, rerun-safe maintenance tool that `db.py`'s `connect()`
deliberately defers to rather than auto-migrating (see
[data-store.md](data-store.md)). It backs up first (`history.db.pre-normalize` — 287 MB present
on disk, evidence it has actually been run) and verifies column-by-column before dropping the old
table.

**Four one-off investigation scripts** each answered a specific past question about the live site,
already produced their answer (often into a sibling CSV/JSON of results), and aren't imported by
anything else in the codebase:

| Script | Question it answered | Where the answer lives now |
|---|---|---|
| `verify_scan_fix.py` | Is the "Bag Changed" / `missingItems` bug actually fixed? | Regression check for the incident in [scraping-and-fetching.md](scraping-and-fetching.md) Traps |
| `payload_probe.py` | How much of `product/search`'s response do we keep, and can we ask for less? | Closed, no action available — `CannaScraper/PLAN_followups.md` item 5 |
| `ratelimit_probe.py` | What is the site's real rate limit? | The numbers now baked into `config.py` / [rate-limiting-and-egress.md](rate-limiting-and-egress.md) |
| `scan_ceiling_probe.py` | How many SKUs can `scan-multiple-items` accept per call before truncating? | [Decisions.md](../Decisions.md#2026-08-25--productsearch-chosen-over-scan-multiple-items-for-province-indexing) |

## Invariants

- `selftest.py` must never touch the real `%LOCALAPPDATA%\CannaCabana` data directory or make a
  live network call — its whole value is being safe to run on every build.
- `build.ps1` treats a `selftest.py` failure as a build failure, not a warning — a change that
  breaks a self-test should never reach `dist/CannaCabana/` unaddressed.

## Traps

- These four probe scripts sit at the `CannaScraper/` root next to the real, load-bearing modules,
  with no marker distinguishing "answered once, historical" from "actively maintained." If the
  site ever changes shape again, re-running the relevant probe (rather than guessing from a slow
  live run) is the right first move — `payload_probe.py`'s own comment says as much: "Re-run if
  the API ever changes; it will detect a working parameter automatically."
- Not wired into `build.ps1` or `selftest.py` — nothing will tell you if one of these scripts bit-rots
  against a future site change. That's acceptable for a closed investigation but worth knowing
  before relying on one of them as if it were live-monitored.
