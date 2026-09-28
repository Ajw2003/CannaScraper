#!/usr/bin/env bash
# Shared helpers for the scrape chain and its watchdog: checking whether a
# workflow has an active run, and dispatching a workflow with retries so a
# single flaky API call doesn't drop the chain.
#
# Sourced, not executed: `source "$(dirname "$0")/chain_lib.sh"`.
# Expects GH_TOKEN and GITHUB_REPOSITORY in the environment (workflows set
# these already); scripts that source this must `set -euo pipefail` too.

ACTIVE_STATUSES='["queued","in_progress","waiting","pending","requested"]'

# runs_json WORKFLOW_FILE -> prints the raw JSON of that workflow's recent runs.
chain_lib_runs_json() {
  local workflow_file="$1"
  gh api "repos/$GITHUB_REPOSITORY/actions/workflows/$workflow_file/runs?per_page=30"
}

# chain_lib_active_run_excluding WORKFLOW_FILE EXCLUDE_RUN_ID
# Prints the id of the newest active run of WORKFLOW_FILE other than
# EXCLUDE_RUN_ID (empty string to exclude nothing), or nothing if there is none.
chain_lib_active_run_excluding() {
  local workflow_file="$1" exclude_id="$2"
  chain_lib_runs_json "$workflow_file" | jq -r --argjson active "$ACTIVE_STATUSES" \
    --arg exclude "$exclude_id" '
    [.workflow_runs[] | select(($exclude == "" or (.id | tostring) != $exclude))
      | select(.status as $s | $active | index($s) != null)]
    | sort_by(.id) | reverse | .[0].id // empty'
}

# chain_lib_newest_run_age_minutes WORKFLOW_FILE
# Prints how many minutes ago the newest run of WORKFLOW_FILE was created, or
# nothing if there are no runs at all.
chain_lib_newest_run_age_minutes() {
  local workflow_file="$1" created
  created=$(chain_lib_runs_json "$workflow_file" | jq -r '
    [.workflow_runs[]] | sort_by(.id) | reverse | .[0].created_at // empty')
  if [ -z "$created" ]; then
    return 0
  fi
  local now_epoch created_epoch
  now_epoch=$(date -u +%s)
  created_epoch=$(date -u -d "$created" +%s)
  echo $(( (now_epoch - created_epoch) / 60 ))
}

# chain_lib_dispatch WORKFLOW_FILE [-f key=value ...]
# Dispatches WORKFLOW_FILE on main, retrying with backoff. Fails loudly and
# exits non-zero if every attempt fails.
chain_lib_dispatch() {
  local workflow_file="$1"
  shift
  local base="${RETRY_BASE_SECONDS:-5}"
  local attempt delay
  for attempt in 1 2 3 4; do
    if gh workflow run "$workflow_file" -R "$GITHUB_REPOSITORY" --ref main "$@"; then
      echo "Dispatched $workflow_file (attempt $attempt)."
      return 0
    fi
    delay=$((base * (2 ** (attempt - 1))))
    echo "attempt $attempt: dispatch of $workflow_file failed; retrying in ${delay}s"
    sleep "$delay"
  done
  echo "::error::dispatch of $workflow_file failed after 4 attempts"
  return 1
}
