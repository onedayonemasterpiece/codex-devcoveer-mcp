#!/usr/bin/env bash
set -euo pipefail

DEV_USER="dev"
DEV_HOME="/home/dev"
DEV_UID="$(id -u "$DEV_USER")"
AUTH_HOME="$DEV_HOME/.local/share/devcoveer/auth"
AUTH_ENV="$DEV_HOME/.config/devcoveer/auth-paths.sh"
SYSTEMD_ENV="$DEV_HOME/.config/environment.d/60-devcoveer-auth.conf"
USER_RUNTIME_DIR="/run/user/$DEV_UID"
USER_BUS="unix:path=$USER_RUNTIME_DIR/bus"

[[ "$(id -u)" -eq 0 ]] || { echo "Run as root." >&2; exit 2; }

install -d -m 0700 -o "$DEV_USER" -g "$DEV_USER" "$AUTH_HOME"
install -d -m 0755 -o "$DEV_USER" -g "$DEV_USER" "$DEV_HOME/.config/devcoveer" "$DEV_HOME/.config/environment.d" "$DEV_HOME/.local/bin"

user_systemctl() {
  runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" XDG_RUNTIME_DIR="$USER_RUNTIME_DIR" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" "$@"
}

WAS_OPENCODE=0
WAS_TUNNEL=0
if [[ -S "$USER_RUNTIME_DIR/bus" ]]; then
  if user_systemctl systemctl --user is-active --quiet opencode-devcoveer.service; then
    WAS_OPENCODE=1
    user_systemctl systemctl --user stop opencode-devcoveer.service
  fi
  if user_systemctl systemctl --user is-active --quiet codex-devserver-tunnel.service; then
    WAS_TUNNEL=1
    user_systemctl systemctl --user stop codex-devserver-tunnel.service
  fi
fi

restore_stopped_services() {
  if [[ -S "$USER_RUNTIME_DIR/bus" ]]; then
    if [[ "$WAS_OPENCODE" == 1 ]]; then
      user_systemctl systemctl --user start opencode-devcoveer.service || true
    fi
    if [[ "$WAS_TUNNEL" == 1 ]]; then
      user_systemctl systemctl --user start codex-devserver-tunnel.service || true
    fi
  fi
}
trap restore_stopped_services EXIT

fail() { echo "AUTH_HOME_BLOCKED: $*" >&2; exit 1; }
dir_empty() { [[ ! -d "$1" || -z "$(find "$1" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; }
same_target() {
  [[ -L "$1" ]] || return 1
  local raw resolved
  raw="$(readlink "$1")"
  if [[ "$raw" = /* ]]; then
    resolved="$(readlink -m "$raw")"
  else
    resolved="$(readlink -m "$(dirname "$1")/$raw")"
  fi
  [[ "$resolved" == "$(readlink -m "$2")" ]]
}

adopt_dir() {
  local native="$1" target="$2"
  install -d -m 0700 -o "$DEV_USER" -g "$DEV_USER" "$(dirname "$target")"
  install -d -m 0755 -o "$DEV_USER" -g "$DEV_USER" "$(dirname "$native")"
  same_target "$native" "$target" && return 0
  [[ ! -L "$native" ]] || fail "unexpected symlink $native -> $(readlink "$native")"
  if [[ -e "$native" ]]; then
    [[ -d "$native" ]] || fail "$native exists but is not a directory"
    if [[ -e "$target" ]]; then
      dir_empty "$target" || fail "both $native and $target contain state; refusing to guess"
      rmdir "$target"
    fi
    mv "$native" "$target"
  else
    install -d -m 0700 -o "$DEV_USER" -g "$DEV_USER" "$target"
  fi
  ln -s "$target" "$native"
  chown -h "$DEV_USER:$DEV_USER" "$native"
  chown -R "$DEV_USER:$DEV_USER" "$target"
  find "$target" -type d -exec chmod 0700 {} +
}

adopt_file() {
  local native="$1" target="$2"
  install -d -m 0700 -o "$DEV_USER" -g "$DEV_USER" "$(dirname "$target")"
  install -d -m 0755 -o "$DEV_USER" -g "$DEV_USER" "$(dirname "$native")"
  same_target "$native" "$target" && return 0
  [[ ! -L "$native" ]] || fail "unexpected symlink $native -> $(readlink "$native")"
  if [[ -f "$native" ]]; then
    if [[ -f "$target" ]]; then
      cmp -s "$native" "$target" || fail "different state exists at $native and $target"
      rm -f "$native"
    else
      mv "$native" "$target"
    fi
  elif [[ -e "$native" ]]; then
    fail "$native exists but is not a regular file"
  fi
  ln -s "$target" "$native"
  chown -h "$DEV_USER:$DEV_USER" "$native"
  if [[ -f "$target" ]]; then chown "$DEV_USER:$DEV_USER" "$target"; chmod 0600 "$target"; fi
}

echo "=== CANONICAL AUTH HOME ==="
echo "auth_home=$AUTH_HOME"

# Native CLI auth/session stores -> one canonical tree.
adopt_dir "$DEV_HOME/.config/gh" "$AUTH_HOME/gh"
adopt_file "$DEV_HOME/.fly/config.yml" "$AUTH_HOME/fly/config.yml"
adopt_dir "$DEV_HOME/.config/yandex-cloud" "$AUTH_HOME/yc"
# Codex/OpenCode perform state writes and refresh OAuth tokens. Move the whole
# state directories so every process mutates one underlying store.
adopt_dir "$DEV_HOME/.codex" "$AUTH_HOME/codex"
adopt_dir "$DEV_HOME/.local/share/opencode" "$AUTH_HOME/opencode"
adopt_dir "$DEV_HOME/.kaggle" "$AUTH_HOME/kaggle"
adopt_dir "$DEV_HOME/.docker" "$AUTH_HOME/docker"
install -d -m 0700 -o "$DEV_USER" -g "$DEV_USER" "$DEV_HOME/.config/tunnel-client"
adopt_file "$DEV_HOME/.config/tunnel-client/runtime-api-key" "$AUTH_HOME/tunnel-client/runtime-api-key"

cat >"$AUTH_ENV" <<EOF
# Non-secret canonical auth paths.
export DEVCOVEER_AUTH_HOME="$AUTH_HOME"
export GH_CONFIG_DIR="$AUTH_HOME/gh"
export FLY_CONFIG_DIR="$AUTH_HOME/fly"
export KAGGLE_CONFIG_DIR="$AUTH_HOME/kaggle"
export DOCKER_CONFIG="$AUTH_HOME/docker"
export CODEX_HOME="$AUTH_HOME/codex"
EOF
chown "$DEV_USER:$DEV_USER" "$AUTH_ENV"; chmod 0644 "$AUTH_ENV"

cat >"$SYSTEMD_ENV" <<EOF
DEVCOVEER_AUTH_HOME=$AUTH_HOME
GH_CONFIG_DIR=$AUTH_HOME/gh
FLY_CONFIG_DIR=$AUTH_HOME/fly
KAGGLE_CONFIG_DIR=$AUTH_HOME/kaggle
DOCKER_CONFIG=$AUTH_HOME/docker
CODEX_HOME=$AUTH_HOME/codex
EOF
chown "$DEV_USER:$DEV_USER" "$SYSTEMD_ENV"; chmod 0644 "$SYSTEMD_ENV"

ensure_shell_source() {
  local rc="$1"
  touch "$rc"; chown "$DEV_USER:$DEV_USER" "$rc"
  if ! grep -Fq '. "$HOME/.config/devcoveer/auth-paths.sh"' "$rc"; then
    cat >>"$rc" <<'EOF'

# DevCoveer canonical CLI auth/session locations.
if [ -f "$HOME/.config/devcoveer/auth-paths.sh" ]; then
  . "$HOME/.config/devcoveer/auth-paths.sh"
fi
EOF
  fi
}
ensure_shell_source "$DEV_HOME/.profile"
ensure_shell_source "$DEV_HOME/.bashrc"

# Codex must use one file-backed credential store. Copying auth.json is forbidden;
# OAuth refresh tokens must be refreshed in the same underlying file.
runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" python3 - <<'PY'
from pathlib import Path
p = Path("/home/dev/.local/share/devcoveer/auth/codex/config.toml")
text = p.read_text(encoding="utf-8") if p.exists() else ""
wanted = {
    "cli_auth_credentials_store": 'cli_auth_credentials_store = "file"',
    "mcp_oauth_credentials_store": 'mcp_oauth_credentials_store = "file"',
}
seen = set()
out = []
for line in text.splitlines():
    stripped = line.strip()
    hit = None
    for key in wanted:
        if stripped.startswith(key + " ") or stripped.startswith(key + "="):
            hit = key
            break
    if hit is None:
        out.append(line)
    elif hit not in seen:
        out.append(wanted[hit]); seen.add(hit)
if out and out[-1] != "": out.append("")
for key, value in wanted.items():
    if key not in seen: out.append(value)
p.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
p.chmod(0o600)
PY

if [[ -S "$USER_RUNTIME_DIR/bus" ]]; then
  user_systemctl systemctl --user set-environment \
    "DEVCOVEER_AUTH_HOME=$AUTH_HOME" "GH_CONFIG_DIR=$AUTH_HOME/gh" "FLY_CONFIG_DIR=$AUTH_HOME/fly" \
    "KAGGLE_CONFIG_DIR=$AUTH_HOME/kaggle" "DOCKER_CONFIG=$AUTH_HOME/docker" "CODEX_HOME=$AUTH_HOME/codex"
  user_systemctl systemctl --user daemon-reload
  if [[ "$WAS_OPENCODE" == 1 ]]; then
    user_systemctl systemctl --user start opencode-devcoveer.service
    for _ in $(seq 1 30); do
      user_systemctl systemctl --user is-active --quiet opencode-devcoveer.service && break
      sleep 1
    done
    user_systemctl systemctl --user is-active --quiet opencode-devcoveer.service || fail "opencode-devcoveer.service did not return active"
    WAS_OPENCODE=0
  fi
  if [[ "$WAS_TUNNEL" == 1 ]]; then
    user_systemctl systemctl --user start codex-devserver-tunnel.service
    for _ in $(seq 1 30); do
      user_systemctl systemctl --user is-active --quiet codex-devserver-tunnel.service && break
      sleep 1
    done
    user_systemctl systemctl --user is-active --quiet codex-devserver-tunnel.service || fail "codex-devserver-tunnel.service did not return active"
    WAS_TUNNEL=0
  fi
fi

if command -v gh >/dev/null 2>&1; then
  if runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" GH_CONFIG_DIR="$AUTH_HOME/gh" gh auth status --hostname github.com >/dev/null 2>&1; then
    # On headless servers force gh to file-backed storage in the canonical tree.
    # If the token currently lives in a keyring, retrieve it without printing it
    # and immediately re-store the same token in hosts.yml.
    if ! grep -Eq '^[[:space:]]*oauth_token:' "$AUTH_HOME/gh/hosts.yml" 2>/dev/null; then
      runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" GH_CONFIG_DIR="$AUTH_HOME/gh"         bash -lc 'set -o pipefail; gh auth token --hostname github.com | gh auth login --hostname github.com --with-token --insecure-storage >/dev/null'
    fi
    runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" GH_CONFIG_DIR="$AUTH_HOME/gh" gh auth setup-git --hostname github.com >/dev/null 2>&1 || true
  fi
fi

cat >"$DEV_HOME/.local/bin/devcoveer-auth-status" <<'EOF'
#!/usr/bin/env bash
set -u
source "$HOME/.config/devcoveer/auth-paths.sh"
status() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then printf "%-14s PASS\n" "$name"; else printf "%-14s LOGIN_REQUIRED_OR_UNAVAILABLE\n" "$name"; fi; }
echo "auth_home=$DEVCOVEER_AUTH_HOME"
status github gh auth status --hostname github.com
status fly flyctl auth whoami
status yc yc config profile list
status codex codex login status
status opencode opencode auth list
if [[ -s "$KAGGLE_CONFIG_DIR/access_token" || -s "$KAGGLE_CONFIG_DIR/kaggle.json" ]]; then printf "%-14s PASS\n" kaggle; else printf "%-14s OWNER_ENV_OR_LOGIN_REQUIRED\n" kaggle; fi
for spec in \
  "gh:$HOME/.config/gh" "fly:$HOME/.fly/config.yml" "yc:$HOME/.config/yandex-cloud" \
  "codex:$HOME/.codex" "opencode:$HOME/.local/share/opencode" "kaggle:$HOME/.kaggle"; do
  name="${spec%%:*}"; path="${spec#*:}"
  if [[ -L "$path" ]]; then printf "link:%-9s %s -> %s\n" "$name" "$path" "$(readlink "$path")"; else printf "link:%-9s MISSING_OR_NOT_SYMLINK %s\n" "$name" "$path"; fi
done
EOF
chmod 0755 "$DEV_HOME/.local/bin/devcoveer-auth-status"; chown "$DEV_USER:$DEV_USER" "$DEV_HOME/.local/bin/devcoveer-auth-status"

echo
echo "=== AUTH HOME READY ==="
echo "canonical=$AUTH_HOME"
echo "paths=$AUTH_ENV"
echo "status_command=$DEV_HOME/.local/bin/devcoveer-auth-status"
