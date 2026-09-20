# Today — 2026-09-20

A documentation day: scaffolding the six-tier `docs/` structure for the whole project (this outer
repo plus the `CannaScraper/` application it wraps) from scratch. Maps to no single roadmap
milestone — it's infrastructure for tracking all of them.

## What was done

- Read every existing plan doc (`docs/plans/*.md`, `CannaScraper/PLAN_*.md`,
  `CannaScraper/BUILD_INSTRUCTIONS.md`) and the CannaScraper README to establish ground truth.
- Delegated a systems-level survey of the CannaScraper codebase (file:line-cited) to a subagent,
  then wrote it up as eight tier-4 system docs under [docs/systems/](systems/).
- Wrote [Roadmap.md](Roadmap.md) (6 milestones, each with an acceptance criterion) and
  [ProjectState.md](ProjectState.md) (status against it, headline ~70%).
- Wrote [Decisions.md](Decisions.md), backfilling the real historical decisions found in the
  existing plan docs and README (schema normalization, potency-range fix, egress pool, pickup-mode
  requirement, browser-vs-HTTP finding, app-distribution shape) plus two newly-discovered,
  unresolved findings from today.

## What was deliberately not done

- **Did not touch `CannaScraper/`'s nested git repository.** Found that it still has its own
  `.git` (remote `github.com/Ajw2003/CannaScraper`), which blocks the GitHub Pages workflows from
  ever seeing its files. Fixing this means deciding whether to fold its standalone history into
  the outer repo — a call for the project owner, not something to do silently while writing docs.
  See [Decisions.md](Decisions.md#2026-09-20--cannascraper-still-has-its-own-git-the-outer-repo-cannot-track-its-files).
- **Did not write the missing catalog/stores weekly refresh workflow**, or fix the `site/app.js`
  active-selection bug, or add `robots.txt` — all found while documenting the Pages pipeline, all
  out of scope for a docs pass, all recorded in
  [ProjectState.md](ProjectState.md#cross-cutting-issues-that-belong-to-no-milestone).
- **Did not move or edit any existing file outside `docs/`** — `CannaScraper/PLAN_*.md` and
  `BUILD_INSTRUCTIONS.md` stay exactly where they are, referenced in place from
  [docs/README.md](README.md), rather than relocated across the two git roots.

## What got surfaced that is not today's job

- Two independent reasons the GitHub Pages pipeline can't complete an end-to-end run yet (see
  [ProjectState.md](ProjectState.md#the-one-thing-that-is-not-what-it-looks-like)) — worth fixing
  before dispatching any of the five province workflows for real.
- `CannaScraper/PLAN_followups.md` item 7 (the THC/CBD magnitude-fallback heuristic) is still
  open and not urgent; left as-is in [ProjectState.md](ProjectState.md)'s cross-cutting issues.

## What to do next, in order

1. **Decide the `CannaScraper/` git situation** — fold its nested `.git` into the outer repo (most
   likely: delete the nested `.git`, since its GitHub history at `Ajw2003/CannaScraper` is
   unaffected either way) so the outer repo can actually track its files. Nothing else in
   milestone 5 can be tested until this is resolved.
2. **Write the missing catalog/stores refresh workflow** and wire `deploy-pages.yml` to pick up
   its output — the second blocker in the same milestone.
3. **Run the per-province plan's own verification steps** once 1 and 2 are done — dispatch
   `scrape-alberta.yml` by hand first, per the plan's own recommendation.
4. **Run the fix-search-and-build-staleness plan's four verification steps** — the code has been
   built once (see [ProjectState.md](ProjectState.md#4-keep-the-exe-from-drifting-behind-source))
   but never proven interactively.
