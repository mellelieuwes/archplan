#!/usr/bin/env bash
set -euo pipefail

# Print the next CalVer release version, without the leading v.
#
# Format: YY.M.BUILD, the same scheme as rela (see docs/releasing.md):
#   YY     two-digit UTC year
#   M      month, no leading zero (semver and PEP 440 forbid leading zeros)
#   BUILD  0 for the month's first release, then 1, 2, ...
#
#   26.10.0   first release of October 2026
#   26.10.1   second release that month
#   26.11.0   first release of November

YEAR=$(date -u +%y)
MONTH=$(date -u +%-m)
BASE="${YEAR}.${MONTH}"

# A failed fetch must not restart the counter at 0 over an existing tag.
git fetch --tags --quiet 2>/dev/null || true

HIGHEST=$(git tag -l "v${BASE}.*" \
  | sed -nE "s/^v${BASE//./\\.}\.([0-9]+)$/\1/p" \
  | sort -n \
  | tail -1 || true)

if [ -z "$HIGHEST" ]; then
  echo "${BASE}.0"
else
  echo "${BASE}.$((HIGHEST + 1))"
fi
