from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import http.client
import importlib.metadata
import ipaddress
import json
import os
import re
import signal
import stat as statmod
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote, urlsplit

INSTALL_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = INSTALL_DIR / "direct_probe_config.json"
MAX_CHECKS = 8
BATCH_DEADLINE_SECONDS = 60.0
CHECK_TIMEOUT_SECONDS: dict[str, float] = {
    # Cheap local facts remain short; network/status and bounded scans get room to finish.
    "git_state": 15.0, "remote_head": 25.0, "git_log": 15.0, "tree": 15.0,
    "worktree_list": 10.0, "diff": 25.0, "read_text": 25.0, "search": 35.0,
    "file_list": 25.0, "file_stat": 12.0, "file_sha256": 35.0, "github_status": 50.0,
    "inventory": 5.0, "service": 12.0, "listeners": 15.0, "logs": 25.0,
    "resources": 50.0, "health": 15.0, "runtime": 20.0, "runtime_file": 35.0,
    "config_presence": 15.0, "opencode_catalog": 20.0,
}
MAX_RESPONSE_BYTES = 64 * 1024
MAX_TEXT_BYTES = 12 * 1024
MAX_FILE_BYTES = 512 * 1024
MAX_SEARCH_FILES = 1500
MAX_SEARCH_BYTES = 8 * 1024 * 1024
MAX_SEARCH_MATCHES = 80
MAX_LIST_ENTRIES = 300
MAX_TREE_ENTRIES = 300
MAX_LOG_LINES = 250
MAX_DIFF_BYTES = 24 * 1024
MAX_PROCESS_OUTPUT = 96 * 1024
SUBPROCESS_CONCURRENCY = 4
_PROCESS_SEMAPHORE = asyncio.Semaphore(SUBPROCESS_CONCURRENCY)

REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
HEX40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SECRET_BASENAMES = {
    ".env", ".npmrc", ".pypirc", "credentials", "credentials.json", "secrets.json",
    "id_rsa", "id_ed25519", "authorized_keys", "known_hosts", "netrc", ".netrc",
}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdb", ".sqlite", ".sqlite3", ".db"}
SECRET_SEGMENTS = {
    ".ssh", ".gnupg", "sessions", "session", "secrets", "credentials", "private",
    "tokens", "keyrings", "wallets", "database", "databases",
}
SKIP_SEARCH_SEGMENTS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".cache", "cache", "dist", "build", "target", ".idea", ".vscode",
    "sessions", "session", "state", "data", "tmp", "temp", "vendor",
}
REDACTION_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)\S+"),
    re.compile(r"(?i)((?:token|secret|password|passwd|api[_-]?key|access[_-]?key)\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(https?://[^\s:/]+:)[^@/\s]+@"),
    re.compile(r"(?i)(gh[pousr]_[A-Za-z0-9_]{12,})"),
]


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _check_schema(kind: str, extra: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    props: dict[str, Any] = {
        "id": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"},
        "kind": {"type": "string", "const": kind},
    }
    if extra:
        props.update(extra)
    return _obj(props, ["id", "kind", *(required or [])])


_SOURCE_SCHEMA = {
    "type": "string", "enum": ["worktree", "local_commit", "remote_commit"], "default": "worktree"
}
_REF_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"}
_PATH_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 500}
_LIMIT_SCHEMA = {"type": "integer", "minimum": 1, "maximum": 300}

PROJECT_CHECK_SCHEMAS = [
    _check_schema("git_state"),
    _check_schema("remote_head", {
        "remote": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "branch": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"},
    }, ["remote", "branch"]),
    _check_schema("git_log", {"ref": _REF_SCHEMA, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["ref"]),
    _check_schema("tree", {"ref": _REF_SCHEMA, "path": {"type": "string", "maxLength": 500}, "limit": _LIMIT_SCHEMA}, ["ref"]),
    _check_schema("worktree_list"),
    _check_schema("diff", {
        "base": {"type": "string", "minLength": 1, "maxLength": 200},
        "target": {"type": "string", "minLength": 1, "maxLength": 200},
        "path": {"type": "string", "maxLength": 500},
        "context": {"type": "integer", "minimum": 0, "maximum": 10},
    }, ["base", "target"]),
    _check_schema("read_text", {
        "source": _SOURCE_SCHEMA, "path": _PATH_SCHEMA, "ref": _REF_SCHEMA,
        "remote": {"type": "string", "minLength": 1, "maxLength": 64},
        "start_line": {"type": "integer", "minimum": 1, "maximum": 1000000},
        "end_line": {"type": "integer", "minimum": 1, "maximum": 1000000},
    }, ["path"]),
    _check_schema("search", {
        "query": {"type": "string", "minLength": 1, "maxLength": 512},
        "mode": {"type": "string", "enum": ["content", "name"], "default": "content"},
        "paths": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "string", "maxLength": 500}},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_MATCHES},
    }, ["query"]),
    _check_schema("file_list", {"path": {"type": "string", "maxLength": 500}, "depth": {"type": "integer", "minimum": 0, "maximum": 4}, "limit": _LIMIT_SCHEMA}),
    _check_schema("file_stat", {"path": _PATH_SCHEMA}, ["path"]),
    _check_schema("file_sha256", {"path": _PATH_SCHEMA}, ["path"]),
    _check_schema("github_status", {
        "branch": {"type": "string", "minLength": 1, "maxLength": 200},
        "pr": {"type": "integer", "minimum": 1},
        "run_id": {"type": "integer", "minimum": 1},
        "failed_log_lines": {"type": "integer", "minimum": 0, "maximum": 120},
        "include_runners": {"type": "boolean", "default": False},
    }),
]

HOST_CHECK_SCHEMAS = [
    _check_schema("inventory"),
    _check_schema("service", {"service": {"type": "string", "minLength": 1, "maxLength": 64}}, ["service"]),
    _check_schema("listeners", {"service": {"type": "string", "minLength": 1, "maxLength": 64}}, ["service"]),
    _check_schema("logs", {
        "log": {"type": "string", "minLength": 1, "maxLength": 64},
        "lines": {"type": "integer", "minimum": 1, "maximum": MAX_LOG_LINES},
        "since_seconds": {"type": "integer", "minimum": 0, "maximum": 604800},
    }, ["log"]),
    _check_schema("resources", {
        "root": {"type": "string", "minLength": 1, "maxLength": 64},
        "largest": {"type": "integer", "minimum": 0, "maximum": 30},
    }),
    _check_schema("health", {
        "endpoint": {"type": "string", "minLength": 1, "maxLength": 64},
        "method": {"type": "string", "enum": ["GET", "HEAD"], "default": "GET"},
    }, ["endpoint"]),
    _check_schema("runtime", {"runtime": {"type": "string", "minLength": 1, "maxLength": 64}}, ["runtime"]),
    _check_schema("runtime_file", {"file": {"type": "string", "minLength": 1, "maxLength": 64}}, ["file"]),
    _check_schema("config_presence", {
        "source": {"type": "string", "minLength": 1, "maxLength": 64},
        "keys": {"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True,
                 "items": {"type": "string", "minLength": 1, "maxLength": 128, "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"}},
    }, ["source", "keys"]),
    _check_schema("opencode_catalog", {
        "catalog": {"type": "string", "minLength": 1, "maxLength": 64},
        "provider": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
    }, ["catalog"]),
]

PROJECT_PROBE_INPUT_SCHEMA: dict[str, Any] = _obj({
    "project": {"type": "string", "minLength": 1, "maxLength": 128},
    "checks": {"type": "array", "minItems": 1, "maxItems": MAX_CHECKS, "items": {"oneOf": PROJECT_CHECK_SCHEMAS}},
}, ["project", "checks"])

HOST_PROBE_INPUT_SCHEMA: dict[str, Any] = _obj({
    "project": {"type": "string", "minLength": 1, "maxLength": 128},
    "checks": {"type": "array", "minItems": 1, "maxItems": MAX_CHECKS, "items": {"oneOf": HOST_CHECK_SCHEMAS}},
}, ["project", "checks"])

HOST_ACTION_OPERATION_SCHEMA: dict[str, Any] = _obj({
    "id": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"},
    "service": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
    "operation": {"type": "string", "enum": ["start", "restart", "enable_start"]},
}, ["id", "service", "operation"])

HOST_ACTION_INPUT_SCHEMA: dict[str, Any] = _obj({
    "project": {"type": "string", "minLength": 1, "maxLength": 128},
    "actions": {"type": "array", "minItems": 1, "maxItems": 6, "items": HOST_ACTION_OPERATION_SCHEMA},
}, ["project", "actions"])


class ProbeError(Exception):
    def __init__(self, category: str, message: str, *, status: str = "error", exit_code: int | None = None):
        super().__init__(message)
        self.category = category
        self.status = status
        self.exit_code = exit_code


@dataclass(slots=True)
class ProcessResult:
    stdout: bytes
    stderr: bytes
    exit_code: int
    truncated: bool
    duration_ms: int


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _redact_text(value: str) -> tuple[str, bool]:
    redacted = False
    out = value
    for pattern in REDACTION_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            nonlocal redacted
            redacted = True
            if match.lastindex and match.lastindex >= 1 and match.group(1):
                prefix = match.group(1)
                if prefix.lower().startswith("http"):
                    return prefix + "<redacted>@"
                return prefix + "<redacted>"
            return "<redacted>"
        out = pattern.sub(repl, out)
    return out, redacted


def _load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError("config", f"trusted probe config unavailable: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("projects"), dict):
        raise ProbeError("config", "trusted probe config has unsupported shape")
    return data


def _binding(config: dict[str, Any], project_id: str) -> dict[str, Any]:
    binding = config["projects"].get(project_id)
    if binding is None:
        return {"github_repo": None, "remotes": {}}
    if not isinstance(binding, dict):
        raise ProbeError("config", "project binding is invalid")
    return binding


def _validate_alias(value: str) -> str:
    if not ALIAS_RE.fullmatch(value):
        raise ProbeError("invalid_input", "invalid configured alias")
    return value


def _validate_ref(value: str) -> str:
    if not REF_RE.fullmatch(value) or value.startswith("-") or ".." in value or "@{" in value or value.endswith(".") or value.endswith("/") or "//" in value:
        raise ProbeError("invalid_ref", "git ref is not allowed")
    return value


def _validate_branch(value: str) -> str:
    if not BRANCH_RE.fullmatch(value) or value.startswith("-") or ".." in value or "@{" in value or value.endswith(".") or value.endswith("/") or "//" in value:
        raise ProbeError("invalid_ref", "branch is not allowed")
    return value


def _validate_rel_path(raw: str, *, allow_empty: bool = False) -> PurePosixPath:
    if not isinstance(raw, str) or "\x00" in raw or "\\" in raw or raw.startswith("/") or raw.startswith("~"):
        raise ProbeError("path_denied", "path must be repository-relative")
    if raw == "" and allow_empty:
        return PurePosixPath(".")
    p = PurePosixPath(raw)
    if raw == "" or any(part in ("", ".", "..") for part in p.parts) or (p.parts and p.parts[0].startswith("-")):
        raise ProbeError("path_denied", "unsafe relative path")
    _assert_nonsecret_parts(p.parts)
    return p


def _assert_nonsecret_parts(parts: tuple[str, ...] | list[str]) -> None:
    lowered = [part.lower() for part in parts]
    for part in lowered:
        if part in SECRET_SEGMENTS:
            raise ProbeError("secret_path", "secret/session/state path is denied", status="denied")
        if part in SECRET_BASENAMES or part.startswith(".env") or any(part.endswith(suffix) for suffix in SECRET_SUFFIXES):
            raise ProbeError("secret_path", "secret/config/database file is denied", status="denied")


def _safe_worktree_path(root: Path, raw: str, *, require_file: bool = False, allow_empty: bool = False) -> Path:
    rel = _validate_rel_path(raw, allow_empty=allow_empty)
    root_real = root.resolve(strict=True)
    candidate = root_real if str(rel) == "." else root_real.joinpath(*rel.parts)
    current = root_real
    for part in (() if str(rel) == "." else rel.parts):
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError as exc:
            raise ProbeError("not_found", "path does not exist", status="not_found") from exc
        if statmod.S_ISLNK(st.st_mode):
            raise ProbeError("symlink_escape", "symlinks are not readable by probes", status="denied")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root_real)
    except ValueError as exc:
        raise ProbeError("path_escape", "path escapes project root", status="denied") from exc
    st = resolved.stat()
    if require_file and not statmod.S_ISREG(st.st_mode):
        raise ProbeError("file_type", "only regular files are readable", status="denied")
    if not (statmod.S_ISREG(st.st_mode) or statmod.S_ISDIR(st.st_mode)):
        raise ProbeError("file_type", "special files are denied", status="denied")
    return resolved


def _is_binary(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _sha256_path(path: Path, max_bytes: int | None = None) -> str:
    h = hashlib.sha256()
    seen = 0
    with path.open("rb", buffering=0) as f:
        while True:
            chunk = f.read(128 * 1024)
            if not chunk:
                break
            seen += len(chunk)
            if max_bytes is not None and seen > max_bytes:
                raise ProbeError("file_too_large", "file exceeds hash size limit", status="denied")
            h.update(chunk)
    return h.hexdigest()


async def _kill_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        await proc.wait()
    except Exception:
        pass


async def _read_limited(stream: asyncio.StreamReader | None, limit: int, proc: asyncio.subprocess.Process) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    parts: list[bytes] = []
    total_kept = 0
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        if total_kept < limit:
            keep = min(len(chunk), limit - total_kept)
            if keep:
                parts.append(chunk[:keep])
                total_kept += keep
            if keep < len(chunk):
                truncated = True
        else:
            truncated = True
        # Once capped, keep draining/discarding. Runtime is still bounded by the
        # subprocess timeout, while memory and returned output remain finite.
    return b"".join(parts), truncated


async def _run_process(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
                       timeout: float = 5.0, output_limit: int = MAX_PROCESS_OUTPUT) -> ProcessResult:
    if not argv or not argv[0].startswith("/"):
        raise ProbeError("runner_policy", "trusted executable must be absolute")
    started = time.monotonic()
    async with _PROCESS_SEMAPHORE:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd) if cwd else None, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        out_task = asyncio.create_task(_read_limited(proc.stdout, output_limit, proc))
        err_task = asyncio.create_task(_read_limited(proc.stderr, min(output_limit, 32 * 1024), proc))
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            stdout, out_truncated = await out_task
            stderr, err_truncated = await err_task
        except asyncio.TimeoutError as exc:
            await _kill_process(proc)
            for task in (out_task, err_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(out_task, err_task, return_exceptions=True)
            raise ProbeError("timeout", "trusted subprocess timed out", status="timeout") from exc
        except asyncio.CancelledError:
            await _kill_process(proc)
            for task in (out_task, err_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(out_task, err_task, return_exceptions=True)
            raise
    return ProcessResult(stdout, stderr, proc.returncode or 0, out_truncated or err_truncated,
                         int((time.monotonic() - started) * 1000))


def _git_env() -> dict[str, str]:
    base = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/home/dev"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
    }
    return base


def _git_argv(root: Path, *args: str) -> list[str]:
    return ["/usr/bin/git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "diff.external=", "-c", "protocol.ext.allow=never", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=", "-C", str(root), *args]


async def _git(root: Path, *args: str, timeout: float = 4.0, output_limit: int = MAX_PROCESS_OUTPUT,
               ok_codes: tuple[int, ...] = (0,)) -> ProcessResult:
    result = await _run_process(_git_argv(root, *args), env=_git_env(), timeout=timeout, output_limit=output_limit)
    if result.exit_code not in ok_codes:
        text, _ = _redact_text(result.stderr.decode("utf-8", "replace")[-2000:])
        category = "not_found" if result.exit_code in (1, 128) and ("unknown revision" in text.lower() or "not a valid object" in text.lower() or "bad object" in text.lower()) else "git"
        raise ProbeError(category, text.strip() or f"git exited {result.exit_code}", status="not_found" if category == "not_found" else "error", exit_code=result.exit_code)
    return result


async def _resolve_commit(root: Path, ref: str) -> str:
    ref = _validate_ref(ref)
    result = await _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}", ok_codes=(0, 128), output_limit=256)
    if result.exit_code != 0:
        raise ProbeError("not_found", "local commit object is absent", status="not_found", exit_code=result.exit_code)
    sha = result.stdout.decode().strip().lower()
    if not HEX40_RE.fullmatch(sha):
        raise ProbeError("git", "resolved commit id is invalid")
    return sha

