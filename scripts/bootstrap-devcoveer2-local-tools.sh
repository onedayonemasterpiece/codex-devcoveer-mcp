#!/usr/bin/env bash
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

DEV_USER="dev"
DEV_HOME="/home/dev"
YC_INSTALL_DIR="${DEV_HOME}/yandex-cloud"
TELETHON_VENV="${DEV_HOME}/.local/share/devcoveer/telethon-venv"
TELETHON_VERSION="1.45.0"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root." >&2
  exit 2
fi

install -d -m 0755 -o "${DEV_USER}" -g "${DEV_USER}" "${DEV_HOME}/.local/bin"
install -d -m 0755 -o "${DEV_USER}" -g "${DEV_USER}" "${DEV_HOME}/.local/share/devcoveer"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTH_BOOTSTRAP="${SCRIPT_DIR}/bootstrap-devcoveer2-auth-home.sh"
if [[ -f "$AUTH_BOOTSTRAP" ]]; then
  echo "=== UNIFIED CLI AUTH HOME ==="
  bash "$AUTH_BOOTSTRAP"
  echo
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "=== YANDEX CLOUD CLI ==="
if [[ ! -x "${YC_INSTALL_DIR}/bin/yc" ]]; then
  curl -fsSL --connect-timeout 15 --max-time 120 \
    https://storage.yandexcloud.net/yandexcloud-yc/install.sh \
    -o "${tmp}/yc-install.sh"
  chmod 0755 "${tmp}/yc-install.sh"

  runuser -u "${DEV_USER}" -- env \
    HOME="${DEV_HOME}" \
    SHELL=/bin/bash \
    PATH="/usr/local/bin:/usr/bin:/bin:${DEV_HOME}/.local/bin" \
    timeout 180 bash "${tmp}/yc-install.sh" -i "${YC_INSTALL_DIR}" -n
fi

test -x "${YC_INSTALL_DIR}/bin/yc"
ln -sfn "${YC_INSTALL_DIR}/bin/yc" /usr/local/bin/yc

echo "yc_path=$(readlink -f /usr/local/bin/yc)"
runuser -u "${DEV_USER}" -- env HOME="${DEV_HOME}" PATH="/usr/local/bin:/usr/bin:/bin" \
  yc version

echo
echo "=== SHARED TELETHON RUNTIME ==="
runuser -u "${DEV_USER}" -- env HOME="${DEV_HOME}" \
  python3 -m venv "${TELETHON_VENV}"

runuser -u "${DEV_USER}" -- env HOME="${DEV_HOME}" \
  timeout 180 "${TELETHON_VENV}/bin/python" -m pip install --disable-pip-version-check -q \
  --upgrade pip setuptools wheel

runuser -u "${DEV_USER}" -- env HOME="${DEV_HOME}" \
  timeout 180 "${TELETHON_VENV}/bin/python" -m pip install --disable-pip-version-check -q \
  "Telethon==${TELETHON_VERSION}"

cat >"${DEV_HOME}/.local/bin/devcoveer-telethon-python" <<EOF
#!/usr/bin/env bash
exec "${TELETHON_VENV}/bin/python" "\$@"
EOF
chmod 0755 "${DEV_HOME}/.local/bin/devcoveer-telethon-python"
chown "${DEV_USER}:${DEV_USER}" "${DEV_HOME}/.local/bin/devcoveer-telethon-python"

runuser -u "${DEV_USER}" -- env HOME="${DEV_HOME}" \
  "${TELETHON_VENV}/bin/python" - <<'PY'
import telethon
print("telethon_version=" + telethon.__version__)
PY

echo
echo "=== LOCAL TOOLING READY ==="
echo "yc=/usr/local/bin/yc"
echo "telethon_python=${DEV_HOME}/.local/bin/devcoveer-telethon-python"
echo "telethon_venv=${TELETHON_VENV}"
echo
echo "NOTE: this installs one shared Telethon library runtime only."
echo "Telegram sessions remain separate credentials/roles in /home/dev/.env."
