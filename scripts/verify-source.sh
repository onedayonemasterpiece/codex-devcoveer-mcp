#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_VERSION="${DEVCOVEER_TEST_PYTHON:-3.12.3}"

command -v uv >/dev/null 2>&1 || {
  echo "uv is required" >&2
  exit 2
}

uv run \
  --python "$PYTHON_VERSION" \
  --with-requirements requirements-test.txt \
  python -m pytest -q tests

python3 -m compileall -q runtime scripts tests
