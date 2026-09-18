#!/usr/bin/env bash
set -euo pipefail

[[ "$(id -u)" -ne 0 ]] || { echo "Run this as dev, not root." >&2; exit 2; }

if [[ -f "$HOME/.config/devcoveer/auth-paths.sh" ]]; then
  . "$HOME/.config/devcoveer/auth-paths.sh"
else
  echo "Missing canonical auth-path contract. Run bootstrap-devcoveer2-auth-home.sh as root first." >&2
  exit 2
fi

export PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/.fly/bin:$HOME/.opencode/bin:$HOME/yandex-cloud/bin:$PATH"

echo "=== LOGIN ONLY WHERE MISSING ==="

if command -v gh >/dev/null 2>&1; then
  if ! gh auth status --hostname github.com >/dev/null 2>&1; then
    echo "--- GitHub CLI ---"
    gh auth login --hostname github.com --git-protocol https --web --insecure-storage
    gh auth setup-git --hostname github.com >/dev/null 2>&1 || true
  fi
fi

if command -v flyctl >/dev/null 2>&1; then
  if ! flyctl auth whoami >/dev/null 2>&1; then
    echo "--- Fly CLI ---"
    flyctl auth login
  fi
fi

if command -v yc >/dev/null 2>&1; then
  if ! timeout 30 yc iam create-token --no-user-output >/dev/null 2>&1; then
    echo "--- Yandex Cloud CLI ---"
    echo "yc init will persist into the canonical ~/.config/yandex-cloud symlink."
    yc init --no-browser
  fi
fi

if command -v codex >/dev/null 2>&1; then
  if ! codex login status >/dev/null 2>&1; then
    echo "--- Codex CLI ---"
    codex login --device-auth
  fi
fi

# OpenCode persists provider login/OAuth in ~/.local/share/opencode/auth.json.
# Do not force login when BYOK keys intentionally come from /home/dev/.env.
if command -v opencode >/dev/null 2>&1; then
  if [[ ! -s "$HOME/.local/share/opencode/auth.json" ]]; then
    echo "--- OpenCode ---"
    echo "No native OpenCode auth.json. This is OK for providers supplied by /home/dev/.env."
    echo "Use: opencode auth login   only for a provider that specifically requires OpenCode OAuth/login."
  fi
fi

if command -v kaggle >/dev/null 2>&1; then
  if [[ ! -s "$KAGGLE_CONFIG_DIR/access_token" && ! -s "$KAGGLE_CONFIG_DIR/kaggle.json" ]]; then
    echo "--- Kaggle ---"
    echo "No native Kaggle login file. Central Kaggle credentials may instead come from /home/dev/.env."
  fi
fi

echo
echo "=== RESULT ==="
"$HOME/.local/bin/devcoveer-auth-status"
