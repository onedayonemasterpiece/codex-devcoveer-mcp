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
MASTER = ROOT / "master.env"
TODO = ROOT / "todo.env"
CATALOG = ROOT / "catalog.json"
RECOVERED = ROOT / "recovered" / "fly"
SERVICES = ROOT / "services"
README = ROOT / "README.txt"
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
    r"BEARER|OAUTH|ACCESS_KEY|SERVICE_ACCOUNT)(?:_|$)"
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
    """Use only the normal flyctl login/config state.

    Authentication is intentionally a separate human step (fly auth login).
    Recovery must not scrape old session history or ask for raw Fly tokens.
    """
    del allow_prompt
    env = fly_env()
    if fly_whoami(flyctl, env):
        return env
    # flyctl may have a normal cached login that is not represented in env.
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


def fly_secret_names(flyctl: Path, app: str, env: dict[str, str]) -> set[str]:
    cp = run(
        [str(flyctl), "secrets", "list", "-a", app, "--json"],
        as_dev=True, env=env, check=False, timeout=60,
    )
    if cp.returncode != 0:
        return set()
    try:
        data = parse_json_output(cp)
    except Exception:
        return set()
    items = data if isinstance(data, list) else data.get("secrets", []) if isinstance(data, dict) else []
    names: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            for key in ("Name", "name"):
                value = item.get(key)
                if isinstance(value, str) and ENV_NAME.fullmatch(value):
                    names.add(value)
                    break
    return names


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


def fly_runtime_env(flyctl: Path, app: str, env: dict[str, str]) -> dict[str, str]:
    # No value is sent to stderr/stdout by this recovery tool. flyctl stdout is
    # captured in-memory and filtered to names listed by "fly secrets list".
    cp = run(
        [str(flyctl), "ssh", "console", "-a", app, "-C", "env -0"],
        as_dev=True, env=env, check=False, timeout=90, binary=True,
    )
    if cp.returncode != 0:
        return {}
    return parse_env_bytes(cp.stdout or b"")


def write_private_env(path: Path, values: dict[str, str], mode: int = 0o600) -> None:
    lines = []
    for key in sorted(values):
        value = values[key]
        lines.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", errors="surrogateescape")
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

    app_to_project = {
        info["fly_app"]: project
        for project, info in project_info.items()
        if info.get("fly_app")
    }

    apps = fly_apps(flyctl, auth)
    safe_mkdir(RECOVERED, 0o700)
    for app in apps:
        secret_names = fly_secret_names(flyctl, app, auth)
        runtime = fly_runtime_env(flyctl, app, auth) if secret_names else {}
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
    fly_secret_canons: set[str] = set()

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
            fly_secret_canons.add(canon)
            usage[canon].add(project)
            actual_names[canon][project].add(actual)

    variables: dict[str, Any] = {}
    for canon in sorted(usage):
        projects = sorted(usage[canon])
        variables[canon] = {
            "canonical_name": canon,
            "sensitive": secret_like(canon) or canon in fly_secret_canons,
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


def merge_master(catalog: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (master, source_note_by_key), preserving prior manual values."""
    existing = parse_master(MASTER)
    master = dict(existing)
    notes: dict[str, str] = {}

    # Index exact recovered values by (project, canonical variable).
    recovered_by_project: dict[tuple[str, str], str] = {}
    recovered_hashes: dict[str, dict[str, str]] = defaultdict(dict)

    for app, info in catalog.get("fly", {}).get("apps", {}).items():
        project = info.get("project")
        if not project:
            continue
        vals = load_recovered_env(app)
        for actual, value in vals.items():
            canon = canonical(actual)
            recovered_by_project[(project, canon)] = value
            recovered_hashes[canon][project] = hashlib.sha256(
                value.encode("utf-8", errors="surrogateescape")
            ).hexdigest()

    for canon, meta in catalog["variables"].items():
        if not meta["sensitive"]:
            continue
        projects = meta["projects"]
        shared_key = f"SHARED__{canon}"

        recovered = {
            project: recovered_by_project[(project, canon)]
            for project in projects
            if (project, canon) in recovered_by_project
        }
        hashes = {recovered_hashes[canon][p] for p in recovered}

        if len(projects) > 1:
            # Promote to SHARED only when at least two independent project
            # runtimes prove the exact same value. Otherwise keep it blank and
            # preserve recovered values as explicit project overrides.
            if len(recovered) >= 2 and len(hashes) == 1:
                master.setdefault(shared_key, next(iter(recovered.values())))
                notes[shared_key] = "recovered-identical-across-projects"
            else:
                master.setdefault(shared_key, "")
                notes.setdefault(shared_key, "shared-candidate-unconfirmed")

        for project in projects:
            project_key = f"{pfx(project)}__{canon}"
            value = recovered.get(project)
            if value is not None:
                master.setdefault(project_key, value)
                notes[project_key] = "recovered-from-fly-runtime"
            elif len(projects) == 1:
                master.setdefault(project_key, "")
                notes.setdefault(project_key, "missing")
            # For shared candidates with no known conflict, do not create
            # hundreds of empty overrides. An override appears automatically
            # once recovery proves a project-specific value.

    return master, notes


def write_master(master: dict[str, str], notes: dict[str, str]) -> None:
    groups: dict[str, list[str]] = defaultdict(list)
    for key in master:
        group = "SHARED" if key.startswith("SHARED__") else key.split("__", 1)[0]
        groups[group].append(key)

    lines = [
        "# DevCoveer2 master environment/secrets.",
        "# ROOT ONLY. Never attach this file directly to a service.",
        "# Edit values here, then run:",
        "#   sudo python3 /home/dev/projects/codex-devcoveer-mcp/scripts/devcoveer-env-recovery.py render",
        "",
    ]
    for group in sorted(groups):
        lines.append(f"# [{group}]")
        for key in sorted(groups[group]):
            note = notes.get(key)
            if note:
                lines.append(f"# source: {note}")
            value = master[key]
            lines.append(f"{key}={json.dumps(value, ensure_ascii=False) if value else ''}")
        lines.append("")
    tmp = MASTER.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, MASTER)


def write_todo(master: dict[str, str], notes: dict[str, str]) -> None:
    lines = [
        "# Only missing values. Fill master.env, not this generated file.",
        "# Names are deduplicated through SHARED__ defaults plus project overrides.",
        "",
    ]
    missing = [k for k, v in master.items() if not v]
    for key in sorted(missing):
        note = notes.get(key)
        if note:
            lines.append(f"# {note}")
        lines.append(f"{key}=")
    TODO.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(TODO, 0o600)


def render(catalog: dict[str, Any] | None = None) -> dict[str, int]:
    if catalog is None:
        if not CATALOG.is_file():
            raise SystemExit("catalog.json is missing; run recover first")
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    master = parse_master(MASTER)
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
            shared = master.get(f"SHARED__{canon}", "")
            override = master.get(f"{pfx(project)}__{canon}", "")
            selected = override or shared
            if not selected:
                continue
            for actual in meta["runtime_names"].get(project, [canon]):
                values[actual] = selected
        target = SERVICES / f"{project}.env"
        write_private_env(target, values, 0o640)
        os.chown(target, 0, gid)
        counts[project] = len(values)
    return counts


def write_readme() -> None:
    README.write_text(
        """DevCoveer2 centralized env layout

master.env
  Human-editable root-only source of truth. Never give it directly to a service.

todo.env
  Generated list of unresolved names. Fill master.env, then rerender.

catalog.json
  Machine-readable project/env usage, aliases, Fly app mapping and recovery status.
  Contains names and hashes only, never recovered secret values.

recovered/fly/*.env
  Root-only exact values recovered from running Fly Machines. Recovery evidence;
  services should not consume these files directly.

services/<project>.env
  Least-privilege rendered env for one project, mode root:dev 0640.

Resolution:
  PROJECT__VARIABLE overrides SHARED__VARIABLE.
  SHARED values are populated automatically only when recovery proves equality
  across at least two project runtimes. Otherwise they remain blank until the
  owner chooses to share them.
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

    master, notes = merge_master(catalog)
    write_master(master, notes)
    write_todo(master, notes)
    counts = render(catalog)
    write_readme()

    recovered_total = sum(
        len(x.get("recovered_names", []))
        for x in fly.get("apps", {}).values()
    )
    missing_total = sum(1 for value in master.values() if not value)
    sensitive_total = sum(
        1 for x in catalog["variables"].values() if x["sensitive"]
    )

    # No values are ever printed.
    print("ENV_RECOVERY_COMPLETE")
    print(f"projects={len(projects)}")
    print(f"sensitive_variable_families={sensitive_total}")
    print(f"fly_available={fly.get('available', False)}")
    print(f"fly_authenticated={fly.get('authenticated', False)}")
    print(f"fly_apps_seen={len(fly.get('apps', {}))}")
    print(f"fly_secret_values_recovered={recovered_total}")
    print(f"master_missing_values={missing_total}")
    print(f"master={MASTER}")
    print(f"todo={TODO}")
    print(f"catalog={CATALOG}")
    print(f"service_envs={SERVICES}")
    print("rendered_project_counts=" + json.dumps(counts, sort_keys=True))
    return 0


def render_cmd(_: argparse.Namespace) -> int:
    ensure_root()
    counts = render()
    master = parse_master(MASTER)
    # Regenerate TODO without source notes on a render-only pass.
    write_todo(master, {})
    print("ENV_RENDER_COMPLETE")
    print("rendered_project_counts=" + json.dumps(counts, sort_keys=True))
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
