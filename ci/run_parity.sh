#!/usr/bin/env bash
# Builds the fixtures the static site's parity checks need, starts the old
# server and the static site, runs ci/parity_check.py and
# ci/live_parity_check.py against them, and always stops the servers.
#
# Shared between the "parity" CI job and local runs, so both do exactly the
# same thing. See docs/4-systems/ci-checks.md, "parity".
#
#   ci/run_parity.sh WORKDIR
#
# WORKDIR is a scratch directory this script owns (created if missing).
# Layout it builds inside WORKDIR:
#   appdata/          CANNACABANA_DATA for the old server: history.db,
#                      catalog.json, stores.json, geocode.json
#   export/            ci/export_province.py + ci/export_catalog.py output
#   served/            site/ + export/ laid out the way the static site
#                      expects, served over plain HTTP
#
# Env:
#   HISTORY_DB      Path to a history-<slug>.db already on disk. If unset,
#                    this downloads it with `gh release download` (needs
#                    GH_TOKEN / gh auth). Set this to skip the network -- for
#                    example when GitHub releases are unreachable locally.
#   PROVINCE         Default: Saskatchewan
#   OLD_PORT         Default: 8950
#   STATIC_PORT       Default: 8951
#   GITHUB_REPOSITORY Needed only when HISTORY_DB is unset (gh release download).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

WORKDIR="${1:?usage: run_parity.sh WORKDIR}"
PROVINCE="${PROVINCE:-Saskatchewan}"
OLD_PORT="${OLD_PORT:-8950}"
STATIC_PORT="${STATIC_PORT:-8951}"
SLUG="$(echo "$PROVINCE" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')"

APPDATA="$WORKDIR/appdata"
EXPORT="$WORKDIR/export"
SERVED="$WORKDIR/served"
mkdir -p "$APPDATA" "$EXPORT" "$SERVED/data"

# ---- 1. fixture DB --------------------------------------------------------
if [ -n "${HISTORY_DB:-}" ]; then
  echo "Using HISTORY_DB=$HISTORY_DB (no download)."
  cp "$HISTORY_DB" "$APPDATA/history.db"
else
  : "${GITHUB_REPOSITORY:?set GITHUB_REPOSITORY or HISTORY_DB}"
  echo "Downloading history-$SLUG.db from the scrape-history release..."
  gh release download scrape-history -p "history-$SLUG.db" -R "$GITHUB_REPOSITORY" \
    --dir "$APPDATA" --clobber
  mv "$APPDATA/history-$SLUG.db" "$APPDATA/history.db"
fi
cp "$ROOT/catalog.json" "$APPDATA/catalog.json"
cp "$ROOT/stores.json" "$APPDATA/stores.json"
cp "$ROOT/geocode.json" "$APPDATA/geocode.json"

# ---- 2. exports ------------------------------------------------------------
# The old server's /api/index/status reports the DB's own most recent index
# run for this province (db.index_runs), independent of anything we make up
# here -- so run.json must name that same real run_id, or /api/index/status
# mismatches between old and static (last_run, last_run_stores, incomplete).
python3 -c "
import json, sys
sys.path.insert(0, '$ROOT')
import db
conn = db.connect('$APPDATA/history.db')
runs = db.index_runs(conn, '$PROVINCE', limit=1)
if not runs:
    print('::error::no index run found for $PROVINCE in $APPDATA/history.db', file=sys.stderr)
    sys.exit(1)
r = runs[0]
json.dump({'run_id': r['run_id'], 'stores': r['stores'], 'failed': 0},
          open('$WORKDIR/run.json', 'w'))
"
python3 "$HERE/export_province.py" --province "$PROVINCE" --db "$APPDATA/history.db" \
  --out "$EXPORT" --run-summary "$WORKDIR/run.json"
# No --refresh: catalog.json was just copied (mtime = now), so it is never
# stale and this must not touch the network. See catalog.catalog_is_stale().
python3 "$HERE/export_catalog.py" --out "$EXPORT"
cp -f "$ROOT/stores.json" "$EXPORT/stores.json"
cp -f "$ROOT/geocode.json" "$EXPORT/geocode.json"
python3 "$HERE/build_manifest.py" "$EXPORT"

cp -r "$ROOT/site/." "$SERVED/"
cp "$EXPORT"/*.json "$SERVED/data/"

# ---- 3. shape check (fast, no servers needed) ------------------------------
python3 "$HERE/check_export_shape.py" "$EXPORT" --province "$PROVINCE"

# ---- 4. servers -------------------------------------------------------------
OLD_PID=""
STATIC_PID=""
cleanup() {
  local status=$?
  if [ -n "$OLD_PID" ]; then kill "$OLD_PID" 2>/dev/null || true; fi
  if [ -n "$STATIC_PID" ]; then kill "$STATIC_PID" 2>/dev/null || true; fi
  wait "$OLD_PID" 2>/dev/null || true
  wait "$STATIC_PID" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT INT TERM

echo "Starting old server on :$OLD_PORT ..."
CANNACABANA_DATA="$APPDATA" python3 -m uvicorn server:app --app-dir "$ROOT" \
  --port "$OLD_PORT" --host 127.0.0.1 >"$WORKDIR/old-server.log" 2>&1 &
OLD_PID=$!

echo "Starting static site server on :$STATIC_PORT ..."
python3 -m http.server "$STATIC_PORT" --directory "$SERVED" --bind 127.0.0.1 \
  >"$WORKDIR/static-server.log" 2>&1 &
STATIC_PID=$!

wait_ready() {
  local url="$1" name="$2"
  for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then
      echo "$name is ready."
      return 0
    fi
    sleep 1
  done
  echo "::error::$name at $url never became ready" >&2
  return 1
}
wait_ready "http://127.0.0.1:$OLD_PORT/api/provinces" "old server"
wait_ready "http://127.0.0.1:$STATIC_PORT/index.html" "static site server"

# ---- 5. checks --------------------------------------------------------------
echo "Running ci/parity_check.py ..."
python3 "$HERE/parity_check.py" --old "http://127.0.0.1:$OLD_PORT" \
  --static "http://127.0.0.1:$STATIC_PORT"

echo "Running ci/live_parity_check.py ..."
python3 "$HERE/live_parity_check.py" --data "$APPDATA" \
  --static "http://127.0.0.1:$STATIC_PORT"

echo "run_parity.sh: all checks passed."
