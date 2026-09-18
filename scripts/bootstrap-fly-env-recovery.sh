#!/usr/bin/env bash
# Install Fly CLI for dev, perform the normal interactive Fly login, then
# recover secret values from every Fly app visible to that authenticated account.
set -Eeuo pipefail

DEV=dev
H=/home/dev
REPO=/home/dev/projects/codex-devcoveer-mcp
FLY_HOME="$H/.fly"
FLY="$FLY_HOME/bin/flyctl"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run as root; Fly itself will run as user dev." >&2
  exit 2
fi

asdev() {
  runuser -u "$DEV" -- env \
    HOME="$H" \
    FLYCTL_INSTALL="$FLY_HOME" \
    PATH="$FLY_HOME/bin:/usr/local/bin:/usr/bin:/bin:$H/.local/bin:$H/.opencode/bin" \
    "$@"
}

echo "============================================================"
echo " Fly account recovery bootstrap"
echo "============================================================"

echo
echo "== 1. Install/update flyctl for dev =="
install -d -o dev -g dev -m 0755 "$FLY_HOME"
if [[ ! -x "$FLY" ]]; then
  tmp="$(mktemp /tmp/fly-install.XXXXXX)"
  trap 'rm -f "$tmp"' EXIT
  curl -fsSL https://fly.io/install.sh -o "$tmp"
  chown dev:dev "$tmp"
  asdev sh "$tmp"
fi

if [[ ! -x "$FLY" ]]; then
  echo "ERROR: flyctl installation did not produce $FLY" >&2
  exit 10
fi

echo -n "flyctl: "
asdev "$FLY" version

echo
echo "== 2. Authenticate Fly account =="
if asdev "$FLY" auth whoami >/tmp/devcoveer-fly-whoami 2>/dev/null; then
  printf "Already authenticated as: "
  cat /tmp/devcoveer-fly-whoami
  rm -f /tmp/devcoveer-fly-whoami
else
  rm -f /tmp/devcoveer-fly-whoami
  echo "fly auth login will now start the official Fly browser authorization."
  echo "Open/confirm the URL it prints if VS Code does not open it automatically."
  asdev "$FLY" auth login
fi

echo
echo "== 3. Verify account and enumerate apps =="
printf "Authenticated as: "
asdev "$FLY" auth whoami
echo
echo "Fly apps visible to this account:"
asdev "$FLY" apps list || {
  echo "ERROR: authenticated Fly account cannot list apps." >&2
  exit 20
}

echo
echo "== 4. Recover env/secrets from ALL visible Fly apps =="
cd "$REPO"
python3 "$REPO/scripts/devcoveer-env-recovery.py" recover --no-install-flyctl --no-prompt

echo
echo "============================================================"
echo " FLY_ENV_SWEEP_COMPLETE"
echo "============================================================"
echo "Recovered app snapshots: /etc/devcoveer/env/recovered/fly/"
echo "Unified editable master: /etc/devcoveer/env/master.env"
echo "Missing-only list:       /etc/devcoveer/env/todo.env"
echo "Per-project/app envs:    /etc/devcoveer/env/services/"
