#!/usr/bin/env bash
# Lays out ROOT the way ci/pages_ux_check.py expects (ROOT/CannaScraper/ holding
# the current site/ files plus the published data/ from gh-pages) and runs it.
#
# Shared between the "page-additions" CI job and local runs. See
# docs/4-systems/ci-checks.md, "page-additions".
#
#   ci/run_pages_ux.sh WORKDIR
#
# WORKDIR is a scratch directory this script owns (created if missing).
# Requires `git fetch origin gh-pages` to have already been run (or to be
# reachable) so `git archive origin/gh-pages` resolves.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT_REPO="$(cd "$HERE/.." && pwd)"

WORKDIR="${1:?usage: run_pages_ux.sh WORKDIR}"
SITE_ROOT="$WORKDIR/site-root"
LAYOUT="$SITE_ROOT/CannaScraper"
SHOTS="$WORKDIR/shots"
rm -rf "$SITE_ROOT" "$SHOTS"
mkdir -p "$LAYOUT" "$SHOTS"

git -C "$ROOT_REPO" fetch -q origin gh-pages
git -C "$ROOT_REPO" archive origin/gh-pages | tar -x -C "$LAYOUT"
# The current branch's site/ files (not gh-pages') answer the page, so a PR
# that touches site/ is tested against its own changes, with gh-pages only
# supplying the published data/ the page reads at runtime.
cp -f "$ROOT_REPO"/site/*.html "$ROOT_REPO"/site/*.js "$LAYOUT/" 2>/dev/null || true

python3 "$HERE/pages_ux_check.py" "$SITE_ROOT" "$SHOTS"
