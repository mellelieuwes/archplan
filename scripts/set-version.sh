#!/usr/bin/env bash
set -euo pipefail

# Write a version (YY.M.BUILD, no leading v) into every file that carries it.

VERSION=${1:?usage: set-version.sh YY.M.BUILD}
if ! [[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "not a version: $VERSION" >&2
  exit 1
fi

cd "$(dirname "$0")/.."
python3 - "$VERSION" <<'PY'
import re, sys
v = sys.argv[1]
for path, pat, new in [
    ("pyproject.toml", r'^version = "[^"]*"', f'version = "{v}"'),
    ("src/archplan/__init__.py", r'^__version__ = "[^"]*"', f'__version__ = "{v}"'),
    (".claude-plugin/plugin.json", r'^  "version": "[^"]*"', f'  "version": "{v}"'),
]:
    s = open(path).read()
    s2, n = re.subn(pat, new, s, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"{path}: no version found")
    open(path, "w").write(s2)
PY
