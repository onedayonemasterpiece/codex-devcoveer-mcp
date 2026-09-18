#!/usr/bin/env bash
set -euo pipefail

[[ "$(id -u)" -ne 0 ]] || { echo "Run this as dev, not root." >&2; exit 2; }
source "$HOME/.config/devcoveer/auth-paths.sh"
export PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/.fly/bin:$HOME/.opencode/bin:$HOME/yandex-cloud/bin:$PATH"

echo "=== DEVCOvEER CLI LOGIN ==="

if command -v gh >/dev/null 2>&1; then
  if ! gh auth status --hostname github.com >/dev/null 2>&1; then
    echo "--- GitHub CLI login ---"
    gh auth login --hostname github.com --git-protocol https --web --insecure-storage
  fi
fi

if command -v flyctl >/dev/null 2>&1; then
  if ! flyctl auth whoami >/dev/null 2>&1; then
    echo "--- Fly CLI login ---"
    flyctl auth login
  fi
fi

if command -v yc >/dev/null 2>&1; then
  yc_profiles="$(yc config profile list --format json 2>/dev/null || true)"
  if ! python3 - "$yc_profiles" <<'PY' >/dev/null 2>&1
import json,sys
raw=sys.argv[1].strip()
data=json.loads(raw) if raw else []
raise SystemExit(0 if isinstance(data,list) and len(data)>0 else 1)
PY
  then
    echo "--- Yandex Cloud CLI init ---"
    yc init
  fi
fi

if command -v codex >/dev/null 2>&1; then
  if ! codex login status >/dev/null 2>&1; then
    echo "--- Codex CLI login ---"
    codex login
  fi
fi

echo
echo "=== LOGIN STATUS ==="
devcoveer-auth-status
