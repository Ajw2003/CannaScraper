# Guard: rebuilding existing behaviour must not silently drop features

Input for a new guard in the house-rules plugin. The observation comes first, because the guard
should be judged against it: any proposed rule or hook should have stopped this specific case.

Status: **observation recorded, guard not built.** Written 2026-09-28; scope widened the same
day at the user's request from "a replacement" to every kind of change listed under *Scope*.

## Scope: which changes the guard covers

Any change that **re-creates behaviour that already exists**, whatever it is called. Each of these
is in scope, and none needs the others' shape to trigger:

| Kind | What it looks like | Example from this repo |
|---|---|---|
| **Replacement** | A new thing takes over an existing thing's job, and the old one stays or is retired later | `site/index.html` taking over `web/index.html` (the observation below) |
| **Port** | The same thing moved to another platform, runtime, host or framework | Desktop app + tunnel → static GitHub Pages site |
| **Rebuild / rewrite** | The same thing written again from scratch, in place or beside it | Rewriting a page or module instead of editing it |
| **Restructure** | The same behaviour split, merged or reorganised across files, jobs or modules | `scrape-province.yml` split into `scrape-one.yml` + `scrape-all.yml` (PR #6), done without an inventory |
| **Migration** | Data, schema or storage moved to a new shape that the code then reads | `db.py`'s split of the `observations` table into `obs` / `products` / `store_meta` |

What they share: after the change, the only features that exist are the ones someone
**re-created**. Anything nobody listed is gone, and nothing in the diff says so, because a new file,
a moved block or a split job doesn't show up as deleted lines. The restructure example is included
because the same process gap applied there. Checked afterwards (2026-09-28, old
`scrape-province.yml` at `229a38d` against `scrape-one.yml` + `scrape-all.yml` +
`ci/publish_gh_pages.sh`): nothing user-visible was lost; the five core scrape steps moved over
byte-for-byte. Three small things were dropped without being mentioned: the verify step no longer
fetches the page itself (`curl "$SITE"`), only `data/index.json`; each run's JSON is no longer
kept as a downloadable Actions artifact (was `data-<slug>`, 3 days); and the publish log no longer
says which provinces' data it kept or that it made the first publish. One maintenance cost was
added: a new province now has to be added in two lists (`ALL_PROVINCES` in `scrape-all.yml` and the
dispatch choices in `scrape-one.yml`) instead of one. So the gap was real, but the losses were
minor, which is luck, not process.

Out of scope: an ordinary edit that changes a few lines of an existing file. That's already
visible in the diff, and `edit-place.md` covers it.

## What happened

The CannaScraper stock page was moved from a desktop app (`web/index.html`, served by
`server.py`) to a static GitHub Pages site (`site/index.html`). The new page kept the core lookup
and dropped a dozen working features. The user was told about only three of them, and found the
rest by noticing the site "feels like it's missing a few things".

Measured afterwards on the same Saskatchewan data (full comparison with screenshots:
`docs/generated/pages-audit/index.html`):

| | Count | Told the user beforehand? |
|---|---|---|
| Lost, needs a server (live check, admin login, rebuild, catalogue refresh, stale-build banner) | 5 | 3 of 5 |
| Lost, could have been kept on a static site | 10 | none |
| Changed for the worse (price emphasis, category list) | 2 | none |
| Bugs introduced (CBD rounding, failed-store names never exported) | 2 | none |

The most costly loss was search coverage. The old search matched title, brand, category and size,
ranked by match quality (`catalog.py:183-223`). The new one matches title and brand only, unranked
(`site/index.html:268-271`). Searching "pre-roll" returns 232 in-stock products in the old app and
7 on the new site; "edibles" and "3.5 g" return none.

## How it got through

1. **The spec was written from memory, not from the original.** The page was delegated to a
   subagent with a feature list written without reading `web/index.html`. "Search box:
   case-insensitive match across title + brand" was in that spec, so the regression was specified,
   not improvised. The subagent built what it was told and reported success honestly.
2. **Only the forced losses were disclosed.** The plan's "Out of scope" section
   (`docs/plans/static-site-one-province.md`, first version) listed "Live re-check, admin login,
   rebuild button (need a server)": losses that were obviously forced. Nobody looked for losses
   that weren't forced, so nothing surfaced them.
3. **Verification compared the new thing with itself.** Every check (headless browser, real
   data, phone width, no console errors) confirmed that the new page worked. None compared it
   with the old one. "Search returns 19 results for blue dream" passed; the same check against the
   old app would have matched too. The first like-for-like comparison was the audit the user asked
   for after noticing.
4. **The existing rule was scoped to the wrong shape.** `rules/detail/edit-place.md` says a
   wholesale rewrite "gets approved by name every time: before it happens, I say plainly which
   existing content is being discarded". It only covers rewriting a file that already exists. The
   replacement was a *new* file taking over an old file's job, so the rule never engaged, though
   the effect was the same: everything the old version did was discarded unless re-created.

## Second observation: the workflow restructure (same day)

The same gap showed up a second time, in a different kind of change, and a new gap was added on
top of it.

**Timeline (UTC, 2026-09-28).**

| Time | What happened |
|---|---|
| 03:15 | `8c3e58e`: the scrape workflow is restructured from one `scrape-province.yml` into `scrape-one.yml` + `scrape-all.yml` + `ci/publish_gh_pages.sh`. No inventory of the old workflow. |
| 03:17 | Merged as PR #6. |
| ~03:25 | The user asks what the Pages site lost; the audit finds the first observation above. |
| 03:39–03:42 | This document is written, then widened to cover restructures. The restructure is listed as an example with "whether it lost anything has **not** been checked". |
| after 03:42 | The user replies "so check?". |
| 03:48 | `1136df0`: checked, in three commands. Nothing user-visible lost; three small things dropped without mention (see *Scope*). |

**The gaps, in order.**

1. **Same gap, different kind of change.** The restructure had no parity inventory, just like the
   page. It came out almost clean for a reason nobody designed: the five core scrape steps were
   carried over by transforming the old file (`git mv`, then reusing the step text), so they moved
   byte-for-byte. Everything that was lost sat in the parts that were **written again** (the
   publish and verify logic), not the parts that were **moved**.
2. **Knowing about the gap didn't close it.** By 03:42 the gap had been named, written up and
   applied to this very restructure, and it still wasn't checked. Writing "not checked" read as
   honest disclosure, but it pushed a three-command job onto the user, who had to ask for it.
   Disclosing an unverified risk is right only when the check is expensive or needs the user.
   When the check is cheap and within reach, disclosure without doing it is just a slower way of
   not knowing.
3. **"The losses were minor" is luck, not process.** The same process that lost 232→7 search
   results here lost a page fetch in a verify step. Nothing in the process decided which it
   would be.

**What it adds to the guard.**

- **The inventory is most needed where code is re-written, not moved.** A guard can weight its
  attention: text carried over verbatim is low risk; logic re-expressed in a new place is not. Don't
  rely on git's rename detection to tell them apart: `8c3e58e` shows `scrape-province.yml` as
  deleted and `scrape-one.yml` as added, even though its five core steps were carried over
  byte-for-byte, because too much of the file around them changed. Compare at the level of steps,
  functions or blocks instead. Re-expressed logic (a new script replacing an inline step,
  a reusable workflow replacing a matrix job, a JS port of a Python function) is where the losses
  were, both times.
- **"Not checked" needs a reason.** When a reply or document says a risk is unchecked or
  unverified, the guard should ask whether the check is cheap and within reach, and if so, require
  it to be done before the turn ends rather than handed over. This is a sibling of
  `evidence-before-claims.md`: that rule stops claiming what wasn't tested; this one stops leaving
  untested what could be tested in a minute.

## Shape of the failure (what a guard should recognise)

- Existing behaviour is **re-created rather than edited**: by any of the kinds under *Scope*. The
  old code keeps existing, is moved, or is retired later, so the loss never appears as a deletion.
- The result is **judged by whether it works**, not by whether it does **everything the
  original did**.
- **Forced losses are named, unforced ones are not.** Naming the forced ones makes the
  disclosure look complete.
- **Delegation multiplies it.** A spec written without reading the original hands the gap to an
  agent that has no way to notice it.

## Proposed guard

### Rule text (for `rules/house-rules.md`, with detail in `rules/detail/`)

> **Re-creating existing behaviour needs a parity inventory first.** Before a replacement, port,
> rebuild, rewrite, restructure or migration — any change after which the only features left are
> the ones someone re-created — inventory what the original does from its code, not from memory:
> every user-visible feature and behaviour, and for a restructure every trigger, input, output and
> side effect. Show the user the inventory marked keep / change / drop, with the reason for each
> drop. Verify the result against the original with the same inputs, not only against itself. A
> dropped feature the user wasn't shown is a regression, even if the new version works.

Also widen `edit-place.md`'s rule to name this case explicitly, so the two rules point at each
other.

### Mechanical checks (hook ideas, in rough order of value)

1. **Parity inventory required before the plan is approved.** When a plan or `ExitPlanMode`
   text, or a delegation prompt, contains language for any in-scope kind — replace, port, move
   to, rebuild, rewrite, redo, from scratch, restructure, split into, merge into, reorganise,
   consolidate, migrate, v2, "static version of", "instead of the old" — require a
   `docs/plans/<name>-parity.md` with a keep / change / drop table before execution is delegated.
   The table's rows should cite the original's code (`file:line`), which shows it was read.
2. **Delegation prompt check.** When an `Agent` / executor prompt describes an in-scope change to
   an existing file or feature, flag it unless the prompt either includes the parity
   table or instructs the agent to read the original and inventory it first.
3. **Parity verification before claiming done.** For any in-scope change, the completion report must
   include at least one like-for-like comparison (same input to old and new, outputs compared).
   The evidence-before-claims check could require a quoted old-vs-new result, not just a
   new-only one.
4. **Feature-surface diff as a cheap tripwire.** For a UI, list the controls in both
   files (`<select>`, `<button>`, `<input>`, `<option>` labels, fetch endpoints) and report the
   ones present in the old and absent in the new. For this case it would have flagged, among
   others: `#top` (5/10/25 nearest), `#sort`, `#src` (live check), `#near` (city/postal code),
   `#stocked` (in-stock-only toggle), `#toggle` ("Show N without it"). That's not proof of
   loss, but it's a list to account for. Checked on 2026-09-28: comparing element IDs alone,

       comm -23 <(grep -o 'id="[a-z]*"' web/index.html | sort -u) \
                <(grep -o 'id="[a-z]*"' site/index.html | sort -u)

   lists 23 IDs only in the old page, including all six above plus the live-check (`refresh`,
   `prog`) and admin (`idxpanel`, `unlock`, `pw`) controls. It's crude (it can't see a changed
   search rule or a demoted price), which is why it's a tripwire and not the guard. The equivalent
   for a restructure of workflows or jobs: diff the set of triggers (`on:` events, schedules,
   inputs), outputs (published files, releases, artifacts) and side effects between the old and new
   files; for a migration: diff the columns and queries that read them.

5. **Unchecked-but-cheap tripwire.** Flag replies and documents that say a risk is "not
   checked", "unverified", "haven't confirmed" or similar, and ask in the same turn whether the check
   can be run now. From the second observation: the phrase "whether it lost anything has **not**
   been checked" was committed at 03:42; the check it deferred took three commands.

### How to test the guard

It passes only if it engages for each kind under *Scope*, not just the one that happened. Primary
case, replay this one: the old `web/index.html`, `server.py` and `catalog.py`, and a request to "serve
the page through GitHub Pages". The guard passes if, before any code is written, the user sees
an inventory that includes at least the search-coverage change (title+brand vs.
title/brand/category/size), the price-emphasis change, the unstocked-products toggle, city/postal
location, nearest-N, and store sort, each marked keep / change / drop.

Secondary cases, each of which must also trigger the inventory step:

- **Restructure:** "split `scrape-province.yml` into a per-province workflow and an
  orchestrator". The inventory should list the old workflow's triggers (schedule, dispatch with
  province choice, push paths), its outputs (history release, `gh-pages`, the live-site check) and
  its concurrency behaviour, marked keep / change / drop.
- **Rebuild in place:** "rewrite `site/index.html` cleanly". Same inventory as the primary case.
- **Deferred check:** a reply that says "I haven't checked whether the restructure lost anything"
  when the old and new files are both in the repo. The guard should prompt for the check in the
  same turn instead of letting the reply end.
- **Migration:** "move history from SQLite to per-run JSON". The inventory should list every
  reader of the old tables (`db.latest_observations`, `index_runs`, `scan_failure_streaks`, the
  exporter, the pruner).

## Evidence

- Comparison and screenshots: `docs/generated/pages-audit/index.html` (also published at
  https://claude.ai/artifact/KkdAM1nhkJxsu9oHP1LV9y).
- Old search: `catalog.py:183-223`, `server.py:280-351`. New search: `site/index.html:268-271`.
- What was disclosed: the first version of `docs/plans/static-site-one-province.md`, section
  "Out of scope for this step".
- Decision log entry: `docs/6-decisions/Decisions.md`, 2026-09-28, "Pages site dropped features
  without saying so".
