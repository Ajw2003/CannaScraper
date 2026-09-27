# Today — 2026-09-27

## What today is

A documentation day: scaffolding the six-tier `docs/` structure for the first time (the repo had
none of it before today), plus — in a separate, concurrent agent session — building the first
step of the GitHub Actions exploration (`.github/workflows/runner-ip-test.yml`).

This maps to no single roadmap milestone by itself; it's infrastructure for tracking all of them
going forward. The IP-test workflow maps to roadmap M7 (static Pages + per-province Actions),
step 1 of its acceptance criteria.

## What was done

- Created `docs/` with all six numbered tiers plus `plans/`, `archive/`, `generated/`:
  - `docs/1-landing/README.md` — full index, moving-parts table, systems table, links to root
    `README.md` and the root `PLAN_*.md` files.
  - `docs/2-roadmap/Roadmap.md` — 7 milestones (M1-M7) derived from `git log`, the README, and
    the `PLAN_*.md` files, each with a Contains list and an Acceptance criterion.
  - `docs/3-state/ProjectState.md` — status table, per-milestone detail, "the one thing that is
    not what it looks like" (the egress pool reads as done but ships disabled/unverified by
    default), and cross-cutting issues owned by no milestone.
  - `docs/4-systems/` — one doc each for catalog, store registry, fetchers+rate
    limiting+egress, index builder+work queue, persistence, web app+auth+tunnel,
    packaging+build, plus a `README.md` index that also names what was deliberately left out
    (`main.py`, `report.py`, `config.py`, the probe/dev scripts, `selftest.py`) and why.
  - `docs/6-decisions/Decisions.md` — one entry: the 2026-09-27 decision to explore a static
    GitHub Pages site fed by per-province GitHub Actions scrapers, Status: Standing, explicitly
    conditional on the runner-IP test passing.
  - `docs/README.md` — short plain-English entry point, written last per the scaffolding order.
- Recorded the static-Pages-+-Actions direction as roadmap milestone M7, with acceptance
  criteria gated on the (currently unknown) result of `runner-ip-test.yml`.

## What was deliberately not done

- **No existing file was moved, renamed, or edited.** `README.md`, `BUILD_INSTRUCTIONS.md`, and
  all three root `PLAN_*.md` files stay exactly where they are; `docs/1-landing/README.md` and
  `docs/2-roadmap/Roadmap.md` link to and reference them instead of absorbing their content.
- **`.github/` was not touched.** As of this session, `.github/` does not exist in this working
  tree at all — the workflow file another agent is building had not landed here yet.
- **No git operations were run** (`git add`/`commit`/`push`) — another agent is committing in
  this repo concurrently, per the task's explicit instruction.
- **`docs/archive/` and `docs/plans/` were left empty** (with no placeholder `README.md` added to
  `archive/`) because nothing in the repo is currently inert, and no docs-tree-specific plan
  exists yet to file under `plans/` — the root `PLAN_*.md` files predate and stay outside this
  structure.

## What got surfaced that is not today's job

- The `.github/` GitHub Actions IP-test result is unknown and blocks everything downstream in
  roadmap M7 — flagged in `docs/2-roadmap/Roadmap.md` and `docs/6-decisions/Decisions.md`, not
  resolved today.
- Two cross-cutting issues with no milestone owner, recorded in `docs/3-state/ProjectState.md`:
  what making the repo public means for every other system's assumptions (secrets in
  `settings.json`, the auth model), and the standing gap between the default `quick` Cloudflare
  tunnel and the named-tunnel requirement `PLAN_receiving_ledger.md` calls out as a prerequisite
  for real production use.

## What to do next, in order

1. **Wait on `runner-ip-test.yml`'s result** (owned by the other, concurrent agent session) —
   everything else in roadmap M7 is blocked on it. This is the single highest-leverage unknown
   right now.
2. If it passes: scaffold the first per-province scrape workflow and the publish-to-public-repo
   step it depends on.
3. If it fails: revisit roadmap M7's acceptance criteria and `docs/6-decisions/Decisions.md`'s
   2026-09-27 entry — the whole direction needs a different approach, and that reasoning belongs
   in a new decision entry, not a silent rewrite of today's.
4. Separately, and not urgently: `docs/3-state/ProjectState.md`'s cross-cutting issue about what
   "repo goes public" implies for `settings.json` secrets and the auth model is worth a real
   pass before M7 goes further than the IP test.
