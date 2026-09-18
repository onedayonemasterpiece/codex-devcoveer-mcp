#!/usr/bin/env python3
"""Recover and centralize DevCoveer2 environment/secrets.

Run as root. The tool:
- inventories environment variable names from repositories under /home/dev/projects;
- recovers Fly secrets from *running* Machines when flyctl auth is available;
- never prints secret values;
- writes one root-only master env plus a blank TODO file;
- renders per-project env files containing only the variables each project needs.

The master file is the editable source of truth. Runtime services must consume
only /etc/devcoveer/env/services/<project>.env, never master.env directly.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

DEV = "dev"
DEV_HOME = Path("/home/dev")
PROJECTS_ROOT = DEV_HOME / "projects"
ROOT = Path("/etc/devcoveer/env")
OWNER = DEV_HOME / ".env"
LEGACY_ROOT_OWNER = ROOT / "owner.env"
LEGACY_MASTER = ROOT / "master.env"
MASTER = OWNER
TODO = ROOT / "todo.env"
CATALOG = ROOT / "catalog.json"
RECOVERED = ROOT / "recovered" / "fly"
SERVICES = ROOT / "services"
README = ROOT / "README.txt"
LEGACY_OWNER_FILL = ROOT / "owner-fill.env"
LEGACY_OWNER_FILL_SUMMARY = ROOT / "owner-fill-summary.json"
FLY_RELEASE_ENV = DEV_HOME / ".config" / "fly" / "release.env"
FLY_COMMON = (
    DEV_HOME / ".fly" / "bin" / "flyctl",
    DEV_HOME / ".local" / "bin" / "flyctl",
    Path("/usr/local/bin/flyctl"),
    Path("/usr/bin/flyctl"),
)

TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".toml", ".yaml", ".yml",
}
IGNORE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "dist", "build", ".astro",
    ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache",
    ".codex", ".agent", ".github", "docs", "tests", "test", "fixtures",
    "artifacts", ".recovery",
}
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
SENSITIVE = re.compile(
    r"(?:^|_)(?:TOKEN|KEY|SECRET|PASSWORD|PASS|CREDENTIALS?|SESSION|AUTH|"
    r"BUNDLE|API_ID|API_HASH|DATABASE_URL|DSN|PRIVATE|SIGNING|SALT|COOKIE|"
    r"BEARER|OAUTH|ACCESS_KEY|SERVICE_ACCOUNT|USERNAME|USER_NAME|LOGIN|"
    r"CLIENT_ID|CLIENT_SECRET|USERNAME|USER_NAME|USER|POSTGRES_USER|DB_USER)(?:_|$)"
)
PY_PATTERNS = (
    re.compile(r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]+)["']"""),
    re.compile(r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]+)["']"""),
    re.compile(r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]+)["']\s*\]"""),
)
JS_PATTERNS = (
    re.compile(r"process\.env\.([A-Z][A-Z0-9_]+)"),
    re.compile(r"""process\.env\[\s*["']([A-Z][A-Z0-9_]+)["']\s*\]"""),
)

# Safe syntactic aliases observed in the project code. These are names for the
# same Telegram application credential, not two independent secrets.
ALIASES = {
    # Telegram legacy names.
    "TG_API_ID": "TELEGRAM_API_ID",
    "TG_API_HASH": "TELEGRAM_API_HASH",

    # Google key-pool spellings used by older jobs and newer shared clients.
    "GOOGLE_API_KEY_2": "GOOGLE_API_KEY2",
    "GOOGLE_API_KEY_3": "GOOGLE_API_KEY3",
    "GOOGLE_API_KEY_4": "GOOGLE_API_KEY4",
    "GOOGLE_API_KEY_5": "GOOGLE_API_KEY5",
    "GOOGLE_API_KEY_6": "GOOGLE_API_KEY6",

    # events-bot benchmark/fallback code explicitly treats this as the same
    # 4o credential, not a separate owner-managed secret.
    "FOUR_4O_TOKEN": "FOUR_O_TOKEN",
}

# Canonical project ownership for Fly apps. This is intentionally explicit:
# app names are deployment identities, not always repository names.
FLY_APP_PROJECT_MAP = {
    "events-bot-new-wngqia": "events-bot-new",
    "events-bot-new": "events-bot-new-legacy-fly",
    "cat-weather-new": "cat-weather-new",
    "znanie-kgd80-fest": "kdg80",
    "kenigevents-autopresenter": "kenigevents-autopresenter",
}

# Common CI/runtime variables that should not become owner secrets.
DENY = {
    "HOME", "PATH", "PWD", "SHELL", "USER", "LOGNAME", "TMPDIR",
    "GITHUB_ACTIONS", "GITHUB_SHA", "GITHUB_REF", "GITHUB_ENV",
    "GITHUB_OUTPUT", "GITHUB_EVENT_PATH", "CI", "PORT",
}

# Authentication state produced by local CLIs/services is not an owner input.
# It stays in the corresponding tool's own auth/config store and must not clutter
# /home/dev/.env. Project-specific provider credentials are NOT excluded here.
DERIVED_AUTH_STATE_NAMES = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "FLY_API_TOKEN",
    "FLY_ACCESS_TOKEN",
}

# Locally generated service-to-service/control-plane secrets are runtime state,
# not credentials the owner obtains externally and pastes into the shared .env.
# They may still be preserved in recovered runtime snapshots and rendered into
# service-specific envs where available.
INTERNAL_RUNTIME_SECRET_PATTERNS = (
    re.compile(r"(?:^|_)MCP_(?:DEVELOPMENT_TOKEN|BEARER_TOKEN|REF_SECRET|PATH_SECRET|OPERATOR_TOKEN|SOCIAL_APPROVAL_TOKEN|SIGNING_KEY)(?:_|$)"),
    re.compile(r"(?:^|_)WORKER_RESULT_TOKEN(?:_|$)"),
    re.compile(r"(?:^|_)CONNECTOR_CREDENTIALS_JSON(?:_|$)"),
    re.compile(r"(?:^|_)SERVICE_TOKEN(?:_|$)"),
)


class CommandError(RuntimeError):
    pass


def ensure_root() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this tool as root.")


def safe_mkdir(path: Path, mode: int = 0o700, owner_dev: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    if owner_dev:
        import pwd
        pw = pwd.getpwnam(DEV)
        os.chown(path, pw.pw_uid, pw.pw_gid)


def run(
    argv: list[str],
    *,
    as_dev: bool = False,
    env: dict[str, str] | None = None,
    timeout: int = 90,
    check: bool = True,
    binary: bool = False,
) -> subprocess.CompletedProcess:
    cmd = argv
    merged = os.environ.copy()
    if env:
        merged.update(env)
    if as_dev:
        cmd = [
            "runuser", "-u", DEV, "--", "env",
            f"HOME={DEV_HOME}",
            f"PATH=/usr/local/bin:/usr/bin:/bin:{DEV_HOME}/.local/bin:{DEV_HOME}/.fly/bin:{DEV_HOME}/.opencode/bin",
            *argv,
        ]
    cp = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
        env=merged,
        timeout=timeout,
        check=False,
    )
    if check and cp.returncode != 0:
        raise CommandError(f"command failed rc={cp.returncode}: {argv[0]}")
    return cp


def project_dirs() -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not PROJECTS_ROOT.is_dir():
        return result
    for p in sorted(PROJECTS_ROOT.iterdir()):
        if p.is_dir() and (p / ".git").exists():
            result[p.name] = p
    return result


def parse_env_example(path: Path) -> set[str]:
    out: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name[7:].strip()
        if ENV_NAME.fullmatch(name):
            out.add(name)
    return out


def scan_project(name: str, root: Path) -> dict[str, Any]:
    required: set[str] = set()
    examples: list[str] = []
    fly_app: str | None = None
    source_controlled_env: set[str] = set()

    for p in root.rglob("*"):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(part in IGNORE_DIRS for part in rel.parts[:-1]):
            continue
        if not p.is_file() or p.is_symlink():
            continue
        if p.stat().st_size > 2_000_000:
            continue

        lname = p.name.lower()
        if (
            lname == ".env.example"
            or lname.endswith(".env.example")
            or lname.endswith(".env.sample")
            or lname.endswith(".env.template")
        ):
            required.update(parse_env_example(p))
            examples.append(rel.as_posix())

        if rel.as_posix() == "fly.toml":
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8"))
                app = data.get("app")
                if isinstance(app, str) and app.strip():
                    fly_app = app.strip()
                env_block = data.get("env")
                if isinstance(env_block, dict):
                    for key in env_block:
                        if isinstance(key, str) and ENV_NAME.fullmatch(key):
                            source_controlled_env.add(key)
            except Exception:
                pass

        if p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        patterns = PY_PATTERNS if p.suffix.lower() == ".py" else JS_PATTERNS
        for pattern in patterns:
            required.update(pattern.findall(text))

    required -= DENY
    return {
        "project": name,
        "path": str(root),
        "fly_app": fly_app,
        "env_examples": sorted(examples),
        "required": sorted(required),
        "source_controlled_env": sorted(source_controlled_env),
    }


def canonical(name: str) -> str:
    return ALIASES.get(name, name)


def secret_like(name: str) -> bool:
    """Keep owner .env limited to actual credentials, not configuration selectors."""
    n = canonical(name)
    if n in DENY:
        return False
    if n.startswith(("ENABLE_", "DISABLE_")) or n.endswith(("_ENABLED", "_DISABLED")):
        return False
    # These variables contain the *name* of another secret env var, not a secret.
    if re.search(r"_(?:KEY|TOKEN|SECRET|AUTH_BUNDLE|SESSION|CREDENTIALS?)_ENVS?$", n):
        return False
    if re.search(r"_(?:KEY|TOKEN|SECRET|AUTH_BUNDLE|SESSION|CREDENTIALS?)_VAR(?:S|_NAME)?$", n):
        return False
    if n.endswith(("_PATH", "_DIR", "_ROOT", "_FILE", "_FILES", "_LINK")):
        return False
    if n.endswith(("_MODEL_KEY", "_PROFILE_KEY", "_CACHE_KEY", "_IDEMPOTENCY_KEY", "_POLICY_KEY")):
        return False
    if n.endswith(("_URL", "_URI", "_ENDPOINT", "_HOST", "_PORT")) and not (
        n.endswith("DATABASE_URL") or n.endswith("_DSN")
    ):
        return False
    # Generic AUTH names are often modes/state paths. Require a concrete
    # credential noun below rather than admitting every string containing AUTH.
    credential_patterns = (
        r"(?:^|_)(?:TOKEN|PASSWORD|PASS|SECRET)(?:_|$)",
        r"(?:^|_)API_KEY\d*(?:_|$)",
        r"(?:^|_)KEY(?:_|$)",
        r"(?:^|_)API_ID(?:_|$)",
        r"(?:^|_)API_HASH(?:_|$)",
        r"(?:^|_)ACCESS_KEY_ID(?:_|$)",
        r"(?:^|_)SECRET_ACCESS_KEY(?:_|$)",
        r"(?:^|_)(?:USERNAME|USER_NAME|LOGIN)(?:_|$)",
        r"(?:^|_)CLIENT_ID(?:_|$)",
        r"(?:^|_)CLIENT_SECRET(?:_|$)",
        r"(?:^|_)SESSION(?:_|$)",
        r"(?:^|_)AUTH_BUNDLE(?:_|$)",
        r"(?:^|_)CREDENTIALS?(?:_|$)",
        r"(?:^|_)PRIVATE_KEY(?:_|$)",
        r"(?:^|_)SIGNING_KEY(?:_|$)",
        r"(?:^|_)SERVICE_ACCOUNT(?:_KEY|_JSON|_CREDENTIALS)(?:_|$)",
        r"(?:^|_)DATABASE_URL(?:_|$)",
        r"(?:^|_)DSN(?:_|$)",
        r"^(?:POSTGRES|DB)_USER$",
    )
    return any(re.search(pattern, n) for pattern in credential_patterns)


def owner_managed_credential(name: str) -> bool:
    """Return True only for credentials the owner intentionally maintains.

    CLI/service login state (gh/fly/etc.) and locally generated control-plane
    secrets stay out of /home/dev/.env. External provider/account credentials
    remain owner-managed even when consumed by MCP code.
    """
    n = canonical(name)
    if not secret_like(n):
        return False
    if n in DERIVED_AUTH_STATE_NAMES:
        return False

    family = credential_family(n)

    # Provider credentials remain owner inputs even when their consumer is an MCP.
    if family in {"TELEGRAM", "VK", "MAX"}:
        return True

    # Internal/local service credentials are generated or recovered elsewhere.
    if any(pattern.search(n) for pattern in INTERNAL_RUNTIME_SECRET_PATTERNS):
        return False
    if "_MCP_" in n or n.startswith("MCP_"):
        return False

    # Generic GitHub/Fly credentials come from CLI login / CI auth state.
    if family in {"GITHUB", "FLY.IO", "OAUTH / MCP"}:
        return False

    return True


def credential_family(name: str) -> str:
    n = canonical(name)
    # Specific embedded providers win over a broader prefix. Example:
    # GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY belongs to SUPABASE, not GOOGLE.
    if "SUPABASE" in n:
        return "SUPABASE"
    if n.startswith("YDB_") or "_YDB_" in n:
        return "YDB"
    if n.startswith("GOOGLE_") or "_GOOGLE_" in n:
        return "GOOGLE AI"
    if n.startswith("NVIDIA_") or "_NVIDIA_" in n:
        return "NVIDIA"
    if n.startswith("GIGACHAT_") or "_GIGACHAT_" in n:
        return "GIGACHAT"
    if n.startswith("GROQ") or "_GROQ" in n:
        return "GROQ"
    if n.startswith("FOUR_") or "_FOUR_" in n:
        return "OPENAI / 4O"
    if n.startswith("OPENAI_") or n.startswith("CODEX_") or "_OPENAI_" in n:
        return "OPENAI / CODEX"
    if n.startswith("KAGGLE_") or "_KAGGLE_" in n:
        return "KAGGLE"
    if n.startswith(("TELEGRAM_", "TG_")) or "_TELEGRAM_" in n:
        return "TELEGRAM"
    if n.startswith("VK_") or "_VK_" in n:
        return "VK"
    if n.startswith("MAX_") or "_MAX_" in n:
        return "MAX"
    if n.startswith(("YANDEX_", "YC_", "AWS_", "S3_")) or any(
        part in n for part in ("_YANDEX_", "_YC_", "_AWS_", "_S3_")
    ):
        return "YANDEX CLOUD / S3"
    if n.startswith("GITHUB_") or "_GITHUB_" in n:
        return "GITHUB"
    if n.startswith("FLY_") or "_FLY_" in n:
        return "FLY.IO"
    if "PINTEREST" in n:
        return "PINTEREST"
    if n.startswith("POSTBOX_") or "_POSTBOX_" in n:
        return "POSTBOX"
    if n.startswith("NOTISEND_") or "_NOTISEND_" in n:
        return "NOTISEND"
    if n.startswith(("SMTP_", "IMAP_", "EMAIL_", "MAIL_")) or any(
        part in n for part in ("_SMTP_", "_IMAP_", "_EMAIL_", "_MAIL_")
    ):
        return "MAIL / IMAP / SMTP"
    if n.startswith("TELEGRAPH_") or "_TELEGRAPH_" in n:
        return "TELEGRAPH"
    if n.startswith("SPACEWEB_") or "_SPACEWEB_" in n:
        return "SPACEWEB"
    if n.startswith(("CLOUDFLARE_", "TUNNEL_")) or any(
        part in n for part in ("_CLOUDFLARE_", "_TUNNEL_")
    ):
        return "TUNNEL / CLOUDFLARE"
    if "OAUTH" in n or "_MCP_" in n or n.startswith("MCP_"):
        return "OAUTH / MCP"
    if "DATABASE_URL" in n or n.endswith("_DSN") or n.startswith(("POSTGRES_", "DB_")):
        return "DATABASES"
    if "SERVICE_ACCOUNT" in n or "PRIVATE_KEY" in n or "SIGNING_KEY" in n:
        return "SERVICE ACCOUNTS / SIGNING"
    # Unknown credential families still get a dedicated named section.
    return n.split("_", 1)[0]


def find_flyctl() -> Path | None:
    for p in FLY_COMMON:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    cp = run(["bash", "-lc", "command -v flyctl || true"], as_dev=True, check=False)
    value = (cp.stdout or "").strip()
    if value:
        p = Path(value)
        if p.is_file():
            return p
    return None


def install_flyctl() -> Path | None:
    """Install official flyctl for dev only when it is absent."""
    current = find_flyctl()
    if current:
        return current
    with tempfile.TemporaryDirectory(prefix="devcoveer-fly-install-") as td:
        script = Path(td) / "install.sh"
        cp = run(
            ["curl", "-fsSL", "https://fly.io/install.sh", "-o", str(script)],
            timeout=60,
            check=False,
        )
        if cp.returncode != 0:
            return None
        # The official installer honors FLYCTL_INSTALL.
        cp = run(
            ["sh", str(script)],
            as_dev=True,
            env={"FLYCTL_INSTALL": str(DEV_HOME / ".fly")},
            timeout=180,
            check=False,
        )
        if cp.returncode != 0:
            return None
    return find_flyctl()


def fly_whoami(flyctl: Path, env: dict[str, str]) -> bool:
    cp = run(
        [str(flyctl), "auth", "whoami"],
        as_dev=True, env=env, check=False, timeout=30,
    )
    return cp.returncode == 0


def maybe_fly_auth(flyctl: Path, *, allow_prompt: bool) -> dict[str, str] | None:
    """Use the normal cached Fly account login; otherwise use saved snapshots."""
    del allow_prompt
    if fly_whoami(flyctl, {}):
        return {}
    return None


def parse_json_output(cp: subprocess.CompletedProcess) -> Any:
    raw = cp.stdout
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(raw or "null")


def fly_apps(flyctl: Path, env: dict[str, str]) -> list[str]:
    cp = run([str(flyctl), "apps", "list", "--json"], as_dev=True, env=env, check=False, timeout=60)
    if cp.returncode != 0:
        return []
    try:
        data = parse_json_output(cp)
    except Exception:
        return []
    items = data if isinstance(data, list) else data.get("apps", []) if isinstance(data, dict) else []
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
            continue
        if isinstance(item, dict):
            for key in ("Name", "name", "App", "app"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    out.append(value)
                    break
    return sorted(set(out))


def fly_secret_names(flyctl: Path, app: str, env: dict[str, str]) -> tuple[set[str], bool]:
    cp = run(
        [str(flyctl), "secrets", "list", "-a", app, "--json"],
        as_dev=True, env=env, check=False, timeout=60,
    )
    if cp.returncode != 0:
        return set(), False
    try:
        data = parse_json_output(cp)
    except Exception:
        return set(), False
    items = data if isinstance(data, list) else data.get("secrets", []) if isinstance(data, dict) else []
    names: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            for key in ("Name", "name"):
                value = item.get(key)
                if isinstance(value, str) and ENV_NAME.fullmatch(value):
                    names.add(value)
                    break
    return names, True


def parse_env_bytes(raw: bytes) -> dict[str, str]:
    # fly ssh may emit a short text prefix before the first NUL; only valid
    # NAME=value records are admitted.
    records = raw.split(b"\0") if b"\0" in raw else raw.splitlines()
    out: dict[str, str] = {}
    for record in records:
        if b"=" not in record:
            continue
        key_b, value_b = record.split(b"=", 1)
        try:
            key = key_b.decode("ascii", errors="strict").strip()
        except UnicodeError:
            continue
        if not ENV_NAME.fullmatch(key):
            continue
        out[key] = value_b.decode("utf-8", errors="surrogateescape")
    return out


def fly_running_machine_ids(flyctl: Path, app: str, env: dict[str, str]) -> tuple[list[str], bool]:
    cp = run(
        [str(flyctl), "machine", "list", "-a", app, "--json"],
        as_dev=True, env=env, check=False, timeout=60,
    )
    if cp.returncode != 0:
        return [], False
    try:
        data = parse_json_output(cp)
    except Exception:
        return [], False
    items = data if isinstance(data, list) else []
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or item.get("State") or "").lower()
        machine_id = item.get("id") or item.get("ID")
        if state in {"started", "running"} and isinstance(machine_id, str) and machine_id:
            out.append(machine_id)
    return out, True


def fly_runtime_env(
    flyctl: Path, app: str, env: dict[str, str], machine_ids: list[str]
) -> tuple[dict[str, str], str | None, bool]:
    # Fly documents that an SSH console inherits the application runtime
    # environment, including secrets. Do not inspect /proc/1/environ: PID 1 may
    # be an init/supervisor rather than the app process and can have a different
    # environment. First use NUL-separated env for byte-safe parsing, then fall
    # back to plain printenv for minimal images that lack env -0.
    any_ssh_success = False
    for machine_id in machine_ids:
        for remote_command, method in (("env -0", "env0"), ("printenv", "printenv")):
            cp = run(
                [str(flyctl), "ssh", "console", "-q", "-a", app, "--machine", machine_id,
                 "--pty=false", "-C", remote_command],
                as_dev=True, env=env, check=False, timeout=90, binary=True,
            )
            if cp.returncode != 0:
                continue
            any_ssh_success = True
            parsed = parse_env_bytes(cp.stdout or b"")
            if parsed:
                return parsed, method, True
    return {}, None, any_ssh_success


def write_private_env(
    path: Path, values: dict[str, str], mode: int = 0o600, *, generated: bool = False
) -> None:
    lines: list[str] = []
    if generated:
        lines.extend([
            "# GENERATED FROM /home/dev/.env — DO NOT EDIT.",
            "# Edit /home/dev/.env only; this file is regenerated automatically.",
            "",
        ])
    for key in sorted(values):
        value = values[key]
        lines.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", errors="surrogateescape")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def recover_fly(
    project_info: dict[str, dict[str, Any]],
    *,
    allow_install: bool,
    allow_prompt: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "authenticated": False,
        "apps": {},
    }
    flyctl = find_flyctl() or (install_flyctl() if allow_install else None)
    if not flyctl:
        result["reason"] = "flyctl_missing"
        return result
    result["available"] = True
    result["flyctl"] = str(flyctl)

    auth = maybe_fly_auth(flyctl, allow_prompt=allow_prompt)
    if auth is None:
        result["reason"] = "fly_auth_missing_using_recovered_snapshots"
        app_to_project = dict(FLY_APP_PROJECT_MAP)
        app_to_project.update({
            info["fly_app"]: project
            for project, info in project_info.items()
            if info.get("fly_app")
        })
        if RECOVERED.is_dir():
            for snapshot in sorted(RECOVERED.glob("*.env")):
                app = snapshot.stem
                vals = parse_master(snapshot)
                names = sorted(vals)
                result["apps"][app] = {
                    "project": app_to_project.get(app),
                    "secret_list_ok": False,
                    "machine_list_ok": False,
                    "running_machine_count": 0,
                    "ssh_ok": False,
                    "ssh_method": "recovered-snapshot",
                    "secret_names": names,
                    "recovered_names": names,
                    "missing_runtime_names": [],
                    "value_sha256": {
                        name: hashlib.sha256(
                            vals[name].encode("utf-8", errors="surrogateescape")
                        ).hexdigest()
                        for name in names
                    },
                }
        return result
    result["authenticated"] = True

    app_to_project = dict(FLY_APP_PROJECT_MAP)
    app_to_project.update({
        info["fly_app"]: project
        for project, info in project_info.items()
        if info.get("fly_app")
    })

    apps = fly_apps(flyctl, auth)
    safe_mkdir(RECOVERED, 0o700)
    for app in apps:
        secret_names, secret_list_ok = fly_secret_names(flyctl, app, auth)
        machine_ids, machine_list_ok = fly_running_machine_ids(flyctl, app, auth)
        runtime: dict[str, str] = {}
        ssh_method: str | None = None
        ssh_ok = False
        if secret_names and machine_ids:
            runtime, ssh_method, ssh_ok = fly_runtime_env(flyctl, app, auth, machine_ids)
        recovered = {
            name: runtime[name]
            for name in sorted(secret_names)
            if name in runtime
        }
        missing_runtime = sorted(secret_names - set(recovered))
        if recovered:
            write_private_env(RECOVERED / f"{app}.env", recovered, 0o600)
        result["apps"][app] = {
            "project": app_to_project.get(app),
            "secret_list_ok": secret_list_ok,
            "machine_list_ok": machine_list_ok,
            "running_machine_count": len(machine_ids),
            "ssh_ok": ssh_ok,
            "ssh_method": ssh_method,
            "secret_names": sorted(secret_names),
            "recovered_names": sorted(recovered),
            "missing_runtime_names": missing_runtime,
            # Hashes permit safe cross-app dedupe without exposing values.
            "value_sha256": {
                name: hashlib.sha256(recovered[name].encode("utf-8", errors="surrogateescape")).hexdigest()
                for name in recovered
            },
        }
    return result


def parse_master(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not ENV_NAME.fullmatch(key):
            continue
        value = raw_value.strip()
        if not value:
            out[key] = ""
            continue
        try:
            parsed = json.loads(value)
            out[key] = parsed if isinstance(parsed, str) else value
        except Exception:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            out[key] = value
    return out


def pfx(project: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", project.upper()).strip("_")


def load_recovered_env(app: str) -> dict[str, str]:
    return parse_master(RECOVERED / f"{app}.env")


def build_catalog(project_info: dict[str, dict[str, Any]], fly: dict[str, Any]) -> dict[str, Any]:
    usage: dict[str, set[str]] = defaultdict(set)
    actual_names: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    all_projects = dict(project_info)

    for project, info in project_info.items():
        for actual in info["required"]:
            canon = canonical(actual)
            usage[canon].add(project)
            actual_names[canon][project].add(actual)

    # Sweep every Fly app visible to the authenticated account. Apps without
    # a local repository on DevCoveer2 still get a stable pseudo-project, so
    # kgd80/cat-weather/old services are not silently dropped from master.env.
    for app, app_info in fly.get("apps", {}).items():
        project = app_info.get("project")
        if not project:
            project = f"flyapp-{app}"
            app_info["project"] = project
        all_projects.setdefault(project, {
            "project": project,
            "path": None,
            "fly_app": app,
            "env_examples": [],
            "required": sorted(app_info.get("secret_names", [])),
            "source_controlled_env": [],
            "scope_kind": "fly_app",
        })
        for actual in app_info.get("secret_names", []):
            canon = canonical(actual)
            usage[canon].add(project)
            actual_names[canon][project].add(actual)

    variables: dict[str, Any] = {}
    for canon in sorted(usage):
        projects = sorted(usage[canon])
        variables[canon] = {
            "canonical_name": canon,
            "sensitive": secret_like(canon),
            "owner_managed": owner_managed_credential(canon),
            "projects": projects,
            "runtime_names": {
                project: sorted(actual_names[canon][project])
                for project in projects
            },
            "shared_candidate": len(projects) > 1,
        }

    return {
        "schema": "devcoveer.env-catalog.v1",
        "aliases": ALIASES,
        "projects": all_projects,
        "variables": variables,
        "fly": fly,
    }


REF_PREFIX = "@ref:"


def resolve_legacy_owner_values(values: dict[str, str]) -> dict[str, str]:
    """Resolve old @ref aliases during one-time migration only.

    The new /home/dev/.env never writes @ref entries.
    """
    resolved: dict[str, str] = {}

    def one(key: str, stack: tuple[str, ...] = ()) -> str:
        if key in resolved:
            return resolved[key]
        if key in stack:
            raise ValueError("legacy owner reference cycle: " + " -> ".join((*stack, key)))
        value = values.get(key, "")
        if value.startswith(REF_PREFIX):
            target = value[len(REF_PREFIX):]
            if target not in values:
                raise ValueError(f"legacy owner reference target missing: {target}")
            value = one(target, (*stack, key))
        resolved[key] = value
        return value

    for key in values:
        one(key)
    return resolved


def _load_existing_owner() -> dict[str, str]:
    owner = parse_master(OWNER)

    # One-time migration from the root-only owner.env design.
    if LEGACY_ROOT_OWNER.is_file() and not LEGACY_ROOT_OWNER.is_symlink():
        for key, value in parse_master(LEGACY_ROOT_OWNER).items():
            if value and not owner.get(key):
                owner[key] = value

    # One-time migration from the previous master.env design.
    if LEGACY_MASTER.is_file() and not LEGACY_MASTER.is_symlink():
        for key, value in parse_master(LEGACY_MASTER).items():
            if value and not owner.get(key):
                owner[key] = value

    # If the owner already filled the short-lived owner-fill file, do not lose it.
    if LEGACY_OWNER_FILL.is_file() and not LEGACY_OWNER_FILL.is_symlink():
        for key, value in parse_master(LEGACY_OWNER_FILL).items():
            if value and not owner.get(key):
                owner[key] = value
    if owner:
        owner = resolve_legacy_owner_values(owner)
    return owner


def _ensure_compat_links() -> None:
    """Keep old /etc paths as symlinks; /home/dev/.env is the only real file."""
    if os.path.lexists(LEGACY_ROOT_OWNER):
        if LEGACY_ROOT_OWNER.is_symlink() and os.readlink(LEGACY_ROOT_OWNER) == str(OWNER):
            pass
        else:
            backup = ROOT / "owner.env.pre-dev-migration"
            if LEGACY_ROOT_OWNER.is_file() and not LEGACY_ROOT_OWNER.is_symlink() and not backup.exists():
                shutil.copy2(LEGACY_ROOT_OWNER, backup)
                os.chmod(backup, 0o600)
            LEGACY_ROOT_OWNER.unlink()
            os.symlink(str(OWNER), LEGACY_ROOT_OWNER)
    else:
        os.symlink(str(OWNER), LEGACY_ROOT_OWNER)

    if os.path.lexists(LEGACY_MASTER):
        if LEGACY_MASTER.is_symlink() and os.readlink(LEGACY_MASTER) == "owner.env":
            return
        backup = ROOT / "master.env.pre-dev-migration"
        if LEGACY_MASTER.is_file() and not LEGACY_MASTER.is_symlink() and not backup.exists():
            shutil.copy2(LEGACY_MASTER, backup)
            os.chmod(backup, 0o600)
        LEGACY_MASTER.unlink()
    os.symlink("owner.env", LEGACY_MASTER)


def _cleanup_legacy_owner_fill() -> None:
    for path in (LEGACY_OWNER_FILL, LEGACY_OWNER_FILL_SUMMARY):
        if path.exists() and not path.is_symlink():
            backup = path.with_name(path.name + ".deprecated")
            if not backup.exists():
                shutil.move(path, backup)
            else:
                path.unlink()


def _split_owner_key(key: str) -> tuple[str | None, str]:
    """Return (project_prefix, canonical_name) for old/new owner keys."""
    if key.startswith("SHARED__"):
        return None, canonical(key[len("SHARED__"):])
    if "__" in key:
        prefix, name = key.split("__", 1)
        return prefix, canonical(name)
    return None, canonical(key)


def merge_master(catalog: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Build the compact owner-input credential file.

    Only externally maintained credentials belong in /home/dev/.env.
    Temporary project-prefixed values from earlier migration attempts are not
    treated as evidence because the old project env layout contained cross-project
    references. Factual recovered runtime values are the conflict evidence.
    """
    previous = _load_existing_owner()
    notes: dict[str, str] = {}

    # Preserve only flat/current owner choices. Temporary SHARED__ entries are
    # migrated to their canonical flat name; project-prefixed migration artifacts
    # are deliberately not trusted as factual evidence.
    flat_existing: dict[str, str] = {}
    for key, value in previous.items():
        prefix, canon = _split_owner_key(key)
        if prefix is not None or not owner_managed_credential(canon):
            continue
        if value or canon not in flat_existing:
            flat_existing[canon] = value

    recovered_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for app, info in catalog.get("fly", {}).get("apps", {}).items():
        project = info.get("project")
        if not project:
            continue
        for actual, value in load_recovered_env(app).items():
            canon = canonical(actual)
            if owner_managed_credential(canon) and value:
                recovered_sets[(project, canon)].add(value)

    owner: dict[str, str] = {}
    for canon, meta in sorted(catalog["variables"].items()):
        if not meta.get("owner_managed"):
            continue
        projects = list(meta.get("projects") or [])
        direct = flat_existing.get(canon, "")

        observed: dict[str, str] = {}
        alias_conflict = False
        for project in projects:
            values = recovered_sets.get((project, canon), set())
            if len(values) == 1:
                observed[project] = next(iter(values))
            elif len(values) > 1:
                alias_conflict = True

        unique = set(observed.values())

        if alias_conflict:
            owner[canon] = direct
            notes[canon] = "alias-runtime-conflict"
            for project, value in sorted(observed.items()):
                if direct and value == direct:
                    continue
                owner[f"{pfx(project)}__{canon}"] = value
            continue

        if direct:
            owner[canon] = direct
            # Only factual runtime differences survive as project overrides.
            for project, value in sorted(observed.items()):
                if value != direct:
                    owner[f"{pfx(project)}__{canon}"] = value
                    notes[f"{pfx(project)}__{canon}"] = "runtime-conflict"
            continue

        if len(unique) == 1:
            owner[canon] = next(iter(unique)) if unique else ""
            continue

        if len(unique) > 1:
            owner[canon] = ""
            notes[canon] = "runtime-conflict"
            for project, value in sorted(observed.items()):
                owner[f"{pfx(project)}__{canon}"] = value
            continue

        owner[canon] = ""

    # A newly added flat owner credential is preserved even before source
    # discovery learns about it, provided it is an owner-managed credential.
    catalog_canons = set(catalog["variables"])
    for canon, value in flat_existing.items():
        if canon not in catalog_canons and owner_managed_credential(canon):
            owner.setdefault(canon, value)

    return owner, notes


def observed_equal_credential_names() -> list[tuple[str, ...]]:
    """Return name groups that held the same owner-managed secret in recovery.

    This is informational only. It does not silently merge different semantic
    variable names. Values are compared by SHA-256 and are never printed.
    """
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    if not RECOVERED.is_dir():
        return []
    for snapshot in sorted(RECOVERED.glob("*.env")):
        for actual, value in parse_master(snapshot).items():
            if not value or not owner_managed_credential(actual):
                continue
            family = credential_family(actual)
            digest = hashlib.sha256(
                value.encode("utf-8", errors="surrogateescape")
            ).hexdigest()
            groups[(family, digest)].add(actual)
    out = {
        tuple(sorted(names))
        for names in groups.values()
        if len(names) > 1
    }
    return sorted(out)


def known_alias_pairs() -> list[tuple[str, str]]:
    return sorted((alias, target) for alias, target in ALIASES.items())


def _owner_usage_comment(key: str, catalog: dict[str, Any]) -> str | None:
    # Keep the owner-facing file compact. Section headers are the navigation.
    return None


def write_master(
    master: dict[str, str], notes: dict[str, str], catalog: dict[str, Any] | None = None
) -> None:
    sections: dict[str, list[str]] = defaultdict(list)
    for key in master:
        _prefix, canon = _split_owner_key(key)
        sections[credential_family(canon)].append(key)

    preferred = [
        "GOOGLE AI", "NVIDIA", "GIGACHAT", "GROQ", "OPENAI / 4O", "OPENAI / CODEX",
        "KAGGLE", "TELEGRAM", "VK", "MAX",
        "SUPABASE", "YDB", "YANDEX CLOUD / S3",
        "GITHUB", "FLY.IO", "TUNNEL / CLOUDFLARE",
        "POSTBOX", "NOTISEND", "MAIL / IMAP / SMTP", "TELEGRAPH", "SPACEWEB", "PINTEREST",
        "OAUTH / MCP", "DATABASES", "SERVICE ACCOUNTS / SIGNING",
    ]
    ordered = [s for s in preferred if s in sections]
    ordered += sorted(s for s in sections if s not in preferred)

    lines = [
        "# DEVCOVEER CREDENTIALS — SINGLE SOURCE OF TRUTH",
        "# Edit only this file. Common credentials are stored once by their real env name.",
        "# PROJECT__VARIABLE exists only when recovered runtimes prove a real value conflict.",
        "# No feature flags/settings belong here.",
        "",
    ]
    for section in ordered:
        lines.extend([
            "# ============================================================================",
            f"# {section}",
            "# ============================================================================",
        ])
        for key in sorted(sections[section], key=lambda k: ("__" in k, k)):
            value = master[key]
            if notes.get(key) == "runtime-conflict":
                lines.append("# CONFLICT: recovered runtimes disagree; review this credential.")
            elif notes.get(key) == "alias-runtime-conflict":
                lines.append("# CONFLICT: known alias names had different runtime values; review.")
            lines.append(f"{key}={json.dumps(value, ensure_ascii=False) if value else ''}")
        lines.append("")

    aliases = known_alias_pairs()
    observed_equal = observed_equal_credential_names()
    if aliases or observed_equal:
        lines.extend([
            "# ============================================================================",
            "# ALIASES / COMPATIBILITY NAMES",
            "# ============================================================================",
            "# These are comments only. Secret values are stored once above.",
        ])
        for alias, target in aliases:
            lines.append(f"# {alias} = {target}")
        if observed_equal:
            lines.append("#")
            lines.append("# Same secret value observed in recovered runtime; informational only:")
            for names in observed_equal:
                lines.append("# " + " = ".join(names))
        lines.append("")

    tmp = OWNER.with_name(OWNER.name + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    import pwd
    pw = pwd.getpwnam(DEV)
    os.chown(tmp, pw.pw_uid, pw.pw_gid)
    os.chmod(tmp, 0o600)
    os.replace(tmp, OWNER)
    os.chown(OWNER, pw.pw_uid, pw.pw_gid)
    os.chmod(OWNER, 0o600)
    _ensure_compat_links()


def write_todo(master: dict[str, str], notes: dict[str, str]) -> None:
    lines = [
        "# GENERATED VIEW OF MISSING CREDENTIALS — DO NOT EDIT.",
        "# Fill the corresponding blank directly in /home/dev/.env.",
        "",
    ]
    for key in sorted(k for k, value in master.items() if not value):
        note = notes.get(key)
        if note:
            lines.append(f"# {note}")
        lines.append(f"{key}=")
    TODO.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(TODO, 0o600)


def install_render_watcher() -> None:
    """Keep generated service env files synchronized with owner.env."""
    service = Path("/etc/systemd/system/devcoveer-env-render.service")
    path_unit = Path("/etc/systemd/system/devcoveer-env-render.path")
    script = Path(__file__).resolve()
    service.write_text(
        "[Unit]\nDescription=Render DevCoveer service envs from /home/dev/.env\n\n"
        "[Service]\nType=oneshot\n"
        f"ExecStart=/usr/bin/python3 {script} render\n"
        "UMask=0027\n",
        encoding="utf-8",
    )
    path_unit.write_text(
        "[Unit]\nDescription=Watch DevCoveer /home/dev/.env credentials\n\n"
        "[Path]\nPathChanged=/home/dev/.env\n"
        "Unit=devcoveer-env-render.service\n\n"
        "[Install]\nWantedBy=multi-user.target\n",
        encoding="utf-8",
    )
    os.chmod(service, 0o644)
    os.chmod(path_unit, 0o644)
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "enable", "--now", "devcoveer-env-render.path"], check=False)


def render(catalog: dict[str, Any] | None = None) -> dict[str, int]:
    if catalog is None:
        if not CATALOG.is_file():
            raise SystemExit("catalog.json is missing; run recover first")
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    owner = parse_master(OWNER)
    safe_mkdir(SERVICES, 0o750)

    import grp
    try:
        gid = grp.getgrnam("dev").gr_gid
    except KeyError:
        gid = os.stat(DEV_HOME).st_gid

    counts: dict[str, int] = {}
    for project, info in catalog["projects"].items():
        values: dict[str, str] = {}
        for canon, meta in catalog["variables"].items():
            if project not in meta["projects"] or not meta["sensitive"]:
                continue
            common = owner.get(canon, "")
            override = owner.get(f"{pfx(project)}__{canon}", "")
            selected = override or common
            if not selected:
                continue
            for actual in meta["runtime_names"].get(project, [canon]):
                values[actual] = selected

        # Owner-added project credentials are supported immediately even before
        # source scanning learns about the new variable.
        prefix = f"{pfx(project)}__"
        for owner_key, value in owner.items():
            if value and owner_key.startswith(prefix):
                runtime_name = owner_key[len(prefix):]
                if ENV_NAME.fullmatch(runtime_name):
                    values.setdefault(runtime_name, value)

        target = SERVICES / f"{project}.env"
        write_private_env(target, values, 0o640, generated=True)
        os.chown(target, 0, gid)
        counts[project] = len(values)

    write_todo(owner, {})
    return counts

def write_readme() -> None:
    README.write_text(
        """DevCoveer2 credential layout

/home/dev/.env
  The only human-edited credential file. dev:dev 0600.
  Common credentials keep their normal env names and exist once.

/etc/devcoveer/env/owner.env
/etc/devcoveer/env/master.env
  Compatibility symlinks to /home/dev/.env.

/etc/devcoveer/env/recovered/fly/*.env
  Read-only recovery snapshots captured from real Fly runtimes.

/etc/devcoveer/env/services/<project>.env
  Generated runtime views. Never edit them.

Rules
  VARIABLE=value
    Normal/common credential used wherever that runtime variable is required.

  PROJECT__VARIABLE=value
    Exists only when recovered runtimes prove that the same variable name had
    different values in different projects. It overrides VARIABLE for that project.

  No @ref aliases, no SHARED__ namespace, no duplicated common credentials.
  Feature flags, paths, timeouts, endpoints and ordinary application settings
  do not belong in /home/dev/.env.

Saving /home/dev/.env regenerates services/*.env. A running process still needs
a controlled restart/reload to receive a changed environment value in memory.
""",
        encoding="utf-8",
    )
    os.chmod(README, 0o600)


def recover(args: argparse.Namespace) -> int:
    ensure_root()
    safe_mkdir(ROOT, 0o700)
    safe_mkdir(RECOVERED, 0o700)
    safe_mkdir(SERVICES, 0o750)

    projects = project_dirs()
    info = {name: scan_project(name, path) for name, path in projects.items()}

    fly = recover_fly(
        info,
        allow_install=not args.no_install_flyctl,
        allow_prompt=not args.no_prompt,
    )
    catalog = build_catalog(info, fly)
    CATALOG.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(CATALOG, 0o600)

    owner, notes = merge_master(catalog)
    write_master(owner, notes, catalog)
    counts = render(catalog)
    write_todo(owner, notes)
    _cleanup_legacy_owner_fill()
    install_render_watcher()
    write_readme()

    recovered_total = sum(
        len(x.get("recovered_names", []))
        for x in fly.get("apps", {}).values()
    )
    missing_total = sum(1 for value in owner.values() if not value)
    credential_total = sum(
        1 for x in catalog["variables"].values() if x["sensitive"]
    )

    print("ENV_RECOVERY_COMPLETE")
    print(f"projects={len(projects)}")
    print(f"credential_variable_families={credential_total}")
    print(f"fly_available={fly.get('available', False)}")
    print(f"fly_authenticated={fly.get('authenticated', False)}")
    print(f"fly_apps_seen={len(fly.get('apps', {}))}")
    print(f"fly_secret_values_recovered={recovered_total}")
    app_diag = {
        app: {
            "secret_names": len(app_info.get("secret_names", [])),
            "running_machines": int(app_info.get("running_machine_count", 0)),
            "secret_list_ok": bool(app_info.get("secret_list_ok", False)),
            "machine_list_ok": bool(app_info.get("machine_list_ok", False)),
            "ssh_ok": bool(app_info.get("ssh_ok", False)),
            "ssh_method": app_info.get("ssh_method"),
            "recovered": len(app_info.get("recovered_names", [])),
            "missing": len(app_info.get("missing_runtime_names", [])),
        }
        for app, app_info in sorted(fly.get("apps", {}).items())
    }
    print("fly_app_recovery=" + json.dumps(app_diag, sort_keys=True))
    owner_sections: dict[str, int] = defaultdict(int)
    runtime_conflicts = 0
    for key in owner:
        _prefix, canon = _split_owner_key(key)
        owner_sections[credential_family(canon)] += 1
        if "__" in key:
            runtime_conflicts += 1
    print(f"owner_missing_values={missing_total}")
    print(f"runtime_conflict_overrides={runtime_conflicts}")
    print("owner_sections=" + json.dumps(dict(sorted(owner_sections.items())), sort_keys=True))
    print(f"owner={OWNER}")
    print(f"master_compat={LEGACY_MASTER}")
    print(f"todo={TODO}")
    print(f"catalog={CATALOG}")
    print(f"service_envs={SERVICES}")
    print("rendered_project_counts=" + json.dumps(counts, sort_keys=True))
    return 0


def render_cmd(_: argparse.Namespace) -> int:
    ensure_root()
    counts = render()
    print("ENV_RENDER_COMPLETE")
    print("rendered_project_counts=" + json.dumps(counts, sort_keys=True))
    print(f"owner={OWNER}")
    print(f"todo={TODO}")
    return 0

def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_recover = sub.add_parser("recover")
    p_recover.add_argument("--no-install-flyctl", action="store_true")
    p_recover.add_argument("--no-prompt", action="store_true")
    p_recover.set_defaults(func=recover)
    p_render = sub.add_parser("render")
    p_render.set_defaults(func=render_cmd)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
