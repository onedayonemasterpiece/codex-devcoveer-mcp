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
    "TG_API_ID": "TELEGRAM_API_ID",
    "TG_API_HASH": "TELEGRAM_API_HASH",
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
    return bool(SENSITIVE.search(name))


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


def read_release_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not FLY_RELEASE_ENV.is_file() or FLY_RELEASE_ENV.is_symlink():
        return values
    try:
        text = FLY_RELEASE_ENV.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key in {"FLY_ACCESS_TOKEN", "FLY_API_TOKEN"} and value:
            values[key] = value
    return values


def fly_env() -> dict[str, str]:
    out = read_release_env()
    # An inherited token is useful but never persisted unless explicitly entered.
    for key in ("FLY_ACCESS_TOKEN", "FLY_API_TOKEN"):
        if os.environ.get(key):
            out[key] = os.environ[key]
    return out


def fly_whoami(flyctl: Path, env: dict[str, str]) -> bool:
    cp = run([str(flyctl), "auth", "whoami"], as_dev=True, env=env, check=False, timeout=30)
    return cp.returncode == 0


def persist_fly_token(token: str, flyctl: Path) -> dict[str, str] | None:
    candidate = {"FLY_ACCESS_TOKEN": token}
    if not fly_whoami(flyctl, candidate):
        candidate = {"FLY_API_TOKEN": token}
        if not fly_whoami(flyctl, candidate):
            return None
    safe_mkdir(FLY_RELEASE_ENV.parent, 0o700, owner_dev=True)
    key = next(iter(candidate))
    tmp = FLY_RELEASE_ENV.with_suffix(".tmp")
    tmp.write_text(f"{key}={json.dumps(token)}\n", encoding="utf-8")
    import pwd
    pw = pwd.getpwnam(DEV)
    os.chown(tmp, pw.pw_uid, pw.pw_gid)
    os.chmod(tmp, 0o600)
    os.replace(tmp, FLY_RELEASE_ENV)
    return candidate


def recover_fly_token_from_history(flyctl: Path) -> dict[str, str] | None:
    """Search bounded user-local history for a previously used Fly token.

    The candidate value is never printed. Every candidate is validated only via
    flyctl auth whoami before it is persisted to release.env.
    """
    token_re = re.compile(rb"FlyV1[ \\t]+[A-Za-z0-9_=.\\-]{20,}")
    roots = [
        DEV_HOME / ".codex" / "sessions",
        DEV_HOME / ".codex",
        DEV_HOME / ".claude" / "projects",
        DEV_HOME / ".claude" / "file-history",
    ]
    seen: set[bytes] = set()
    inspected = 0
    for root in roots:
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else root.rglob("*")
        for p in candidates:
            if inspected >= 600:
                return None
            if not p.is_file() or p.is_symlink():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size <= 0 or size > 128 * 1024 * 1024:
                continue
            inspected += 1
            try:
                data = p.read_bytes()
            except OSError:
                continue
            for match in token_re.findall(data):
                token_b = re.sub(rb"[ \\t]+", b" ", match.strip())
                if token_b in seen:
                    continue
                seen.add(token_b)
                try:
                    token = token_b.decode("ascii")
                except UnicodeError:
                    continue
                validated = persist_fly_token(token, flyctl)
                if validated is not None:
                    return validated
    return None


def maybe_fly_auth(flyctl: Path, *, allow_prompt: bool) -> dict[str, str] | None:
    """Use only the normal full-account flyctl cached login.

    Authentication is intentionally a separate human step (fly auth login).
    Do not inject historical/release tokens here: scope-limited tokens can make
    fly apps list silently incomplete, which is unacceptable for recovery.
    """
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
            "# GENERATED FROM /etc/devcoveer/env/owner.env — DO NOT EDIT.",
            "# Edit owner.env only; this file is regenerated automatically.",
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
        result["reason"] = "fly_auth_missing_run_fly_auth_login_as_dev"
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


def resolve_owner_values(values: dict[str, str]) -> dict[str, str]:
    """Resolve owner @ref: aliases without mutating the editable file."""
    resolved: dict[str, str] = {}

    def one(key: str, stack: tuple[str, ...] = ()) -> str:
        if key in resolved:
            return resolved[key]
        if key in stack:
            raise ValueError("owner credential reference cycle: " + " -> ".join((*stack, key)))
        value = values.get(key, "")
        if value.startswith(REF_PREFIX):
            target = value[len(REF_PREFIX):]
            if target not in values:
                raise ValueError(f"owner credential reference target missing: {target}")
            value = one(target, (*stack, key))
        resolved[key] = value
        return value

    for key in values:
        one(key)
    return resolved


def dedupe_owner_values(
    values: dict[str, str], notes: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Replace exact duplicate credential values with aliases to one owner key."""
    expanded = resolve_owner_values(values)
    by_value: dict[str, list[str]] = defaultdict(list)
    for key, value in expanded.items():
        if value:
            by_value[value].append(key)

    result = dict(expanded)
    for value, keys in by_value.items():
        if len(keys) < 2:
            continue
        primary = sorted(
            keys,
            key=lambda k: (0 if k.startswith("SHARED__") else 1, len(k), k),
        )[0]
        for key in keys:
            if key == primary:
                continue
            result[key] = REF_PREFIX + primary
            notes[key] = f"alias-of:{primary}"
    return result, notes

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
        owner = resolve_owner_values(owner)
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


def merge_master(catalog: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Build the single editable owner credential store.

    The owner file contains credentials only. Proven identical credentials are
    deduplicated into SHARED__ entries. Feature flags / ordinary config are
    intentionally absent and remain source-controlled or service-specific config.
    """
    master = _load_existing_owner()
    notes: dict[str, str] = {}

    recovered_by_project: dict[tuple[str, str], str] = {}
    for app, info in catalog.get("fly", {}).get("apps", {}).items():
        project = info.get("project")
        if not project:
            continue
        for actual, value in load_recovered_env(app).items():
            canon = canonical(actual)
            if not secret_like(canon):
                continue
            recovered_by_project[(project, canon)] = value

    allowed_canons = {
        canon for canon, meta in catalog["variables"].items() if meta.get("sensitive")
    }

    # Remove historical flag/config entries accidentally admitted by the older
    # "every Fly secret name is sensitive" rule.
    for key in list(master):
        if "__" not in key:
            master.pop(key, None)
            continue
        canon = key.split("__", 1)[1]
        if canon not in allowed_canons:
            master.pop(key, None)

    for canon in sorted(allowed_canons):
        meta = catalog["variables"][canon]
        projects = list(meta["projects"])
        shared_key = f"SHARED__{canon}"
        project_keys = {project: f"{pfx(project)}__{canon}" for project in projects}
        recovered = {
            project: recovered_by_project[(project, canon)]
            for project in projects
            if (project, canon) in recovered_by_project
        }

        if len(projects) == 1:
            project = projects[0]
            key = project_keys[project]
            master.pop(shared_key, None)
            if recovered.get(project) is not None and not master.get(key):
                master[key] = recovered[project]
                notes[key] = "recovered-from-fly-runtime"
            else:
                master.setdefault(key, "")
                notes.setdefault(key, "owner-value" if master.get(key) else "missing")
            continue

        # If the owner explicitly has a shared value, preserve that decision.
        shared_value = master.get(shared_key, "")
        if shared_value:
            notes[shared_key] = "shared-owner-source-of-truth"
            for project, key in project_keys.items():
                recovered_value = recovered.get(project)
                existing = master.get(key, "")
                if recovered_value is not None and recovered_value != shared_value:
                    if not existing:
                        master[key] = recovered_value
                    notes[key] = "project-override-recovered-different"
                elif existing == shared_value or not existing:
                    master.pop(key, None)
            continue

        recovered_values = list(recovered.values())
        recovered_unique = set(recovered_values)
        existing_project_values = {
            project: master.get(key, "")
            for project, key in project_keys.items()
            if master.get(key, "")
        }

        # Two or more independent runtimes with the same value are strong
        # evidence of a truly shared credential. Store it exactly once.
        if len(recovered_values) >= 2 and len(recovered_unique) == 1:
            shared_value = recovered_values[0]
            master[shared_key] = shared_value
            notes[shared_key] = "recovered-identical-across-projects"
            for project, key in project_keys.items():
                existing = master.get(key, "")
                recovered_value = recovered.get(project)
                if existing and existing != shared_value:
                    notes[key] = "project-override-owner-value"
                    continue
                if recovered_value is not None and recovered_value != shared_value:
                    master[key] = recovered_value
                    notes[key] = "project-override-recovered-different"
                else:
                    master.pop(key, None)
            continue

        # No evidence yet: expose one shared blank. Filling it in owner.env is
        # the owner explicitly declaring the credential shared. A project-specific
        # override can still be added later as PROJECT__NAME.
        if not recovered and not existing_project_values:
            master[shared_key] = ""
            notes[shared_key] = "shared-candidate-unconfirmed"
            for key in project_keys.values():
                if not master.get(key, ""):
                    master.pop(key, None)
            continue

        # One recovered value or conflicting values cannot safely be shared.
        master.pop(shared_key, None)
        for project, key in project_keys.items():
            if master.get(key):
                notes[key] = "project-owner-value"
                continue
            if project in recovered:
                master[key] = recovered[project]
                notes[key] = "recovered-from-fly-runtime"
            else:
                master.setdefault(key, "")
                notes[key] = "missing"

    master, notes = dedupe_owner_values(master, notes)
    return master, notes


def _owner_usage_comment(key: str, catalog: dict[str, Any]) -> str | None:
    if "__" not in key:
        return None
    prefix, canon = key.split("__", 1)
    meta = catalog.get("variables", {}).get(canon)
    if not isinstance(meta, dict):
        return None
    projects = list(meta.get("projects") or [])
    if prefix == "SHARED":
        return "used by: " + ", ".join(sorted(projects))
    for project in projects:
        if pfx(project) == prefix:
            runtime_names = meta.get("runtime_names", {}).get(project, [canon])
            return f"project: {project}; runtime: {', '.join(runtime_names)}"
    return None


def write_master(
    master: dict[str, str], notes: dict[str, str], catalog: dict[str, Any] | None = None
) -> None:
    groups: dict[str, list[str]] = defaultdict(list)
    for key in master:
        group = "SHARED" if key.startswith("SHARED__") else key.split("__", 1)[0]
        groups[group].append(key)

    lines = [
        "# DEVCOVEER OWNER CREDENTIALS — SINGLE SOURCE OF TRUTH.",
        "# DEV-EDITABLE (dev:dev 0600). EDIT THIS FILE AND NO OTHER ENV FILE.",
        "# Contains credentials only: tokens, API keys, logins/usernames, passwords,",
        "# sessions/auth bundles, database DSNs, signing/private/service-account keys.",
        "# Feature flags, =0/=1, true/false switches and ordinary runtime config do not belong here.",
        "# /etc/devcoveer/env/owner.env and master.env are compatibility symlinks here.",
        "# services/*.env are generated automatically and MUST NOT be edited.",
        "# @ref:OTHER_KEY means this line reuses the exact same credential value;",
        "# rotate the value only at OTHER_KEY and every generated env follows it.",
        "",
    ]
    for group in sorted(groups):
        lines.append(f"# [{group}]")
        for key in sorted(groups[group]):
            if catalog is not None:
                usage = _owner_usage_comment(key, catalog)
                if usage:
                    lines.append(f"# {usage}")
            note = notes.get(key)
            if note:
                lines.append(f"# source: {note}")
            value = master[key]
            lines.append(f"{key}={json.dumps(value, ensure_ascii=False) if value else ''}")
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
        "[Unit]\nDescription=Render DevCoveer service envs from owner.env\n\n"
        "[Service]\nType=oneshot\n"
        f"ExecStart=/usr/bin/python3 {script} render\n"
        "UMask=0027\n",
        encoding="utf-8",
    )
    path_unit.write_text(
        "[Unit]\nDescription=Watch DevCoveer owner credentials\n\n"
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
    owner_raw = parse_master(OWNER)
    try:
        owner = resolve_owner_values(owner_raw)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
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
            shared = owner.get(f"SHARED__{canon}", "")
            override = owner.get(f"{pfx(project)}__{canon}", "")
            selected = override or shared
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

.env (/home/dev/.env)
  The ONLY human-editable credential file. Owned by dev:dev, mode 0600.
  Tokens, API keys, logins/usernames, passwords, sessions/auth bundles, DSNs,
  private/signing/service-account credentials live here.

/etc/devcoveer/env/owner.env and master.env
  Compatibility symlinks to /home/dev/.env. They are not second sources of truth.

todo.env
  Generated missing-credential view. Never edit it.

catalog.json
  Generated mapping from canonical owner credentials to runtime variable names
  and projects. It contains no recovered plaintext values.

recovered/fly/*.env
  Root-only recovery evidence from Fly. Never edit or consume directly.

services/<project>.env
  Generated least-privilege runtime env files, root:dev 0640. Never edit.

Editing /home/dev/.env as user dev automatically regenerates services/*.env via the systemd
devcoveer-env-render.path watcher. Running processes still require a controlled
restart/reload before a changed environment value takes effect in memory.

Resolution:
  PROJECT__VARIABLE overrides SHARED__VARIABLE.
  A SHARED value is created automatically only from proven identical recovered
  credentials, or when the owner explicitly fills a shared blank.
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
    print(f"owner_missing_values={missing_total}")
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
