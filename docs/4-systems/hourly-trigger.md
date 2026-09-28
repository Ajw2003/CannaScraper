# Hourly trigger: the scrape chain and its watchdog

## What runs the scrape hourly

Not GitHub's `schedule:` trigger. It is unreliable in this repo (evidence below), so hourly
runs happen through two things that keep each other alive instead:

1. **The scrape chain.** `scrape-all.yml`'s final job (`next`, `ci/scrape_chain_next.sh`) runs
   after every scrape, pass or fail, and dispatches the next `scrape-all.yml` run on `main` via
   `workflow_dispatch` -- waiting until at least 60 minutes (`MIN_GAP_MINUTES`) after this run
   started, and skipping the dispatch if another run is already queued/in progress/waiting. Each
   run also makes sure a watchdog loop is active, dispatching one if not.
2. **The watchdog loop.** `scrape-watchdog.yml` ("Keep the hourly scrape going",
   `ci/scrape_watchdog.sh`) runs a single long job (up to ~5.5h, under GitHub's 6h job cap) that
   polls every `POLL_SECONDS` (default 10 min). Each pass, if no `scrape-all.yml` run is
   active and the newest one was created more than `STALE_MINUTES` (default 70) minutes ago --
   or there are no runs at all -- it dispatches `scrape-all.yml` itself, restarting the chain.
   Before its window ends it dispatches its own successor watchdog run
   (`workflow_dispatch` with `successor_of=$GITHUB_RUN_ID`), so the loop never actually stops.
   Only one loop runs at a time: a new watchdog run steps aside if an older one is already
   active, unless it is that older run's own successor.

The watchdog's `schedule:` cron (`2-57/5 * * * *`, i.e. every 5 minutes, off the hour) is the
only `schedule:` trigger left in this system. It only has to succeed often enough to notice "no
watchdog loop is running" and start one -- it does not have to fire reliably itself, because a
missed or duplicate 5-minute wakeup just means a loop starts a few minutes later, or a wakeup
that finds a loop already running exits in seconds.

**How the two revive each other:** if the scrape chain stops (a dispatch fails, a run hangs),
the watchdog notices within one poll and restarts it. If the watchdog loop itself dies (its job
is killed, its dispatch chain breaks), the next scrape-all run's `next` job notices no watchdog
is active and starts a new one. Either side coming back brings the other back too.

## Evidence that `schedule:` is unreliable here

- GitHub's own documentation: scheduled workflows "can be delayed during periods of high loads
  of GitHub Actions workflow runs... during those periods, some queued jobs may be dropped."
  It also documents that events triggered using the repository's `GITHUB_TOKEN` will not create
  a new workflow run, *except* `workflow_dispatch` and `repository_dispatch`, which always do --
  which is why the chain uses `workflow_dispatch` rather than relying on any event.
- Observed in this repo: the hourly `scrape-all.yml` schedule fired once (09:59 UTC on
  2026-09-28, run 36406897565) out of roughly 12 hourly slots it should have hit.
- The `schedule-probe.yml` diagnostic (`*/5 * * * *`, GitHub's minimum interval) fired **zero**
  times in the 26 minutes after PR #10 merged at 15:43 UTC, despite running every 5 minutes.
- Others have hit the same thing: a community report of a `*/5 * * * *` job firing only ~5% of
  the time: https://github.com/orgs/community/discussions/156282

## Cost

The watchdog keeps one GitHub-hosted runner busy continuously (a single long-running job, not
one job per poll). That's free for public repos, and it counts as one job toward the account's
20-concurrent-jobs limit alongside the up-to-5 province jobs `scrape-all.yml` can run at once,
so it does not meaningfully compete with them.

## Stopping it

1. Actions tab -> disable both **"Keep the hourly scrape going"** and **"Scrape all
   provinces"**.
2. Cancel any runs of either that are currently in progress (a disabled workflow does not stop
   a run already in flight, and an in-flight watchdog run would otherwise keep polling until it
   times out).

## Restarting it

Actions tab -> **"Keep the hourly scrape going"** -> Run workflow (leave `successor_of` blank).
The watchdog's first pass will find the scrape chain stale (or absent) and dispatch
`scrape-all.yml` itself, and re-enabling `scrape-all.yml` if disabled makes its own `next` job
resume the chain from then on.
