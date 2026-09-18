#!/usr/bin/env bash
# DevCoveer2 emergency control-plane bootstrap.
#
# Goal: regain ChatGPT -> Codex DevCoveer MCP -> OpenCode control first.
# This intentionally does NOT restore product runtimes such as VibePublish,
# Browser Bridge, Dataset Loop, mail, VPN, or Telegram proxy. Once this control
# plane is reachable again, those should be restored through DevCoveer itself.
#
# The temporary MCP runtime is reconstructed from the credential-free 3.6.1
# baseline committed in my-data-hub. It is installed in a separate bootstrap
# directory and must be upgraded to the current validated codex-devcoveer-mcp
# lineage after remote control has been re-established.
set -Eeuo pipefail

DEV=dev
H=/home/dev
P="$H/projects"

CODEX_VERSION="0.151.0-alpha.7.2"
OPENCODE_VERSION="1.18.15"
TUNNEL_VERSION="v0.0.14"
TUNNEL_ID="${CONTROL_PLANE_TUNNEL_ID:-tunnel_6a8afbe7d1c08191a9bf0769ccba7077}"
TUNNEL_ASSET="tunnel-client-${TUNNEL_VERSION}-linux-amd64.zip"
TUNNEL_BASE="https://github.com/openai/tunnel-client/releases/download/${TUNNEL_VERSION}"

BASE_REPO="$P/my-data-hub"
BASE_BRANCH="feat/devcoveer-opencode-dataset-loop-20260901"
BASE_SHA="c15688a9f16df9d9acf4478ebe854993c03b2679"
BASE_PATH="integrations/devcoveer/openai-codex-mcp/baseline"

BOOT_RUNTIME="$H/.local/libexec/openai-codex-mcp-bootstrap"
STATE_ROOT="$H/.local/share/openai-codex-mcp"
VENV="$STATE_ROOT/bridge-venv"
PUBLIC_ENTRY="$H/.local/bin/codex-mcp-server"
CONF="$H/.config/tunnel-client"
KEY_FILE="$CONF/runtime-api-key"
USER_UNIT_DIR="$H/.config/systemd/user"
UNIT="$USER_UNIT_DIR/codex-devserver-tunnel.service"
HEALTH="http://127.0.0.1:18080"
LOG="/var/log/devcoveer2-control-plane-bootstrap.log"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run this recovery bootstrap as root." >&2
  exit 2
fi

exec > >(tee -a "$LOG") 2>&1

asdev() {
  runuser -u "$DEV" -- env \
    HOME="$H" \
    PATH="/usr/local/bin:/usr/bin:/bin:$H/.local/bin:$H/.opencode/bin" \
    "$@"
}

devsh() {
  runuser -u "$DEV" -- env \
    HOME="$H" \
    PATH="/usr/local/bin:/usr/bin:/bin:$H/.local/bin:$H/.opencode/bin" \
    bash -lc "$1"
}

UID_DEV="$(id -u "$DEV")"
USER_ENV=(
  "HOME=$H"
  "XDG_RUNTIME_DIR=/run/user/$UID_DEV"
  "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UID_DEV/bus"
  "PATH=/usr/local/bin:/usr/bin:/bin:$H/.local/bin:$H/.opencode/bin"
)

userctl() {
  timeout 20s runuser -u "$DEV" -- env "${USER_ENV[@]}" systemctl --user "$@"
}

echo "============================================================"
echo " DevCoveer2 control-plane bootstrap"
echo " $(date -Is)"
echo "============================================================"

echo
echo "== 1. user systemd =="
loginctl enable-linger "$DEV" || true
systemctl start "user@${UID_DEV}.service" || true

install -d -o dev -g dev -m 0755 "$H/.local/bin" "$H/.local/libexec"
install -d -o dev -g dev -m 0700 "$H/.config/openai-codex-mcp" "$CONF" "$USER_UNIT_DIR" "$STATE_ROOT"

echo
echo "== 2. OpenCode must already be healthy =="
OC_BIN="$H/.opencode/bin/opencode"
OC_ENV="$H/.config/openai-codex-mcp/opencode-server.env"

if [[ ! -x "$OC_BIN" || ! -s "$OC_ENV" ]]; then
  echo "ERROR: OpenCode binary/auth config is missing; this bootstrap expects the already-restored localhost OpenCode service." >&2
  exit 10
fi

set -a
# shellcheck disable=SC1090
. "$OC_ENV"
set +a

OC_JSON="$(curl --connect-timeout 2 --max-time 8 -fsS \
  -u "$OPENCODE_SERVER_USERNAME:$OPENCODE_SERVER_PASSWORD" \
  http://127.0.0.1:4097/global/health)" || {
    echo "ERROR: OpenCode localhost health failed." >&2
    exit 11
}

python3 - "$OC_JSON" "$OPENCODE_VERSION" <<'PY'
import json, sys
payload=json.loads(sys.argv[1])
expected=sys.argv[2]
if payload.get("healthy") is not True:
    raise SystemExit("OpenCode is not healthy")
print("OpenCode health: PASS; version=" + str(payload.get("version")))
if payload.get("version") != expected:
    print("WARNING: expected OpenCode %s, observed %s" % (expected, payload.get("version")))
PY

unset OPENCODE_SERVER_USERNAME OPENCODE_SERVER_PASSWORD

echo
echo "== 3. pinned Codex CLI =="
if [[ -x "$H/.local/bin/codex" ]] && "$H/.local/bin/codex" --version 2>/dev/null | grep -Fq "$CODEX_VERSION"; then
  echo "Codex already installed: $("$H/.local/bin/codex" --version)"
else
  echo "Installing @openai/codex@$CODEX_VERSION into the dev user prefix..."
  asdev npm install --global --prefix "$H/.local" --no-audit --no-fund "@openai/codex@$CODEX_VERSION"
fi

if [[ -x "$H/.local/bin/codex" ]]; then
  echo "Codex: $(asdev "$H/.local/bin/codex" --version 2>/dev/null || true)"
  if asdev "$H/.local/bin/codex" app-server --help >/dev/null 2>&1; then
    echo "Codex app-server capability: PASS"
  else
    echo "WARNING: Codex app-server probe failed; OpenCode takeover path can still be used."
  fi
  if asdev "$H/.local/bin/codex" login status >/dev/null 2>&1; then
    echo "Codex auth: PRESENT"
  else
    echo "Codex auth: LOGIN_REQUIRED (not a blocker for explicit OpenCode tasks)"
  fi
else
  echo "WARNING: Codex CLI is still unavailable; continuing with OpenCode-only takeover path."
fi

echo
echo "== 4. temporary credential-free MCP bootstrap runtime =="
if [[ ! -d "$BASE_REPO/.git" ]]; then
  echo "ERROR: $BASE_REPO is not available." >&2
  exit 20
fi

# Ensure the historical credential-free baseline commit is reachable locally.
if ! asdev git -C "$BASE_REPO" cat-file -e "$BASE_SHA^{commit}" 2>/dev/null; then
  asdev git -C "$BASE_REPO" fetch origin "$BASE_BRANCH" --no-tags
fi
asdev git -C "$BASE_REPO" cat-file -e "$BASE_SHA^{commit}"

rm -rf "$BOOT_RUNTIME.new"
install -d -o dev -g dev -m 0700 "$BOOT_RUNTIME.new"

materialize() {
  local rel="$1"
  local out="$2"
  asdev git -C "$BASE_REPO" show "$BASE_SHA:$BASE_PATH/$rel" >"$out"
  chown dev:dev "$out"
  chmod 0600 "$out"
}

materialize "codex-chatgpt-bridge.py" "$BOOT_RUNTIME.new/codex-chatgpt-bridge.py"
materialize "opencode_backend.py" "$BOOT_RUNTIME.new/opencode_backend.py"

# Verify against the immutable credential-free manifest recorded in
# codex-devcoveer-mcp/historical/3.6.1/BASELINE_MANIFEST.json.
printf '%s  %s\n' \
  "73756c81b852cb183943c880d5918faadeee25ea9b034504a80df9f5f05d2107" \
  "$BOOT_RUNTIME.new/codex-chatgpt-bridge.py" \
  "56699a39b2d635cdc397774cc25f122b119e7794599b0d50776c7596e6c9af84" \
  "$BOOT_RUNTIME.new/opencode_backend.py" \
  | sha256sum -c -

# Keep bootstrap separate from any partially reconstructed canonical runtime.
if [[ -d "$BOOT_RUNTIME" ]]; then
  rm -rf "$BOOT_RUNTIME.previous"
  mv "$BOOT_RUNTIME" "$BOOT_RUNTIME.previous"
fi
mv "$BOOT_RUNTIME.new" "$BOOT_RUNTIME"
chown -R dev:dev "$BOOT_RUNTIME"

echo
echo "== 5. bridge Python environment =="
if [[ ! -x "$VENV/bin/python" ]]; then
  rm -rf "$VENV"
  if command -v uv >/dev/null 2>&1; then
    asdev uv venv --python 3.12 "$VENV"
  else
    asdev python3 -m venv "$VENV"
  fi
fi

if command -v uv >/dev/null 2>&1; then
  asdev uv pip install --python "$VENV/bin/python" \
    "mcp==2.0.0" "PyYAML>=6,<7" "jsonschema>=4,<5" "anyio>=4,<5"
else
  asdev "$VENV/bin/python" -m pip install \
    "mcp==2.0.0" "PyYAML>=6,<7" "jsonschema>=4,<5" "anyio>=4,<5"
fi

asdev "$VENV/bin/python" -m py_compile \
  "$BOOT_RUNTIME/codex-chatgpt-bridge.py" \
  "$BOOT_RUNTIME/opencode_backend.py"

echo
echo "== 6. public stdio MCP entrypoint =="
if [[ -e "$PUBLIC_ENTRY" && ! -e "$PUBLIC_ENTRY.pre-devcoveer2-bootstrap" ]]; then
  cp -a "$PUBLIC_ENTRY" "$PUBLIC_ENTRY.pre-devcoveer2-bootstrap"
fi

cat >"$PUBLIC_ENTRY" <<'EOF'
#!/bin/sh
export HOME=/home/dev
export CODEX_HOME=/home/dev/.codex
# Never pass control-plane/provider credentials into the MCP child.
unset CONTROL_PLANE_API_KEY OPENAI_API_KEY CODEX_API_KEY OPENAI_ADMIN_KEY
exec /home/dev/.local/share/openai-codex-mcp/bridge-venv/bin/python \
  /home/dev/.local/libexec/openai-codex-mcp-bootstrap/codex-chatgpt-bridge.py "$@"
EOF
chown dev:dev "$PUBLIC_ENTRY"
chmod 0755 "$PUBLIC_ENTRY"

echo
echo "== 7. official OpenAI tunnel-client $TUNNEL_VERSION =="
TC_DIR="$H/.local/libexec/tunnel-client-${TUNNEL_VERSION#v}"
TC_BIN="$TC_DIR/tunnel-client"

if [[ ! -x "$TC_BIN" ]] || ! "$TC_BIN" --version 2>/dev/null | grep -Fq "${TUNNEL_VERSION#v}"; then
  tmp="$(mktemp -d /tmp/tunnel-client-bootstrap.XXXXXX)"
  trap 'rm -rf "${tmp:-}"' EXIT

  curl -fL --retry 3 --retry-delay 2 \
    "$TUNNEL_BASE/$TUNNEL_ASSET" -o "$tmp/$TUNNEL_ASSET"
  curl -fL --retry 3 --retry-delay 2 \
    "$TUNNEL_BASE/SHA256SUMS.txt" -o "$tmp/SHA256SUMS.txt"

  (
    cd "$tmp"
    grep -F "  $TUNNEL_ASSET" SHA256SUMS.txt | sha256sum -c -
  )

  python3 - "$tmp/$TUNNEL_ASSET" "$tmp/unpacked" <<'PY'
import sys, zipfile
from pathlib import Path
archive=Path(sys.argv[1]); dest=Path(sys.argv[2]); dest.mkdir()
with zipfile.ZipFile(archive) as z:
    z.extractall(dest)
PY

  rm -rf "$TC_DIR.new"
  install -d -o dev -g dev -m 0755 "$TC_DIR.new"

  candidate="$(find "$tmp/unpacked" -type f -name 'tunnel-client' -print -quit)"
  if [[ -z "$candidate" ]]; then
    candidate="$(find "$tmp/unpacked" -type f -name 'tunnel-client-runtime*' ! -name '*.json' ! -name '*.txt' -print -quit)"
  fi
  [[ -n "$candidate" && -f "$candidate" ]] || {
    echo "ERROR: tunnel-client executable not found in official release archive." >&2
    exit 30
  }

  install -o dev -g dev -m 0755 "$candidate" "$TC_DIR.new/tunnel-client"

  # Keep the matched cloudflared companion/manifest beside the client when the
  # official bundle contains them. They are not required by the current stdio
  # poller path, but retaining the matched pair avoids future drift.
  cf="$(find "$tmp/unpacked" -type f -name 'cloudflared' -print -quit)"
  [[ -z "$cf" ]] || install -o dev -g dev -m 0755 "$cf" "$TC_DIR.new/cloudflared"
  manifest="$(find "$tmp/unpacked" -type f -name 'cloudflared-manifest.json' -print -quit)"
  [[ -z "$manifest" ]] || install -o dev -g dev -m 0644 "$manifest" "$TC_DIR.new/cloudflared-manifest.json"

  rm -rf "$TC_DIR.previous"
  if [[ -d "$TC_DIR" ]]; then mv "$TC_DIR" "$TC_DIR.previous"; fi
  mv "$TC_DIR.new" "$TC_DIR"
fi

ln -sfn "$TC_BIN" "$H/.local/bin/tunnel-client"
chown -h dev:dev "$H/.local/bin/tunnel-client"
echo "tunnel-client: $(asdev "$H/.local/bin/tunnel-client" --version)"

echo
echo "== 8. tunnel runtime credential =="
# Prefer a purpose-specific mode-0600 key file. Never print its contents.
if [[ ! -s "$KEY_FILE" ]]; then
  if [[ -n "${CONTROL_PLANE_API_KEY:-}" ]]; then
    umask 077
    printf '%s' "$CONTROL_PLANE_API_KEY" >"$KEY_FILE"
    chown dev:dev "$KEY_FILE"
    chmod 0600 "$KEY_FILE"
    unset CONTROL_PLANE_API_KEY
  elif [[ -s "$CONF/codex-devserver.env" ]]; then
    python3 - "$CONF/codex-devserver.env" "$KEY_FILE" <<'PY'
from pathlib import Path
import os, sys
src=Path(sys.argv[1]); dst=Path(sys.argv[2])
value=None
for raw in src.read_text(encoding="utf-8").splitlines():
    line=raw.strip()
    if line.startswith("export "):
        line=line[7:].lstrip()
    if line.startswith("CONTROL_PLANE_API_KEY="):
        value=line.split("=",1)[1].strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in {"'", '"'}:
            value=value[1:-1]
        break
if value:
    fd=os.open(dst, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
    with os.fdopen(fd,"w") as f: f.write(value)
PY
    chown dev:dev "$KEY_FILE" 2>/dev/null || true
    chmod 0600 "$KEY_FILE" 2>/dev/null || true
  fi
fi

if [[ ! -s "$KEY_FILE" ]]; then
  if [[ -t 0 ]]; then
    echo
    echo "One account secret is unavoidable: the old host's tunnel runtime API key"
    echo "was not stored in Git. Paste a Runtime API key with Tunnels Read + Use."
    echo "Input is hidden and will be written only to $KEY_FILE (mode 0600)."
    read -r -s -p "CONTROL_PLANE_API_KEY: " key
    echo
    if [[ -z "$key" ]]; then
      echo "NEEDS_RUNTIME_KEY"
      exit 40
    fi
    umask 077
    printf '%s' "$key" >"$KEY_FILE"
    unset key
    chown dev:dev "$KEY_FILE"
    chmod 0600 "$KEY_FILE"
  else
    echo "NEEDS_RUNTIME_KEY"
    echo "Create $KEY_FILE as dev, mode 0600, containing a Runtime API key with Tunnels Read + Use."
    exit 40
  fi
fi

echo "Tunnel runtime key: PRESENT (content not displayed)"
echo "Tunnel ID: $TUNNEL_ID"

echo
echo "== 9. codex-devserver-tunnel user service =="
cat >"$UNIT" <<EOF
[Unit]
Description=Codex DevCoveer MCP secure tunnel
After=network-online.target opencode-devcoveer.service
Wants=network-online.target
Requires=opencode-devcoveer.service

[Service]
Type=simple
WorkingDirectory=/home/dev
Environment=HOME=/home/dev
Environment=PATH=/home/dev/.local/bin:/home/dev/.opencode/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/dev/.local/bin/tunnel-client run --control-plane.tunnel-id=$TUNNEL_ID --control-plane.api-key=file:$KEY_FILE --mcp.command=/home/dev/.local/bin/codex-mcp-server --health.listen-addr=127.0.0.1:18080 --log.level=info --log.format=struct-text
Restart=always
RestartSec=3
UMask=0077

[Install]
WantedBy=default.target
EOF

chown dev:dev "$UNIT"
chmod 0644 "$UNIT"

userctl daemon-reload
userctl enable --now codex-devserver-tunnel.service

echo
echo "== 10. readiness =="
ready=0
for _ in $(seq 1 30); do
  if curl --connect-timeout 1 --max-time 3 -fsS "$HEALTH/readyz" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done

echo
printf 'healthz: '
curl --connect-timeout 1 --max-time 3 -fsS "$HEALTH/healthz" || true
echo
printf 'readyz:  '
curl --connect-timeout 1 --max-time 3 -fsS "$HEALTH/readyz" || true
echo

echo
echo "Tunnel service:"
userctl status codex-devserver-tunnel.service --no-pager -l || true

if [[ "$ready" -ne 1 ]]; then
  echo
  echo "CONTROL_PLANE_NOT_READY"
  echo "Recent tunnel journal:"
  runuser -u "$DEV" -- env "${USER_ENV[@]}" \
    journalctl --user -u codex-devserver-tunnel.service -n 80 --no-pager || true
  exit 41
fi

echo
echo "============================================================"
echo " CONTROL_PLANE_READY"
echo "============================================================"
echo "OpenCode is healthy on 127.0.0.1:4097."
echo "Temporary Codex DevCoveer MCP bootstrap runtime is installed."
echo "Secure tunnel is ready on the existing tunnel ID."
echo
echo "Next step is intentionally NOT another root bootstrap:"
echo "ChatGPT should now use Codex DevCoveer MCP itself to restore/upgrade the host."
echo "Log: $LOG"
