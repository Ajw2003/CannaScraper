#!/usr/bin/env bash
# Runs as scrape-all.yml's final job. GitHub's `schedule:` trigger is
# unreliable (see docs/4-systems/hourly-trigger.md), so instead of relying on
# it, each scrape-all run dispatches the next one itself, waiting until at
# least MIN_GAP_MINUTES after this run started so the chain still runs
# roughly hourly. It also makes sure the watchdog loop (the chain's own
# restarter) is alive, dispatching one if none is active.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./chain_lib.sh
source "$HERE/chain_lib.sh"

SCRAPE_ALL_FILE="scrape-all.yml"
WATCHDOG_FILE="scrape-watchdog.yml"
MIN_GAP_MINUTES="${MIN_GAP_MINUTES:-60}"
THIS_RUN_ID="${GITHUB_RUN_ID:?GITHUB_RUN_ID must be set}"

# Don't dispatch a next run if one is already queued/running/waiting --
# a manual dispatch or a retry could otherwise pile up parallel chains.
active="$(chain_lib_active_run_excluding "$SCRAPE_ALL_FILE" "$THIS_RUN_ID")"
if [ -n "$active" ]; then
  echo "Another $SCRAPE_ALL_FILE run ($active) is already queued/active; not dispatching another."
else
  started_at="$(gh api "repos/$GITHUB_REPOSITORY/actions/runs/$THIS_RUN_ID" | jq -r .run_started_at)"
  if [ -n "$started_at" ]; then
    started_epoch=$(date -u -d "$started_at" +%s)
    target_epoch=$(( started_epoch + MIN_GAP_MINUTES * 60 ))
    now_epoch=$(date -u +%s)
    remaining=$(( target_epoch - now_epoch ))
    if [ "$remaining" -gt 0 ]; then
      echo "Waiting ${remaining}s so the next run starts >= ${MIN_GAP_MINUTES}m after this one."
      sleep "$remaining"
    fi
  fi
  # Check again after the wait: a manual run, or one the watchdog started,
  # may have begun in the meantime, and dispatching now would start a second
  # chain alongside it.
  active="$(chain_lib_active_run_excluding "$SCRAPE_ALL_FILE" "$THIS_RUN_ID")"
  if [ -n "$active" ]; then
    echo "Run $active started while this one waited; not dispatching another."
  else
    chain_lib_dispatch "$SCRAPE_ALL_FILE"
  fi
fi

# The chain's restarter must itself be running, or a chain that stops (a
# dispatch failure, a run left hanging) never comes back.
watchdog_active="$(chain_lib_active_run_excluding "$WATCHDOG_FILE" "")"
if [ -n "$watchdog_active" ]; then
  echo "Watchdog run $watchdog_active is already queued/active."
else
  echo "No watchdog run active; dispatching one."
  chain_lib_dispatch "$WATCHDOG_FILE"
fi
