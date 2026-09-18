#!/usr/bin/env bash
set -Eeuo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/home/dev/.local/bin"

DEV_USER="dev"
DEV_HOME="/home/dev"
AUTH_TUNNEL_ROOT="${DEV_HOME}/.local/share/devcoveer/auth/tunnel-client"
PROFILE_DIR="${DEV_HOME}/.config/tunnel-client"
RUNTIME_KEY="${AUTH_TUNNEL_ROOT}/runtime-api-key"
USER_UNIT_DIR="${DEV_HOME}/.config/systemd/user"
BROWSER_PROFILE="${PROFILE_DIR}/my-browser-bridge.yaml"
BROWSER_UNIT="${USER_UNIT_DIR}/my-browser-bridge-tunnel.service"
MAIL_DIR="/etc/mail-mcp-bridge"
MAIL_PROFILE="${MAIL_DIR}/tunnel-client.yaml"
MAIL_UNIT="/etc/systemd/system/mail-mcp-bridge-tunnel.service"

[[ "$(id -u)" -eq 0 ]] || { echo "Run as root." >&2; exit 2; }
[[ -x "$DEV_HOME/.local/bin/tunnel-client" ]] || { echo "tunnel-client missing" >&2; exit 2; }
[[ -s "$RUNTIME_KEY" ]] || { echo "Runtime tunnel API key missing at $RUNTIME_KEY" >&2; exit 2; }

DEV_UID="$(id -u "$DEV_USER")"
DEV_RUNTIME="/run/user/${DEV_UID}"
USER_BUS="unix:path=${DEV_RUNTIME}/bus"

userctl() {
  runuser -u "$DEV_USER" -- env \
    HOME="$DEV_HOME" \
    XDG_RUNTIME_DIR="$DEV_RUNTIME" \
    DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
    systemctl --user "$@"
}

read_profile_id() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  awk '/^[[:space:]]*tunnel_id:[[:space:]]*/ {gsub(/["[:space:]]/,"",$2); print $2; exit}' "$file"
}

validate_id() {
  [[ "$1" =~ ^tunnel_[0-9a-f]{32}$ ]]
}

resolve_id() {
  local label="$1" env_name="$2" profile="$3" value=""
  value="${!env_name:-}"
  if [[ -z "$value" ]]; then
    value="$(read_profile_id "$profile" 2>/dev/null || true)"
  fi
  if [[ -z "$value" ]]; then
    if [[ -t 0 ]]; then
      read -r -p "$label tunnel ID (tunnel_...): " value
    else
      echo "Missing $label tunnel ID; set $env_name for non-interactive use." >&2
      return 1
    fi
  fi
  validate_id "$value" || { echo "Invalid tunnel ID for $label" >&2; return 1; }
  printf '%s' "$value"
}

echo "=== 1/6 RESOLVE EXISTING PLATFORM TUNNEL IDS ==="
BROWSER_ID="$(resolve_id 'DevCoveer browser-bridge' DEVCOVEER_BROWSER_BRIDGE_TUNNEL_ID "$BROWSER_PROFILE")"
MAIL_ID="$(resolve_id 'DevCoveer mailru' DEVCOVEER_MAILRU_TUNNEL_ID "$MAIL_PROFILE")"
echo "browser_bridge_tunnel_id=$BROWSER_ID"
echo "mailru_tunnel_id=$MAIL_ID"
echo "runtime_key=PRESENT"

echo
echo "=== 2/6 WRITE NAMED PROFILES ==="
install -d -m 0700 -o "$DEV_USER" -g "$DEV_USER" "$AUTH_TUNNEL_ROOT"
[[ -L "$PROFILE_DIR" || "$(readlink -f "$PROFILE_DIR" 2>/dev/null || true)" == "$AUTH_TUNNEL_ROOT" ]] || {
  echo "$PROFILE_DIR is not the canonical tunnel-client profile directory" >&2
  exit 20
}

cat >"$BROWSER_PROFILE" <<EOF_PROFILE
config_version: 1
control_plane:
  base_url: "https://api.openai.com"
  tunnel_id: "$BROWSER_ID"
  api_key: "file:$RUNTIME_KEY"
log:
  level: info
  format: json
health:
  listen_addr: "127.0.0.1:8081"
admin_ui:
  open_browser: false
mcp:
  server_urls:
    - channel: main
      url: "http://127.0.0.1:7311/mcp"
  startup_wait_timeout: 60s
  max_concurrent_requests: 10
EOF_PROFILE
chown "$DEV_USER:$DEV_USER" "$BROWSER_PROFILE"
chmod 0600 "$BROWSER_PROFILE"

# Mail keeps the repository's stronger dedicated system-user boundary. The
# runtime API key stays in the one canonical source file and systemd projects it
# into a transient credential file for this service; no second persistent key is made.
getent group mail-mcp-tunnel >/dev/null 2>&1 || groupadd --system mail-mcp-tunnel
if ! id mail-mcp-tunnel >/dev/null 2>&1; then
  useradd --system --gid mail-mcp-tunnel --home-dir /var/lib/mail-mcp-bridge-tunnel \
    --create-home --shell /usr/sbin/nologin mail-mcp-tunnel
fi
install -d -m 0750 -o root -g mail-mcp-tunnel "$MAIL_DIR"

cat >"$MAIL_PROFILE" <<EOF_PROFILE
config_version: 1
control_plane:
  base_url: "https://api.openai.com"
  tunnel_id: "$MAIL_ID"
  api_key: "file:/run/credentials/mail-mcp-bridge-tunnel.service/control_plane_api_key"
log:
  level: info
  format: json
health:
  listen_addr: "127.0.0.1:8082"
admin_ui:
  open_browser: false
mcp:
  server_urls:
    - channel: main
      url: "http://127.0.0.1:7312/mcp"
  extra_headers:
    Authorization: "env:MAIL_MCP_AUTHORIZATION_HEADER"
  startup_wait_timeout: 60s
  max_concurrent_requests: 10
EOF_PROFILE
chown root:mail-mcp-tunnel "$MAIL_PROFILE"
chmod 0640 "$MAIL_PROFILE"

# A system tunnel service cannot execute through ProtectHome=true from a binary
# stored below /home/dev. Install the already-verified binary itself system-wide;
# this copies code, never credentials or state.
install -o root -g root -m 0755 "$(readlink -f "$DEV_HOME/.local/bin/tunnel-client")" /usr/local/bin/tunnel-client

echo "browser_profile=$BROWSER_PROFILE"
echo "mail_profile=$MAIL_PROFILE"

echo
echo "=== 3/6 INSTALL TUNNEL SERVICES ==="
install -d -m 0755 -o "$DEV_USER" -g "$DEV_USER" "$USER_UNIT_DIR"
cat >"$BROWSER_UNIT" <<'EOF_UNIT'
[Unit]
Description=OpenAI Secure MCP Tunnel for my-browser-bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/dev
Environment=HOME=/home/dev
Environment=TUNNEL_CLIENT_PROFILE_DIR=/home/dev/.config/tunnel-client
ExecStart=/home/dev/.local/bin/tunnel-client run --profile my-browser-bridge
Restart=on-failure
RestartSec=5
TimeoutStartSec=30
TimeoutStopSec=30
KillSignal=SIGTERM
UMask=0077

[Install]
WantedBy=default.target
EOF_UNIT
chown "$DEV_USER:$DEV_USER" "$BROWSER_UNIT"
chmod 0644 "$BROWSER_UNIT"

cat >"$MAIL_UNIT" <<EOF_UNIT
[Unit]
Description=OpenAI Secure MCP Tunnel for Mail MCP Bridge
After=network-online.target mail-mcp-bridge.service
Wants=network-online.target
Requires=mail-mcp-bridge.service

[Service]
Type=simple
User=mail-mcp-tunnel
Group=mail-mcp-tunnel
WorkingDirectory=/var/lib/mail-mcp-bridge-tunnel
EnvironmentFile=/etc/mail-mcp-bridge/mail-mcp.env
LoadCredential=control_plane_api_key:$RUNTIME_KEY
ExecStart=/bin/sh -c 'export MAIL_MCP_AUTHORIZATION_HEADER="Bearer \$MAIL_MCP_BEARER_TOKEN"; exec /usr/local/bin/tunnel-client run --config /etc/mail-mcp-bridge/tunnel-client.yaml'
Restart=on-failure
RestartSec=5
TimeoutStartSec=30
TimeoutStopSec=30
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
ProcSubset=pid
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RuntimeDirectory=mail-mcp-bridge-tunnel
RuntimeDirectoryMode=0700
StateDirectory=mail-mcp-bridge-tunnel
StateDirectoryMode=0700

[Install]
WantedBy=multi-user.target
EOF_UNIT
chmod 0644 "$MAIL_UNIT"

userctl daemon-reload
systemctl daemon-reload
userctl enable my-browser-bridge-tunnel.service >/dev/null

echo
echo "=== 4/6 RESTORE CODEX DEVCOVEER TUNNEL ==="
userctl start codex-devserver-tunnel.service
codex_ready=0
for _ in $(seq 1 30); do
  if curl --connect-timeout 1 --max-time 3 -fsS http://127.0.0.1:18080/readyz >/dev/null 2>&1; then
    codex_ready=1
    break
  fi
  sleep 2
done
if [[ "$codex_ready" == 1 ]]; then
  echo "codex_tunnel=READY"
else
  echo "codex_tunnel=NOT_READY" >&2
  userctl --no-pager --full status codex-devserver-tunnel.service || true
  userctl --no-pager -n 60 status codex-devserver-tunnel.service || true
  exit 30
fi

echo
echo "=== 5/6 BROWSER + MAIL TUNNEL READINESS ==="
if curl --connect-timeout 1 --max-time 3 -fsS http://127.0.0.1:7311/healthz >/dev/null 2>&1; then
  if runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" TUNNEL_CLIENT_PROFILE_DIR="$PROFILE_DIR" \
      timeout 40 "$DEV_HOME/.local/bin/tunnel-client" doctor --profile my-browser-bridge --explain; then
    userctl start my-browser-bridge-tunnel.service
    browser_ready=0
    for _ in $(seq 1 20); do
      if curl --connect-timeout 1 --max-time 3 -fsS http://127.0.0.1:8081/readyz >/dev/null 2>&1; then
        browser_ready=1
        break
      fi
      sleep 2
    done
    [[ "$browser_ready" == 1 ]] && echo "browser_tunnel=READY" || echo "browser_tunnel=STARTED_NOT_READY"
  else
    echo "browser_tunnel=PROFILE_OR_REMOTE_NOT_READY"
  fi
else
  echo "browser_tunnel=STAGED_WAITING_FOR_BROWSER_BRIDGE_7311"
fi

mail_local=0
if curl --connect-timeout 1 --max-time 3 -fsS http://127.0.0.1:7312/healthz >/dev/null 2>&1; then
  mail_local=1
fi
if [[ "$mail_local" == 1 && -s /etc/mail-mcp-bridge/mail-mcp.env ]] && systemctl cat mail-mcp-bridge.service >/dev/null 2>&1; then
  systemctl enable mail-mcp-bridge-tunnel.service >/dev/null
  systemctl start mail-mcp-bridge-tunnel.service
  mail_ready=0
  for _ in $(seq 1 20); do
    if curl --connect-timeout 1 --max-time 3 -fsS http://127.0.0.1:8082/readyz >/dev/null 2>&1; then
      mail_ready=1
      break
    fi
    sleep 2
  done
  [[ "$mail_ready" == 1 ]] && echo "mail_tunnel=READY" || echo "mail_tunnel=STARTED_NOT_READY"
else
  echo "mail_tunnel=STAGED_WAITING_FOR_MAIL_MCP_7312_AND_CREDENTIALS"
fi

echo
echo "=== 6/6 FINAL TUNNEL INVENTORY ==="
echo "codex_service=$(userctl is-active codex-devserver-tunnel.service 2>/dev/null || true)"
echo "browser_service=$(userctl is-active my-browser-bridge-tunnel.service 2>/dev/null || true)"
echo "mail_service=$(systemctl is-active mail-mcp-bridge-tunnel.service 2>/dev/null || true)"
echo "profiles=DevCoveer_Codex,DevCoveer_browser-bridge,DevCoveer_mailru"
echo "MCP_TUNNEL_RECOVERY_COMPLETE"
