#!/usr/bin/env bash
# The last-resort restarter for the hourly scrape chain (see
# docs/4-systems/hourly-trigger.md). GitHub's `schedule:` trigger is
# unreliable, so scrape-all.yml chains itself by dispatching its own next run
# -- but that chain has no restarter if it ever stops. This script is that
# restarter: it wakes up every 5 minutes (its own `schedule:` trigger, which
# is fine to miss occasionally since a duplicate wakeup just exits) and, if
# nobody else is already running this loop and the scrape chain looks stale,
# dispatches scrape-all.yml. Before it times out (GitHub caps a job at 6h) it
# dispatches its own successor so the loop never actually stops.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./chain_lib.sh
source "$HERE/chain_lib.sh"

WATCHDOG_FILE="scrape-watchdog.yml"
SCRAPE_ALL_FILE="scrape-all.yml"

WATCH_MINUTES="${WATCH_MINUTES:-330}"
POLL_SECONDS="${POLL_SECONDS:-600}"
STALE_MINUTES="${STALE_MINUTES:-70}"
SUCCESSOR_OF="${SUCCESSOR_OF:-}"
THIS_RUN_ID="${GITHUB_RUN_ID:?GITHUB_RUN_ID must be set}"

# Only one watchdog loop should run at a time. If another watchdog run is
# already active and it's not the one that spawned us, and it's older than
# us, step aside -- the older one wins so a burst of near-simultaneous cron
# fires collapses to a single loop instead of racing.
other_active="$(chain_lib_active_run_excluding "$WATCHDOG_FILE" "$THIS_RUN_ID")"
if [ -n "$other_active" ] && [ "$other_active" != "$SUCCESSOR_OF" ] && [ "$other_active" -lt "$THIS_RUN_ID" ]; then
  echo "Another watchdog run ($other_active) is already active and is older than us ($THIS_RUN_ID); exiting."
  exit 0
fi

echo "Watchdog run $THIS_RUN_ID starting (successor_of=${SUCCESSOR_OF:-<none>}). Watching for ${WATCH_MINUTES}m, polling every ${POLL_SECONDS}s."

start_epoch=$(date -u +%s)
end_epoch=$(( start_epoch + WATCH_MINUTES * 60 ))

first_pass=1
while :; do
  now_epoch=$(date -u +%s)
  # Always do at least one pass, even if WATCH_MINUTES is 0 (used by tests),
  # so a fresh watchdog checks the chain immediately rather than only ever
  # handing off to a successor.
  if [ "$first_pass" -ne 1 ] && [ "$now_epoch" -ge "$end_epoch" ]; then
    echo "Reached the ${WATCH_MINUTES}m watch window; handing off to a successor."
    break
  fi
  first_pass=0

  active="$(chain_lib_active_run_excluding "$SCRAPE_ALL_FILE" "")"
  age="$(chain_lib_newest_run_age_minutes "$SCRAPE_ALL_FILE")"

  if [ -z "$active" ] && { [ -z "$age" ] || [ "$age" -gt "$STALE_MINUTES" ]; }; then
    if [ -z "$age" ]; then
      echo "Scrape chain was down (no runs at all); restarted it."
    else
      echo "Scrape chain was down (newest run created ${age} min ago); restarted it."
    fi
    chain_lib_dispatch "$SCRAPE_ALL_FILE"
  else
    if [ -n "$active" ]; then
      echo "chain alive: run $active is active"
    else
      echo "chain alive: newest run created ${age} min ago (<= ${STALE_MINUTES}m threshold)"
    fi
  fi

  if [ "$POLL_SECONDS" -le 0 ]; then
    break
  fi
  sleep "$POLL_SECONDS"
done

echo "Dispatching successor watchdog (successor_of=$THIS_RUN_ID)."
chain_lib_dispatch "$WATCHDOG_FILE" -f "successor_of=$THIS_RUN_ID"
