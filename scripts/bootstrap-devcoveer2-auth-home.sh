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
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$DEV_HOME/.local/bin:$DEV_HOME/.fly/bin:$DEV_HOME/.opencode/bin:$DEV_HOME/yandex-cloud/bin"

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

fly_identity_from_dir() {
  local config_dir="$1"
  [[ -f "$config_dir/config.yml" ]] || return 1
  runuser -u "$DEV_USER" -- env -u FLY_API_TOKEN -u FLY_ACCESS_TOKEN \
    HOME="$DEV_HOME" \
    FLY_CONFIG_DIR="$config_dir" \
    PATH="$DEV_HOME/.fly/bin:/usr/local/bin:/usr/bin:/bin:$DEV_HOME/.local/bin" \
    timeout 30 flyctl auth whoami 2>/dev/null \
    | tr -d '\r\n'
}

reconcile_fly_config() {
  local native="$DEV_HOME/.fly/config.yml"
  local target="$AUTH_HOME/fly/config.yml"
  local target_dir
  target_dir="$(dirname "$target")"

  install -d -m 0700 -o "$DEV_USER" -g "$DEV_USER" "$target_dir"
  install -d -m 0755 -o "$DEV_USER" -g "$DEV_USER" "$(dirname "$native")"

  same_target "$native" "$target" && return 0
  [[ ! -L "$native" ]] || fail "unexpected Fly symlink $native -> $(readlink "$native")"

  if [[ ! -e "$native" || ! -e "$target" ]]; then
    adopt_file "$native" "$target"
    return 0
  fi

  [[ -f "$native" && -f "$target" ]] || fail "Fly config paths are not regular files"

  if cmp -s "$native" "$target"; then
    rm -f "$native"
    ln -s "$target" "$native"
    chown -h "$DEV_USER:$DEV_USER" "$native"
    chown "$DEV_USER:$DEV_USER" "$target"
    chmod 0600 "$target"
    return 0
  fi

  local native_identity="" target_identity=""
  native_identity="$(fly_identity_from_dir "$DEV_HOME/.fly" || true)"
  target_identity="$(fly_identity_from_dir "$AUTH_HOME/fly" || true)"

  if [[ -n "$native_identity" && -n "$target_identity" && "$native_identity" != "$target_identity" ]]; then
    fail "native and canonical Fly configs authenticate as different accounts; refusing to choose"
  fi

  local backup_dir="$AUTH_HOME/fly/conflicts"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  install -d -m 0700 -o "$DEV_USER" -g "$DEV_USER" "$backup_dir"

  if [[ -n "$target_identity" && -z "$native_identity" ]]; then
    mv "$native" "$backup_dir/native-config-$stamp.yml"
    echo "fly_config_reconcile=kept_canonical_valid archived_native"
  else
    mv "$target" "$backup_dir/canonical-config-$stamp.yml"
    mv "$native" "$target"
    if [[ -n "$native_identity" ]]; then
      echo "fly_config_reconcile=kept_native_valid archived_canonical"
    else
      echo "fly_config_reconcile=no_valid_session_detected kept_native archived_canonical"
    fi
  fi

  ln -s "$target" "$native"
  chown -h "$DEV_USER:$DEV_USER" "$native"
  chown -R "$DEV_USER:$DEV_USER" "$target_dir"
  chmod 0600 "$target"
}

echo "=== CANONICAL AUTH HOME ==="
echo "auth_home=$AUTH_HOME"

GH_ACTIVE_TOKEN=""
if command -v gh >/dev/null 2>&1; then
  GH_ACTIVE_TOKEN="$(runuser -u "$DEV_USER" -- env -u GH_TOKEN -u GITHUB_TOKEN HOME="$DEV_HOME" gh auth token --hostname github.com 2>/dev/null || true)"
fi

# Native CLI auth/session stores -> one canonical tree.
adopt_dir "$DEV_HOME/.config/gh" "$AUTH_HOME/gh"
reconcile_fly_config
adopt_dir "$DEV_HOME/.config/yandex-cloud" "$AUTH_HOME/yc"
# Codex/OpenCode perform state writes and refresh OAuth tokens. Move the whole
# state directories so every process mutates one underlying store.
adopt_dir "$DEV_HOME/.codex" "$AUTH_HOME/codex"
adopt_dir "$DEV_HOME/.local/share/opencode" "$AUTH_HOME/opencode"
adopt_dir "$DEV_HOME/.kaggle" "$AUTH_HOME/kaggle"
adopt_dir "$DEV_HOME/.docker" "$AUTH_HOME/docker"
adopt_dir "$DEV_HOME/.aws" "$AUTH_HOME/aws"
adopt_dir "$DEV_HOME/.supabase" "$AUTH_HOME/supabase"
# OpenAI tunnel client auth + tunnel configuration/state.
adopt_dir "$DEV_HOME/.config/tunnel-client" "$AUTH_HOME/tunnel-client"

# GitHub CLI may otherwise keep the token in a desktop keyring. On this headless
# host force the active account into the canonical 0600 GH_CONFIG_DIR so agents,
# shells and git credential-helper calls resolve the exact same persisted login.
if [[ -n "$GH_ACTIVE_TOKEN" ]] && command -v gh >/dev/null 2>&1; then
  printf '%s\n' "$GH_ACTIVE_TOKEN" | runuser -u "$DEV_USER" -- env     -u GH_TOKEN -u GITHUB_TOKEN     HOME="$DEV_HOME" GH_CONFIG_DIR="$AUTH_HOME/gh"     gh auth login --hostname github.com --with-token --insecure-storage >/dev/null
  GH_ACTIVE_TOKEN=""
fi

cat >"$AUTH_ENV" <<EOF
# Non-secret canonical auth paths.
export DEVCOVEER_AUTH_HOME="$AUTH_HOME"
export GH_CONFIG_DIR="$AUTH_HOME/gh"
export FLY_CONFIG_DIR="$AUTH_HOME/fly"
export KAGGLE_CONFIG_DIR="$AUTH_HOME/kaggle"
export DOCKER_CONFIG="$AUTH_HOME/docker"
export AWS_SHARED_CREDENTIALS_FILE="$AUTH_HOME/aws/credentials"
export AWS_CONFIG_FILE="$AUTH_HOME/aws/config"
export CODEX_HOME="$AUTH_HOME/codex"
for _d in "\$HOME/.local/bin" "\$HOME/.fly/bin" "\$HOME/.opencode/bin" "\$HOME/yandex-cloud/bin"; do
  case ":\$PATH:" in *":\$_d:"*) ;; *) PATH="\$_d:\$PATH" ;; esac
done
export PATH
unset _d
EOF
chown "$DEV_USER:$DEV_USER" "$AUTH_ENV"; chmod 0644 "$AUTH_ENV"

cat >"$SYSTEMD_ENV" <<EOF
DEVCOVEER_AUTH_HOME=$AUTH_HOME
GH_CONFIG_DIR=$AUTH_HOME/gh
FLY_CONFIG_DIR=$AUTH_HOME/fly
KAGGLE_CONFIG_DIR=$AUTH_HOME/kaggle
DOCKER_CONFIG=$AUTH_HOME/docker
AWS_SHARED_CREDENTIALS_FILE=$AUTH_HOME/aws/credentials
AWS_CONFIG_FILE=$AUTH_HOME/aws/config
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

# Long-lived user services read the exact same canonical auth-path contract as
# interactive shells. This avoids service-vs-terminal session drift after reboot
# or when a service is restarted by an agent.
for unit in opencode-devcoveer.service codex-devserver-tunnel.service; do
  dropin_dir="$DEV_HOME/.config/systemd/user/$unit.d"
  install -d -m 0755 -o "$DEV_USER" -g "$DEV_USER" "$dropin_dir"
  cat >"$dropin_dir/20-devcoveer-auth.conf" <<EOF
[Service]
EnvironmentFile=-$SYSTEMD_ENV
EOF
  chown "$DEV_USER:$DEV_USER" "$dropin_dir/20-devcoveer-auth.conf"
  chmod 0644 "$dropin_dir/20-devcoveer-auth.conf"
done
unset unit dropin_dir

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
    "KAGGLE_CONFIG_DIR=$AUTH_HOME/kaggle" "DOCKER_CONFIG=$AUTH_HOME/docker" \
    "AWS_SHARED_CREDENTIALS_FILE=$AUTH_HOME/aws/credentials" "AWS_CONFIG_FILE=$AUTH_HOME/aws/config" \
    "CODEX_HOME=$AUTH_HOME/codex"
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
status github env -u GH_TOKEN -u GITHUB_TOKEN timeout 30 gh auth status --hostname github.com
status fly env -u FLY_API_TOKEN -u FLY_ACCESS_TOKEN timeout 30 flyctl auth whoami
status yc env -u YC_TOKEN -u YC_IAM_TOKEN -u YANDEX_IAM_TOKEN timeout 30 yc iam create-token --no-user-output
status codex env -u OPENAI_API_KEY -u CODEX_API_KEY timeout 30 codex login status
if [[ -s "$HOME/.local/share/opencode/auth.json" ]]; then
  printf "%-14s NATIVE_AUTH_PRESENT\n" opencode
elif [[ -s "$HOME/.config/opencode/devcoveer-providers.env" ]] && grep -Eq '^[A-Z][A-Z0-9_]*=' "$HOME/.config/opencode/devcoveer-providers.env"; then
  printf "%-14s BYOK_PROVIDER_ENV\n" opencode
else
  printf "%-14s LOGIN_OR_PROVIDER_KEY_REQUIRED\n" opencode
fi
if command -v aws >/dev/null 2>&1; then status aws timeout 30 aws sts get-caller-identity; else printf "%-14s NOT_INSTALLED\n" aws; fi
if command -v supabase >/dev/null 2>&1; then status supabase timeout 30 supabase projects list; else printf "%-14s NOT_INSTALLED\n" supabase; fi
if [[ -s "$KAGGLE_CONFIG_DIR/access_token" || -s "$KAGGLE_CONFIG_DIR/kaggle.json" ]]; then printf "%-14s PASS\n" kaggle; else printf "%-14s OWNER_ENV_OR_LOGIN_REQUIRED\n" kaggle; fi
for spec in \
  "gh:$HOME/.config/gh" "fly:$HOME/.fly/config.yml" "yc:$HOME/.config/yandex-cloud" \
  "codex:$HOME/.codex" "opencode:$HOME/.local/share/opencode" "kaggle:$HOME/.kaggle" \
  "aws:$HOME/.aws" "supabase:$HOME/.supabase" "docker:$HOME/.docker" \
  "tunnel:$HOME/.config/tunnel-client"; do
  name="${spec%%:*}"; path="${spec#*:}"
  if [[ -L "$path" ]]; then printf "link:%-9s %s -> %s\n" "$name" "$path" "$(readlink "$path")"; else printf "link:%-9s MISSING_OR_NOT_SYMLINK %s\n" "$name" "$path"; fi
done
EOF
chmod 0755 "$DEV_HOME/.local/bin/devcoveer-auth-status"; chown "$DEV_USER:$DEV_USER" "$DEV_HOME/.local/bin/devcoveer-auth-status"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/devcoveer2-login-missing-clis.sh" ]]; then
  ln -sfn "$SCRIPT_DIR/devcoveer2-login-missing-clis.sh" "$DEV_HOME/.local/bin/devcoveer-login-missing-clis"
  chown -h "$DEV_USER:$DEV_USER" "$DEV_HOME/.local/bin/devcoveer-login-missing-clis"
fi

echo
echo "=== AUTH HOME READY ==="
echo "canonical=$AUTH_HOME"
echo "paths=$AUTH_ENV"
echo "status_command=$DEV_HOME/.local/bin/devcoveer-auth-status"
