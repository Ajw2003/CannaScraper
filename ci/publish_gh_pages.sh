#!/usr/bin/env bash
# Publish site/ plus province data to the gh-pages branch as one orphan commit.
#
#   ci/publish_gh_pages.sh REMOTE_URL [NEW_DATA_DIR]
#
# REMOTE_URL    where gh-pages lives (the workflows pass an authenticated URL)
# NEW_DATA_DIR  optional folder of data/<slug>.json files to add or replace;
#               omit it to republish the page over the existing data
#
# Several province runs can finish at once, and each rebuilds gh-pages from
# what it fetched. A plain force-push would let the last one erase the
# others' data, so the push is --force-with-lease against the commit this run
# started from: if another publish landed in between, the push is refused,
# and this run fetches again, re-applies its own files on top, and retries.
# gh-pages stays a single commit either way, so daily data never piles up in
# git history.

set -euo pipefail

REMOTE="${1:?usage: publish_gh_pages.sh REMOTE_URL [NEW_DATA_DIR]}"
NEW_DATA="${2:-}"
SITE_DIR="${SITE_DIR:-site}"
MESSAGE="${PUBLISH_MESSAGE:-Publish}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ -n "$NEW_DATA" ] && ! ls "$NEW_DATA"/*.json >/dev/null 2>&1; then
  echo "error: $NEW_DATA holds no .json files" >&2
  exit 1
fi

for attempt in 1 2 3 4 5 6; do
  rm -rf "$WORK/fetch" "$WORK/pub"
  mkdir -p "$WORK/pub/data"
  git init -q "$WORK/fetch"

  # ls-remote failing is a real error (network, auth); an empty answer just
  # means gh-pages does not exist yet.
  HEADS="$(git ls-remote --heads "$REMOTE" gh-pages)"
  if [ -n "$HEADS" ]; then
    # The lease is whatever commit this fetch returns: the state the new
    # commit is built from.
    git -C "$WORK/fetch" fetch -q --depth 1 "$REMOTE" gh-pages
    BASE="$(git -C "$WORK/fetch" rev-parse FETCH_HEAD)"
    if git -C "$WORK/fetch" ls-tree --name-only "$BASE" data/ | grep -q .; then
      git -C "$WORK/fetch" archive "$BASE" data | tar -x -C "$WORK/pub"
      KEPT="$(git -C "$WORK/fetch" ls-tree --name-only "$BASE" data/ | tr '\n' ' ')"
      echo "Kept from the current site: $KEPT"
    fi
  else
    BASE=""
    echo "No gh-pages branch yet: first publish."
  fi

  cp -r "$SITE_DIR"/. "$WORK/pub/"
  # Browsers may keep a Pages file for 10 minutes, so after an update a phone
  # could run the new page against the old script (seen 2026-09-28: the new
  # price menu showed, the member prices it needs did not). Naming the script
  # by its content makes any change to it a different URL.
  if [ -f "$WORK/pub/static-api.js" ]; then
    API_VER="$(sha256sum "$WORK/pub/static-api.js" | cut -c1-12)"
    sed -i "s|src=\"static-api.js\"|src=\"static-api.js?v=$API_VER\"|" "$WORK/pub/index.html"
    grep -q "static-api.js?v=$API_VER" "$WORK/pub/index.html" \
      || { echo "error: could not stamp static-api.js into index.html" >&2; exit 1; }
  fi
  if [ -n "$NEW_DATA" ]; then
    cp "$NEW_DATA"/*.json "$WORK/pub/data/"
  fi
  # Pages would otherwise run Jekyll over the files for no benefit.
  touch "$WORK/pub/.nojekyll"
  python3 "$HERE/build_manifest.py" "$WORK/pub/data"

  (
    cd "$WORK/pub"
    git init -q -b gh-pages
    git config user.name "github-actions[bot]"
    git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
    git add -A
    git commit -q -m "$MESSAGE"
  )

  # An empty expected value means "only if gh-pages still does not exist".
  if git -C "$WORK/pub" push -q --force-with-lease="gh-pages:$BASE" "$REMOTE" gh-pages 2>"$WORK/push.err"; then
    echo "Published gh-pages ($(git -C "$WORK/pub" ls-files | wc -l) files) on attempt $attempt."
    exit 0
  fi
  if ! grep -qi "stale info\|rejected\|fetch first" "$WORK/push.err"; then
    cat "$WORK/push.err" >&2
    echo "error: push to gh-pages failed for a reason other than a concurrent publish" >&2
    exit 1
  fi
  echo "attempt $attempt: another publish changed gh-pages first; refetching and retrying"
  sleep $((attempt * 3 + RANDOM % 5))
done

echo "error: gh-pages kept changing underneath this publish; gave up after 6 attempts" >&2
exit 1
