# Today — 2026-09-20

A documentation day, in two sittings. The first scaffolded the six-tier `docs/` structure for the
whole project (this outer repo plus the `CannaScraper/` application it wraps) but was cut off
before finishing; the second finished it. Maps to no single roadmap milestone — it's
infrastructure for tracking all of them.

## What was done

- **Sitting one.** Read every existing plan doc (`docs/plans/*.md`, `CannaScraper/PLAN_*.md`,
  `CannaScraper/BUILD_INSTRUCTIONS.md`) and the CannaScraper README to establish ground truth.
  Wrote the nine tier-4 system docs under [docs/systems/](systems/) from a file:line-cited
  survey of the codebase, then [Roadmap.md](Roadmap.md) (6 milestones, each with an acceptance
  criterion), [ProjectState.md](ProjectState.md) (status against it, headline ~70%), and
  [Decisions.md](Decisions.md) (historical decisions backfilled from the plan docs and README,
  plus two findings that were still unresolved).
- **Sitting two.** Wrote the missing tier 1, [README.md](README.md), which every other doc was
  already linking to; added [archive/](archive/README.md) and [generated/](generated/README.md)
  with honest "nothing here yet" READMEs. Checked the whole set mechanically: all nine system docs
  have the four required sections, every `file:line` citation points at a line that exists, and
  no internal link is dead. Fixed seven links in
  [plans/fix-search-and-build-staleness.md](plans/fix-search-and-build-staleness.md) that were
  missing their `../../` prefix. Corrected [ProjectState.md](ProjectState.md), which still said
  the repo had no commits.
- **New finding:** the three workflows hard-code `ref: main` but the outer repo's only branch is
  `master`. Recorded as a third Pages blocker in
  [ProjectState.md](ProjectState.md#the-one-thing-that-is-not-what-it-looks-like) and in the Traps
  of [pages-deployment.md](systems/pages-deployment.md).

## What was deliberately not done

- **Did not touch `CannaScraper/`'s nested git repository.** It still has its own `.git` (remote
  `github.com/Ajw2003/CannaScraper`, branch `ApiFork`), which blocks the GitHub Pages workflows
  from ever seeing its files. Whether to fold its history into the outer repo is a call for the
  project owner, not something to do silently while writing docs. See
  [Decisions.md](Decisions.md#2026-09-20--cannascraper-still-has-its-own-git-the-outer-repo-cannot-track-its-files).
- **Did not write the missing catalog/stores weekly refresh workflow**, fix the `site/app.js`
  active-selection bug, add `robots.txt`, or rename `master` to `main` — all found while
  documenting the Pages pipeline, all out of scope for a docs pass, all recorded in
  [ProjectState.md](ProjectState.md#cross-cutting-issues-that-belong-to-no-milestone).
- **Did not move or edit any file outside `docs/`** — `CannaScraper/PLAN_*.md` and
  `BUILD_INSTRUCTIONS.md` stay where they are, referenced in place from [README.md](README.md).
- **Did not re-audit every system doc against the code.** Spot-checked structure and citations
  only; a line-by-line "does this still say what the code does" audit has not been done.

## What got surfaced that is not today's job

- Three independent reasons the GitHub Pages pipeline can't complete an end-to-end run yet (see
  [ProjectState.md](ProjectState.md#the-one-thing-that-is-not-what-it-looks-like)) — worth fixing
  before dispatching any of the five province workflows for real.
- `export_pages_json.py` and `site/` exist only as uncommitted files in the nested repo, so the
  outer repo's gitlink pin (`a0a2aaf`) does not include them.
- `CannaScraper/PLAN_followups.md` item 7 (the THC/CBD magnitude-fallback heuristic) is still
  open and not urgent; left as-is in [ProjectState.md](ProjectState.md)'s cross-cutting issues.

## What to do next, in order

1. **Decide the `CannaScraper/` git situation** — fold its nested `.git` into the outer repo (most
   likely: delete the nested `.git`, since its GitHub history at `Ajw2003/CannaScraper` is
   unaffected either way) so the outer repo can actually track its files. Nothing else in
   milestone 5 can be tested until this is resolved.
2. **Settle `main` vs `master`** so the three `ref: main` lines match the branch that exists.
3. **Write the missing catalog/stores refresh workflow** and wire `deploy-pages.yml` to pick up
   its output — the last blocker in the same milestone.
4. **Run the per-province plan's own verification steps** once 1–3 are done — dispatch
   `scrape-alberta.yml` by hand first, per the plan's own recommendation.
5. **Run the fix-search-and-build-staleness plan's four verification steps** — the code has been
   built once (see [ProjectState.md](ProjectState.md#4-keep-the-exe-from-drifting-behind-source))
   but never proven interactively.
