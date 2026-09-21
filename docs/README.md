# CannaCabanaScraper — documentation

Start here. Everything in `docs/` is reachable from this page; nobody should have to search the
folder.

## What this is

A scraper and stock index for Canna Cabana's public storefront. It pulls comparable price and
stock data for every store in a province without paid APIs or accounts, keeps a history in
SQLite, and serves it two ways: a **desktop app** (a packaged Windows exe with a local web UI and
an optional public tunnel) and a **read-only GitHub Pages mirror** updated nightly by Actions.
A larger, not-yet-started plan would replace "ask the website" with a receiving-and-POS ledger
([Roadmap milestone 6](Roadmap.md#6-shipment-receiving--central-stock-ledger--not-started)).

## The moving parts

| Part | Where | What it is |
|---|---|---|
| The application | `CannaScraper/` | All the Python, the desktop web UI (`web/`), the static Pages frontend (`site/`), and the packaging scripts. **Its own nested git repository** — see [ProjectState.md](ProjectState.md#the-one-thing-that-is-not-what-it-looks-like) |
| Deployment plumbing | `.github/workflows/` | Five per-province scrape workflows, one shared build workflow, the Pages deploy, and a heartbeat |
| Documentation | `docs/` | This folder |
| Application-level plans | `CannaScraper/PLAN_*.md`, `CannaScraper/BUILD_INSTRUCTIONS.md` | Live in the application repo and are referenced in place, not copied here |

## The six tiers

| Tier | Document | Answers | Rewritten when |
|---|---|---|---|
| 1 | [README.md](README.md) (this page) | What is this, where is everything | The shape of the project changes |
| 2 | [Roadmap.md](Roadmap.md) | What 0–100% means; what "done" looks like | The *definition* of done changes — rare |
| 3 | [ProjectState.md](ProjectState.md) | Where it stands right now against the roadmap | A milestone's status changes |
| 4 | [systems/](systems/README.md) | How each runtime-critical system works | That system changes |
| 5 | [Today.md](Today.md) | What is being worked on today, and why that | Every working session |
| 6 | [Decisions.md](Decisions.md) | Why a decision was made, and what it replaced | Never rewritten — only appended to |

## The systems

Each system document covers what it owns, how it works, its invariants, and its traps. The
[index](systems/README.md) also lists what was considered and deliberately left out.

| System | Owns |
|---|---|
| [scraping-and-fetching](systems/scraping-and-fetching.md) | Getting one store's price/stock off the live site, via either backend |
| [rate-limiting-and-egress](systems/rate-limiting-and-egress.md) | Staying under the site's shared rate-limit budget; optional multi-route throughput |
| [data-store](systems/data-store.md) | The SQLite history: schema, writes, and every "what do we know now" read |
| [catalog-and-stores](systems/catalog-and-stores.md) | Product metadata cache, watchlist resolution, the store registry, nearest-store math |
| [indexing-pipeline](systems/indexing-pipeline.md) | Building a full-province stock snapshot durably and resumably |
| [web-server-and-jobs](systems/web-server-and-jobs.md) | The desktop app's HTTP API, the serialized job worker, the password gate |
| [desktop-packaging](systems/desktop-packaging.md) | Turning the source tree into a working, self-verifying, self-updating exe |
| [pages-deployment](systems/pages-deployment.md) | The GitHub Pages per-province mirror: nightly export, workflows, static frontend |
| [diagnostics](systems/diagnostics.md) | The regression-test harness and the historical one-off investigation scripts |

## Everything else

- **Plans, live until executed** — [plans/](plans/):
  [github-pages-per-province.md](plans/github-pages-per-province.md),
  [fix-search-and-build-staleness.md](plans/fix-search-and-build-staleness.md). Both have
  verification steps that have not been run.
- **Plans that live in the application repo** —
  [PLAN_app_distribution.md](../CannaScraper/PLAN_app_distribution.md),
  [PLAN_followups.md](../CannaScraper/PLAN_followups.md),
  [PLAN_receiving_ledger.md](../CannaScraper/PLAN_receiving_ledger.md),
  [BUILD_INSTRUCTIONS.md](../CannaScraper/BUILD_INSTRUCTIONS.md), and the application
  [README](../CannaScraper/README.md) (usage, the nightly `schtasks` job, verification recipes).
- **Archive** — [archive/](archive/README.md): documents that were correct and are now inert.
  Nothing there describes current behaviour.
- **Generated** — [generated/](generated/README.md): tool-produced reports and images. Not
  hand-edited; regenerated instead.

## Conventions

- **Cite claims to `file:line`.** Most assertions link to the code they describe, which is what
  makes an audit mechanical instead of a matter of opinion.
- **Say what a measurement cannot show.** A finding that states its own limits stops a dead end
  being reopened on a hunch.
- **When a decision reverses, fix the body and leave a pointer.** Rewrite the old text; record in
  [Decisions.md](Decisions.md) what it used to say and why it changed. No stacked "superseded"
  boxes above a table that still says the old thing.
- **Inert documents move to `archive/`; they are not deleted.** Move, then fix the pointers.
- **Update the tier that changed, not every tier.** Tier 3 moving is routine. Tier 2 moving means
  the definition of done moved, and is worth announcing.
- **`TODO` is honest; a plausible guess is not.** Where knowledge is missing, the document says so.
