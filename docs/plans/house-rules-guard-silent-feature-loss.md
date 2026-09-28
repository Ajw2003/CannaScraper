# Guard: a replacement must not silently drop features

Input for a new guard in the house-rules plugin. The observation comes first, because the guard
should be judged against it: any proposed rule or hook should have stopped this specific case.

Status: **observation recorded, guard not built.** Written 2026-09-28.

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

## Shape of the failure (what a guard should recognise)

- A **new artifact replaces an existing one's role**: a new page, module, service, CLI or
  workflow; a port to another platform or framework; a "v2"; a migration. The old one keeps
  existing, or is retired later, so no file is ever "rewritten".
- The replacement is **judged by whether it works**, not by whether it does **everything the
  original did**.
- **Forced losses are named, unforced ones are not.** Naming the forced ones makes the
  disclosure look complete.
- **Delegation multiplies it.** A spec written without reading the original hands the gap to an
  agent that has no way to notice it.

## Proposed guard

### Rule text (for `rules/house-rules.md`, with detail in `rules/detail/`)

> **A replacement is a rewrite, even as a new file.** Before building something that takes over
> an existing thing's job (a port, a migration, a v2, a new page or service), inventory what the
> original does from its code, not from memory: every user-visible feature and behaviour. Show the
> user the inventory marked keep / change / drop, with the reason for each drop. Verify the
> replacement against the original with the same inputs, not only against itself. A dropped
> feature the user wasn't shown is a regression, even if the new thing works.

Also widen `edit-place.md`'s rule to name this case explicitly, so the two rules point at each
other.

### Mechanical checks (hook ideas, in rough order of value)

1. **Parity inventory required before a replacement plan is approved.** When a plan or
   `ExitPlanMode` text contains replacement language ("port", "migrate", "replace", "move to",
   "rewrite", "v2", "static version of", "instead of the old"), require a
   `docs/plans/<name>-parity.md` with a keep / change / drop table before execution is delegated.
   The table's rows should cite the original's code (`file:line`), which shows it was read.
2. **Delegation prompt check.** When an `Agent` / executor prompt describes building something that
   replaces an existing file or feature, flag it unless the prompt either includes the parity
   table or instructs the agent to read the original and inventory it first.
3. **Parity verification before claiming done.** For a replacement, the completion report must
   include at least one like-for-like comparison (same input to old and new, outputs compared).
   The evidence-before-claims check could require a quoted old-vs-new result, not just a
   new-only one.
4. **Feature-surface diff as a cheap tripwire.** For UI replacements, list the controls in both
   files (`<select>`, `<button>`, `<input>`, `<option>` labels, fetch endpoints) and report the
   ones present in the old and absent in the new. For this case it would have flagged, among
   others: `#top` (5/10/25 nearest), `#sort`, `#src` (live check), `#near` (city/postal code),
   `#stocked` (in-stock-only toggle), `#toggle` ("Show N without it"). That's not proof of
   loss, but it's a list to account for. Checked on 2026-09-28: comparing element IDs alone,

       comm -23 <(grep -o 'id="[a-z]*"' web/index.html | sort -u) \
                <(grep -o 'id="[a-z]*"' site/index.html | sort -u)

   lists 23 IDs only in the old page, including all six above plus the live-check (`refresh`,
   `prog`) and admin (`idxpanel`, `unlock`, `pw`) controls. It's crude (it can't see a changed
   search rule or a demoted price), which is why it's a tripwire and not the guard.

### How to test the guard

Replay this case: the old `web/index.html`, `server.py` and `catalog.py`, and a request to "serve
the page through GitHub Pages". The guard passes if, before any code is written, the user sees
an inventory that includes at least the search-coverage change (title+brand vs.
title/brand/category/size), the price-emphasis change, the unstocked-products toggle, city/postal
location, nearest-N, and store sort, each marked keep / change / drop.

## Evidence

- Comparison and screenshots: `docs/generated/pages-audit/index.html` (also published at
  https://claude.ai/artifact/KkdAM1nhkJxsu9oHP1LV9y).
- Old search: `catalog.py:183-223`, `server.py:280-351`. New search: `site/index.html:268-271`.
- What was disclosed: the first version of `docs/plans/static-site-one-province.md`, section
  "Out of scope for this step".
- Decision log entry: `docs/6-decisions/Decisions.md`, 2026-09-28, "Pages site dropped features
  without saying so".
