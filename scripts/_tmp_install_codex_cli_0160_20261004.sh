#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED="codex-cli 0.160.0"
BIN="/home/dev/.local/bin/codex"

if [[ -x "$BIN" ]] && [[ "$("$BIN" --version)" == "$EXPECTED" ]]; then
  "$BIN" app-server --help >/dev/null
  printf 'codex_version=%s\n' "$("$BIN" --version)"
  printf 'app_server=PASS\n'
  exit 0
fi

PATH="/usr/local/bin:/usr/bin:/bin:/home/dev/.local/bin" npm install --global --prefix /home/dev/.local --no-audit --no-fund '@openai/codex@0.160.0'

actual="$("$BIN" --version)"
if [[ "$actual" != "$EXPECTED" ]]; then
  printf 'expected=%s actual=%s\n' "$EXPECTED" "$actual" >&2
  exit 3
fi
"$BIN" app-server --help >/dev/null
printf 'codex_version=%s\n' "$actual"
printf 'app_server=PASS\n'
