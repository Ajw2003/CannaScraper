#!/usr/bin/env bash
# Guards against web/index.html and site/index.html drifting apart: the
# static Pages site (site/index.html) is a copy of the desktop page
# (web/index.html) with a small, deliberate set of edits (see
# docs/plans/restore-original-page.md). If a PR changes web/index.html but
# not site/index.html, the Pages copy silently falls behind. See:
# docs/4-systems/ci-checks.md, "pages-in-step".
#
#   ci/check_pages_in_step.sh BASE_REF
#
# BASE_REF is the ref to diff HEAD against (e.g. "origin/main"). Requires a
# checkout with fetch-depth: 0 so BASE_REF is actually reachable.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: check_pages_in_step.sh BASE_REF" >&2
  exit 2
fi
BASE_REF="$1"

CHANGED="$(git diff --name-only "${BASE_REF}...HEAD")"

if echo "$CHANGED" | grep -qx 'web/index.html' && ! echo "$CHANGED" | grep -qx 'site/index.html'; then
  echo "::error file=web/index.html::web/index.html changed but site/index.html did not. site/index.html is the Pages copy of this page and must get the same change (see docs/plans/restore-original-page.md)."
  exit 1
fi

echo "OK: web/index.html and site/index.html are in step (or neither changed)."
