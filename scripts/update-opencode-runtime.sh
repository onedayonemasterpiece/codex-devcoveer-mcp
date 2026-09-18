#!/usr/bin/env bash
# Safely update the user-scoped OpenCode binary without changing its config or credentials.
set -Eeuo pipefail

TARGET="${1:-latest}"
DEV_UID="$(id -u dev)"
CURRENT_UID="$(id -u)"
[ "$CURRENT_UID" -eq "$DEV_UID" ] || {
  echo "ERROR: run directly as dev; sudo/root is not used." >&2
  exit 2
}

BIN=/home/dev/.opencode/bin/opencode
UNIT=opencode-devcoveer.service
AUTH=/home/dev/.config/openai-codex-mcp/opencode-server.env
STATE=/home/dev/.local/state/opencode-upgrades
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$STATE/opencode.before-${STAMP}"
export HOME=/home/dev
export XDG_RUNTIME_DIR="/run/user/${DEV_UID}"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${DEV_UID}/bus"
export PATH="/home/dev/.opencode/bin:/home/dev/.local/bin:/usr/local/bin:/usr/bin:/bin"

test -x "$BIN" || { echo "ERROR: OpenCode binary missing: $BIN" >&2; exit 3; }
test -s "$AUTH" || { echo "ERROR: OpenCode server auth file missing" >&2; exit 4; }
mkdir -p "$STATE"
chmod 700 "$STATE"

OLD_VERSION="$($BIN --version)"
cp -a "$BIN" "$BACKUP"
chmod 700 "$BACKUP"
echo "old_version=$OLD_VERSION"
echo "backup=$BACKUP"

rollback() {
  cp -a "$BACKUP" "$BIN"
  systemctl --user restart "$UNIT" || true
  echo "rollback=RESTORED_$OLD_VERSION" >&2
}
trap 'rc=$?; if [ "$rc" -ne 0 ]; then rollback; fi; exit "$rc"' EXIT

if [ "$TARGET" = latest ]; then
  "$BIN" upgrade --method curl
else
  case "$TARGET" in
    v[0-9]*.[0-9]*.[0-9]*|[0-9]*.[0-9]*.[0-9]*) ;;
    *) echo "ERROR: target must be latest or a semantic version" >&2; exit 5 ;;
  esac
  "$BIN" upgrade "$TARGET" --method curl
fi

NEW_VERSION="$($BIN --version)"
echo "new_version=$NEW_VERSION"
if [ "$TARGET" != latest ] && [ "${TARGET#v}" != "$NEW_VERSION" ]; then
  echo "ERROR: requested $TARGET but installed $NEW_VERSION" >&2
  exit 6
fi

systemctl --user restart "$UNIT"
ready=0
for _ in $(seq 1 45); do
  if systemctl --user is-active --quiet "$UNIT"; then
    if /usr/bin/python3 - "$AUTH" "$NEW_VERSION" <<'PY' >/dev/null 2>&1
import base64, json, sys, urllib.request
from pathlib import Path
values={}
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if raw and not raw.lstrip().startswith("#") and "=" in raw:
        key,value=raw.split("=",1); values[key.strip()]=value.strip()
user=values.get("OPENCODE_SERVER_USERNAME"); password=values.get("OPENCODE_SERVER_PASSWORD")
if not user or not password: raise SystemExit(2)
req=urllib.request.Request("http://127.0.0.1:4097/global/health")
token=base64.b64encode(f"{user}:{password}".encode()).decode()
req.add_header("Authorization", "Basic "+token)
with urllib.request.urlopen(req,timeout=3) as response: payload=json.load(response)
if payload.get("healthy") is not True or payload.get("version") != sys.argv[2]: raise SystemExit(3)
PY
    then
      ready=1
      break
    fi
  fi
  sleep 1
done
[ "$ready" -eq 1 ] || { echo "ERROR: updated OpenCode failed authenticated health" >&2; exit 7; }

trap - EXIT
echo "opencode_health=PASS"
echo "rollback=NOT_NEEDED"
