#!/usr/bin/env bash
# Names static-api.js by its content in DIR/index.html, in place:
#   <script src="static-api.js">  ->  <script src="static-api.js?v=<12 hex>">
#
#   ci/stamp_page.sh DIR
#
# Browsers may keep a Pages file for 10 minutes, so after an update a phone
# could run the new page against the old script (seen 2026-09-28: the new
# price menu showed, the member prices and sorting it needs did not). A
# changed script is then always a new URL. Used by publish_gh_pages.sh when
# publishing and by publish-site.yml to know what the live page should be.
set -euo pipefail

DIR="${1:?usage: stamp_page.sh DIR}"
[ -f "$DIR/static-api.js" ] || exit 0
API_VER="$(sha256sum "$DIR/static-api.js" | cut -c1-12)"
sed -i "s|src=\"static-api.js\"|src=\"static-api.js?v=$API_VER\"|" "$DIR/index.html"
grep -q "static-api.js?v=$API_VER" "$DIR/index.html" \
  || { echo "error: could not stamp static-api.js into $DIR/index.html" >&2; exit 1; }
