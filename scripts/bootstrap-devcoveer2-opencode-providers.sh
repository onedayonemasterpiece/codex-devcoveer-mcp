#!/usr/bin/env bash
set -Eeuo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/home/dev/.local/bin:/home/dev/.fly/bin:/home/dev/.opencode/bin:/home/dev/yandex-cloud/bin"

DEV_USER="dev"
DEV_HOME="/home/dev"
REPO="${DEV_HOME}/projects/codex-devcoveer-mcp"
OWNER_ENV="${DEV_HOME}/.env"
GIGA_DIR="${DEV_HOME}/.config/gigachat"
GIGA_VENV="${DEV_HOME}/.local/share/gpt2giga/venv"
GIGA_ENV="${GIGA_DIR}/key-1.env"
GIGA_CA="${GIGA_DIR}/russian-ca-bundle.pem"
GIGA_UNIT="${DEV_HOME}/.config/systemd/user/gpt2giga-giga1.service"
OPENCODE_CONFIG="${DEV_HOME}/.config/opencode/config.json"
OPENCODE_AUTH="${DEV_HOME}/.config/openai-codex-mcp/opencode-server.env"
OPENCODE_URL="http://127.0.0.1:4097"
EXPECTED_CA_FP="D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31"
CA_URL="https://raw.githubusercontent.com/koenrh/russian-trusted-root-ca/main/root-ca_rsa-2022.pem"

[[ "$(id -u)" -eq 0 ]] || { echo "Run as root." >&2; exit 2; }
[[ -s "$OWNER_ENV" ]] || { echo "Missing $OWNER_ENV" >&2; exit 2; }
[[ -s "$OPENCODE_AUTH" ]] || { echo "Missing $OPENCODE_AUTH" >&2; exit 2; }

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

opencode_api() {
  local path="$1"
  runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" \
    python3 - "$OPENCODE_AUTH" "$OPENCODE_URL" "$path" <<'PY'
from pathlib import Path
import base64, http.client, json, sys
from urllib.parse import urlsplit

auth_path = Path(sys.argv[1])
base = urlsplit(sys.argv[2])
path = sys.argv[3]
values = {}
for raw in auth_path.read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip().strip('"').strip("'")
username = values.get("OPENCODE_SERVER_USERNAME", "")
password = values.get("OPENCODE_SERVER_PASSWORD", "")
if not username or not password:
    raise SystemExit("OpenCode server auth is incomplete")
token = base64.b64encode(f"{username}:{password}".encode()).decode()
conn = http.client.HTTPConnection(base.hostname, base.port or 80, timeout=10)
try:
    conn.request("GET", path, headers={"Authorization": f"Basic {token}", "Accept": "application/json"})
    response = conn.getresponse()
    body = response.read(2_000_001)
finally:
    conn.close()
if response.status != 200:
    raise SystemExit(f"OpenCode API {path} returned HTTP {response.status}")
if len(body) > 2_000_000:
    raise SystemExit("OpenCode API response exceeded 2 MB")
sys.stdout.buffer.write(body)
PY
}

echo "=== 1/8 PROVIDER OWNER CONTRACT ==="
python3 - <<'PY'
from pathlib import Path
import json

path = Path("/home/dev/.env")
values = {}
for raw in path.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
    s = raw.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    key, value = s.split("=", 1)
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    values[key.strip()] = value

required = (
    "GROQ_API_KEY", "GROQ_API_KEY2", "GROQ_API_KEY3",
    "NVIDIA_API_KEY",
    "GIGACHAT_API_KEY", "GIGACHAT_API_KEY2", "GIGACHAT_API_KEY3",
)
missing = [key for key in required if not values.get(key)]
for key in required:
    print(f"{key}={'SET' if values.get(key) else 'MISSING'}")
if missing:
    raise SystemExit("Missing provider credentials: " + ", ".join(missing))
print("PROVIDER_OWNER_CONTRACT=PASS")
PY

echo
echo "=== 2/8 RENDER OPENCODE PROVIDER ENV ONLY ==="
python3 "$REPO/scripts/devcoveer-env-recovery.py" render-providers

echo
echo "=== 3/8 ACTIVE SESSION GATE ==="
opencode_api "/session/status" > /tmp/devcoveer-opencode-session-status.json
python3 - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("/tmp/devcoveer-opencode-session-status.json").read_text(encoding="utf-8"))
active = sorted(
    sid for sid, state in payload.items()
    if isinstance(state, dict) and state.get("type") in {"busy", "retry"}
)
print("active_sessions=" + ",".join(active))
if active:
    raise SystemExit("OpenCode has active/retrying sessions; provider restart blocked")
print("ACTIVE_SESSION_GATE=PASS")
PY
rm -f /tmp/devcoveer-opencode-session-status.json

echo
echo "=== 4/8 RESTORE WORKING GIGACHAT GIGA1 ==="
install -d -m 0700 -o "$DEV_USER" -g "$DEV_USER" "$GIGA_DIR"
install -d -m 0755 -o "$DEV_USER" -g "$DEV_USER" "$(dirname "$GIGA_VENV")"
install -d -m 0755 -o "$DEV_USER" -g "$DEV_USER" "$(dirname "$GIGA_UNIT")"

if [[ ! -x "$GIGA_VENV/bin/python" ]]; then
  if command -v uv >/dev/null 2>&1; then
    runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" uv venv --python 3.12 "$GIGA_VENV"
  else
    runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" python3 -m venv "$GIGA_VENV"
  fi
fi

if ! runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" "$GIGA_VENV/bin/python" - <<'PY' >/dev/null 2>&1
import importlib.metadata
raise SystemExit(0 if importlib.metadata.version("gpt2giga") == "0.3.0" else 1)
PY
then
  if command -v uv >/dev/null 2>&1; then
    runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" \
      uv pip install --python "$GIGA_VENV/bin/python" "gpt2giga==0.3.0"
  else
    runuser -u "$DEV_USER" -- env HOME="$DEV_HOME" \
      "$GIGA_VENV/bin/python" -m pip install "gpt2giga==0.3.0"
  fi
fi

tmp_ca="$(mktemp /tmp/devcoveer-giga-ca.XXXXXX.pem)"
trap 'rm -f "$tmp_ca"' EXIT
curl -fsSL --connect-timeout 15 --max-time 90 "$CA_URL" -o "$tmp_ca"

observed_fp="$(
  openssl x509 -in "$tmp_ca" -noout -fingerprint -sha256 \
    | sed 's/^.*=//;s/://g' | tr '[:lower:]' '[:upper:]'
)"
[[ "$observed_fp" == "$EXPECTED_CA_FP" ]] || {
  echo "GigaChat CA fingerprint mismatch" >&2
  exit 30
}
install -o "$DEV_USER" -g "$DEV_USER" -m 0644 "$tmp_ca" "$GIGA_CA"

python3 - <<'PY'
from pathlib import Path
import json, os, pwd

src = Path("/home/dev/.env")
dst = Path("/home/dev/.config/gigachat/key-1.env")
values = {}
for raw in src.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
    s = raw.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    key, value = s.split("=", 1)
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    values[key.strip()] = value

credential = values.get("GIGACHAT_API_KEY", "")
if not credential:
    raise SystemExit("GIGACHAT_API_KEY is empty")

lines = [
    "GIGACHAT_CREDENTIALS=" + json.dumps(credential, ensure_ascii=False),
    "GIGACHAT_SCOPE=GIGACHAT_API_PERS",
    "GIGACHAT_MODEL=GigaChat-3-Ultra",
    "GIGACHAT_CA_BUNDLE_FILE=/home/dev/.config/gigachat/russian-ca-bundle.pem",
]
tmp = dst.with_name(".key-1.env.tmp")
tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
pw = pwd.getpwnam("dev")
os.chown(tmp, pw.pw_uid, pw.pw_gid)
os.chmod(tmp, 0o600)
os.replace(tmp, dst)
os.chown(dst, pw.pw_uid, pw.pw_gid)
os.chmod(dst, 0o600)
PY

cat >"$GIGA_UNIT" <<'EOF_UNIT'
[Unit]
Description=GigaChat OpenAI-compatible gateway giga1
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/dev
Environment=HOME=/home/dev
ExecStart=/home/dev/.local/share/gpt2giga/venv/bin/python3 /home/dev/.local/share/gpt2giga/venv/bin/gpt2giga --proxy.mode DEV --proxy.host 127.0.0.1 --proxy.port 8091 --env-path /home/dev/.config/gigachat/key-1.env --gigachat.ca-bundle-file /home/dev/.config/gigachat/russian-ca-bundle.pem --gigachat.verify-ssl-certs true
Restart=on-failure
RestartSec=3
UMask=0077

[Install]
WantedBy=default.target
EOF_UNIT
chown "$DEV_USER:$DEV_USER" "$GIGA_UNIT"
chmod 0644 "$GIGA_UNIT"

loginctl enable-linger "$DEV_USER" >/dev/null 2>&1 || true
systemctl start "user@${DEV_UID}.service" >/dev/null 2>&1 || true
userctl daemon-reload
userctl enable --now gpt2giga-giga1.service

echo

echo "=== 5/8 INSTALL OPENCODE PROVIDER DEFINITIONS ==="
python3 - <<'PY'
from pathlib import Path
import json, os, pwd, time

path = Path("/home/dev/.config/opencode/config.json")
path.parent.mkdir(parents=True, exist_ok=True)
backup_marker = Path("/tmp/devcoveer-opencode-provider-backup-path")

if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Refusing to replace invalid existing {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"Refusing non-object OpenCode config: {path}")
    backup = path.with_name(f"config.json.before-provider-recovery-{int(time.time())}")
    backup.write_bytes(path.read_bytes())
    backup_marker.write_text(str(backup), encoding="utf-8")
else:
    data = {}
    backup_marker.write_text("", encoding="utf-8")

data.setdefault("$schema", "https://opencode.ai/config.json")
providers = data.setdefault("provider", {})
if not isinstance(providers, dict):
    raise SystemExit("OpenCode config provider field is not an object")

groq_model = {
    "id": "openai/gpt-oss-120b",
    "name": "GPT-OSS-120B",
    "reasoning": True,
    "tool_call": True,
    "limit": {"context": 131072, "output": 65536},
}

providers["groq2"] = {
    "id": "groq2",
    "name": "Groq GPT-OSS-120B - key 2",
    "npm": "@ai-sdk/openai-compatible",
    "options": {
        "baseURL": "https://api.groq.com/openai/v1",
        "apiKey": "{env:GROQ_API_KEY2}",
    },
    "models": {"openai/gpt-oss-120b": groq_model},
}
providers["groq3"] = {
    "id": "groq3",
    "name": "Groq GPT-OSS-120B - key 3",
    "npm": "@ai-sdk/openai-compatible",
    "options": {
        "baseURL": "https://api.groq.com/openai/v1",
        "apiKey": "{env:GROQ_API_KEY3}",
    },
    "models": {"openai/gpt-oss-120b": groq_model},
}
providers["giga1"] = {
    "id": "giga1",
    "name": "GigaChat 3.5 Ultra - key 1",
    "npm": "@ai-sdk/openai-compatible",
    "options": {"baseURL": "http://127.0.0.1:8091/v1"},
    "models": {
        "GigaChat-3-Ultra": {
            "id": "GigaChat-3-Ultra",
            "name": "GigaChat 3.5 Ultra",
            "tool_call": True,
            "reasoning": False,
            "limit": {"context": 32768, "output": 8192},
        }
    },
}

tmp = path.with_name(".config.json.provider-recovery.tmp")
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
pwd = pwd.getpwnam("dev")
os.chown(tmp, pw.pw_uid, pw.pw_gid)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
os.chown(path, pw.pw_uid, pw.pw_gid)
os.chmod(path, 0o600)
print("opencode_provider_config=UPDATED")
print("active_custom_providers=groq2,groq3,giga1")
print("planned_not_enabled=giga2,giga3")
PY

echo
echo "=== 6/8 RESTART OPENCODE WITH ROLLBACK ==="
userctl daemon-reload
if ! userctl restart opencode-devcoveer.service; then
  echo "OpenCode restart command failed" >&2
  exit 40
fi

opencode_ready=0
for _ in $(seq 1 30); do
  if userctl is-active --quiet opencode-devcoveer.service \
     && opencode_api "/global/health" >/tmp/devcoveer-opencode-health.json 2>/dev/null; then
    opencode_ready=1
    break
  fi
  sleep 2
done

if [[ "$opencode_ready" != 1 ]]; then
  echo "OpenCode failed after provider config; rolling config back" >&2
  backup_path="$(cat /tmp/devcoveer-opencode-provider-backup-path 2>/dev/null || true)"
  if [[ -n "$backup_path" && -f "$backup_path" ]]; then
    cp -a "$backup_path" "$OPENCODE_CONFIG"
    chown "$DEV_USER:$DEV_USER" "$OPENCODE_CONFIG"
    chmod 0600 "$OPENCODE_CONFIG"
  else
    rm -f "$OPENCODE_CONFIG"
  fi
  userctl restart opencode-devcoveer.service || true
  userctl --no-pager --full status opencode-devcoveer.service || true
  exit 41
fi
rm -f /tmp/devcoveer-opencode-health.json

echo
echo "=== 7/8 LIVE CATALOG READBACK ==="
opencode_api "/config/providers" > /tmp/devcoveer-opencode-providers.json
python3 - <<'PY'
from pathlib import Path
import json

payload = json.loads(Path("/tmp/devcoveer-opencode-providers.json").read_text(encoding="utf-8"))
providers = payload.get("providers") if isinstance(payload, dict) else None
if not isinstance(providers, list):
    raise SystemExit("OpenCode /config/providers response is invalid")

catalog = {}
for provider in providers:
    if not isinstance(provider, dict) or not isinstance(provider.get("id"), str):
        continue
    models = provider.get("models") if isinstance(provider.get("models"), dict) else {}
    catalog[provider["id"]] = set(models)

requirements = {
    "groq/openai/gpt-oss-120b": "openai/gpt-oss-120b" in catalog.get("groq", set()),
    "groq2/openai/gpt-oss-120b": "openai/gpt-oss-120b" in catalog.get("groq2", set()),
    "groq3/openai/gpt-oss-120b": "openai/gpt-oss-120b" in catalog.get("groq3", set()),
    "giga1/GigaChat-3-Ultra": "GigaChat-3-Ultra" in catalog.get("giga1", set()),
    "nvidia": bool(catalog.get("nvidia")),
    "zen": bool(catalog.get("opencode")),
}
for key, ok in requirements.items():
    print(f"{key}={'PASS' if ok else 'MISSING'}")
print("connected_provider_ids=" + ",".join(sorted(catalog)))
if not all(requirements.values()):
    raise SystemExit("OpenCode live catalog is incomplete")
print("OPENCODE_CATALOG=PASS")
PY
rm -f /tmp/devcoveer-opencode-providers.json /tmp/devcoveer-opencode-provider-backup-path

echo
echo "=== 8/8 BOUNDED REAL PROVIDER SMOKES ==="
smoke_model() {
  local selection="$1"
  local label="$2"
  local out="/tmp/devcoveer-smoke-${label}.txt"
  if runuser -u "$DEV_USER" -- env \
      HOME="$DEV_HOME" \
      PATH="/usr/local/bin:/usr/bin:/bin:$DEV_HOME/.local/bin:$DEV_HOME/.opencode/bin" \
      bash -c '
        set -a
        . "$HOME/.config/openai-codex-mcp/opencode-server.env"
        set +a
        timeout 120 "$HOME/.opencode/bin/opencode" run \
          --attach http://127.0.0.1:4097 \
          --dir /home/dev/projects/codex-devcoveer-mcp \
          --model "$1" \
          "Reply with exactly OK and nothing else."
      ' _ "$selection" >"$out" 2>&1; then
    if grep -Eq '(^|[^A-Z])OK([^A-Z]|$)' "$out"; then
      echo "${label}=PASS"
      rm -f "$out"
      return 0
    fi
  fi
  echo "${label}=FAIL" >&2
  tail -n 20 "$out" | sed -E 's/(Authorization: Bearer )[A-Za-z0-9._~+\/=-]+/\1<redacted>/Ig; s/([A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Za-z0-9_]*=)[^ ]+/\1<redacted>/Ig' >&2 || true
  return 1
}

smoke_model "groq/openai/gpt-oss-120b" "groq"
smoke_model "groq2/openai/gpt-oss-120b" "groq2"
smoke_model "groq3/openai/gpt-oss-120b" "groq3"
smoke_model "giga1/GigaChat-3-Ultra" "giga1"

echo
echo "groq_pool=groq,groq2,groq3"
echo "giga_active=giga1"
echo "giga_planned_not_enabled=giga2,giga3"
echo "nvidia=catalog_connected"
echo "zen=catalog_connected"
echo "OPENCODE_PROVIDER_RECOVERY_COMPLETE"
