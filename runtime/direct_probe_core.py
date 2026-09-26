from __future__ import annotations

import asyncio
import base64
import fcntl
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
DIRECT_RECEIPT_LOG = Path.home() / ".local" / "state" / "openai-codex-mcp" / "direct-ops-receipts.jsonl"
MAX_DIRECT_RECEIPT_LOG_BYTES = 1024 * 1024
MAX_CHECKS = 20
BATCH_DEADLINE_SECONDS = 60.0
CHECK_TIMEOUT_SECONDS: dict[str, float] = {
    # Cheap local facts remain short; network/status and bounded scans get room to finish.
    "git_state": 15.0, "recent_direct_ops": 5.0, "remote_head": 25.0, "git_log": 15.0, "tree": 15.0,
    "worktree_list": 10.0, "diff": 25.0, "read_text": 25.0, "search": 35.0,
    "file_list": 25.0, "file_stat": 12.0, "file_sha256": 35.0, "github_status": 50.0,
    "inventory": 5.0, "service": 12.0, "listeners": 15.0, "logs": 25.0,
    "resources": 50.0, "health": 15.0, "runtime": 20.0, "runtime_file": 35.0,
    "config_presence": 15.0,
    "system_info": 10.0, "service_list": 20.0, "journal": 25.0,
    "network_listeners": 15.0,
}
MAX_RESPONSE_BYTES = 256 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_FILES = 5000
MAX_SEARCH_BYTES = 64 * 1024 * 1024
MAX_SEARCH_MATCHES = 200
MAX_LIST_ENTRIES = 1000
MAX_TREE_ENTRIES = 1000
MAX_LOG_LINES = 500
MAX_DIFF_BYTES = 128 * 1024
MAX_PROCESS_OUTPUT = 256 * 1024
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
    "sessions", "session",
}
REDACTION_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)\S+"),
    re.compile(r"(?i)((?:token|secret|password|passwd|api[_-]?key|access[_-]?key)\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(https?://[^\s:/]+:)[^@/\s]+@"),
    re.compile(r"(?i)(?:gh[pousr]_[A-Za-z0-9_]{12,})"),
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
_LIMIT_SCHEMA = {"type": "integer", "minimum": 1, "maximum": MAX_LIST_ENTRIES}

PROJECT_CHECK_SCHEMAS = [
    _check_schema("git_state"),
    _check_schema("recent_direct_ops", {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    }),
    _check_schema("remote_head", {
        "remote": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "branch": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"},
    }, ["remote", "branch"]),
    _check_schema("git_log", {"ref": _REF_SCHEMA, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["ref"]),
    _check_schema("tree", {"ref": _REF_SCHEMA, "path": {"type": "string", "maxLength": 500}, "limit": _LIMIT_SCHEMA}, ["ref"]),
    {**_check_schema("worktree_list"), "description": (
        "List Git worktrees for the resolved repository. Direct-child checkouts under /home/dev/projects "
        "include a tool-addressable project hint and current marker; use them to select the physical workspace."
    )},
    _check_schema("worktree_compare", {
        "worktree": {"type":"string","minLength":1,"maxLength":1000},
        "target_ref": _REF_SCHEMA,
        "include_untracked": {"type":"boolean","default":True},
        "limit": {"type":"integer","minimum":1,"maximum":1000},
    }, ["worktree","target_ref"]),
    _check_schema("capabilities"),
    {**_check_schema("instructions", {
        "scope": {"type": "string", "enum": ["home", "project"], "default": "project"},
        "cwd": {"type": "string", "maxLength": 500},
        "start_line": {"type": "integer", "minimum": 1, "maximum": 1000000},
        "end_line": {"type": "integer", "minimum": 1, "maximum": 1000000},
    }), "description": "Read only ~/AGENTS.md (scope=home) or project cwd/AGENTS.md (scope=project). Exact bounded redacted read, no recursive HOME search. Follow next_start_line when present."},
    {**_check_schema("web_preflight", {
        "cwd": {"type": "string", "maxLength": 500, "description": "Project-relative directory containing package.json, e.g. site."},
        "browser": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "default": "chromium"},
    }), "description": "Inspect this project's installed Node/Playwright, script names and expected browser executable. No download, browser launch or website access. Returns pinned local CLI job_start parameters; executable presence is not launch acceptance."},
    _check_schema("workspace_compare", {
        "target_ref": _REF_SCHEMA,
        "include_untracked": {"type": "boolean", "default": True},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
    }, ["target_ref"]),
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
    _check_schema("file_list", {"path": {"type": "string", "maxLength": 500}, "depth": {"type": "integer", "minimum": 0, "maximum": 8}, "limit": _LIMIT_SCHEMA}),
    _check_schema("file_stat", {"path": _PATH_SCHEMA}, ["path"]),
    _check_schema("file_sha256", {"path": _PATH_SCHEMA}, ["path"]),
    _check_schema("github_status", {
        "branch": {"type": "string", "minLength": 1, "maxLength": 200},
        "pr": {"type": "integer", "minimum": 1},
        "run_id": {"type": "integer", "minimum": 1},
        "failed_log_lines": {"type": "integer", "minimum": 0, "maximum": 500},
        "include_runners": {"type": "boolean", "default": False},
    }),
]

HOST_CHECK_SCHEMAS = [
    _check_schema("inventory"),
    _check_schema("system_info"),
    _check_schema("service_list", {
        "scope": {"type": "string", "enum": ["user", "system", "all"], "default": "all"},
        "pattern": {"type": "string", "minLength": 1, "maxLength": 128},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
    }),
    _check_schema("service", {"service": {"type": "string", "minLength": 1, "maxLength": 64}}, ["service"]),
    _check_schema("journal", {
        "unit": {"type": "string", "minLength": 1, "maxLength": 200,
                 "pattern": r"^[A-Za-z0-9_.@:-]+\.service$"},
        "scope": {"type": "string", "enum": ["user", "system"], "default": "user"},
        "lines": {"type": "integer", "minimum": 1, "maximum": MAX_LOG_LINES, "default": 100},
        "since_seconds": {"type": "integer", "minimum": 0, "maximum": 2592000, "default": 3600},
    }, ["unit"]),
    _check_schema("network_listeners"),
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
    _check_schema("root_file_stat", {
        "root": {"type":"string","minLength":1,"maxLength":64},
        "path": _PATH_SCHEMA,
    }, ["root","path"]),
    _check_schema("root_file_sha256", {
        "root": {"type":"string","minLength":1,"maxLength":64},
        "path": _PATH_SCHEMA,
    }, ["root","path"]),
    _check_schema("root_read_text", {
        "root": {"type":"string","minLength":1,"maxLength":64},
        "path": _PATH_SCHEMA,
        "start_line": {"type":"integer","minimum":1,"maximum":1000000},
        "end_line": {"type":"integer","minimum":1,"maximum":1000000},
    }, ["root","path"]),
    _check_schema("process_tree", {
        "service": {"type":"string","minLength":1,"maxLength":64},
        "limit": {"type":"integer","minimum":1,"maximum":128},
    }, ["service"]),
    _check_schema("network", {
        "mode": {"type":"string","enum":["dns","tcp","tls","http"]},
        "host": {"type":"string","minLength":1,"maxLength":253},
        "port": {"type":"integer","minimum":0,"maximum":65535},
        "url": {"type":"string","minLength":1,"maxLength":4096},
        "method": {"type":"string","enum":["GET","HEAD"]},
    }, ["mode"]),
    _check_schema("mcp_manifest", {
        "file": {"type":"string","minLength":1,"maxLength":64},
    }),
    _check_schema("config_presence", {
        "source": {"type": "string", "minLength": 1, "maxLength": 64},
        "keys": {"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True,
                 "items": {"type": "string", "minLength": 1, "maxLength": 128, "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"}},
    }, ["source", "keys"]),
]

PROJECT_PROBE_INPUT_SCHEMA: dict[str, Any] = _obj({
    "project": {"type": "string", "minLength": 1, "maxLength": 128},
    "checks": {"type": "array", "minItems": 1, "maxItems": MAX_CHECKS, "items": {"oneOf": PROJECT_CHECK_SCHEMAS}},
}, ["project", "checks"])

HOST_PROBE_INPUT_SCHEMA: dict[str, Any] = _obj({
    "project": {"type": "string", "minLength": 1, "maxLength": 128},
    "checks": {"type": "array", "minItems": 1, "maxItems": MAX_CHECKS, "items": {"oneOf": HOST_CHECK_SCHEMAS}},
}, ["project", "checks"])


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
    # The leader may have exited while its children still own stdout/stderr pipes.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if proc.returncode is None:
            try: proc.kill()
            except ProcessLookupError: pass
    await proc.wait()


def _append_job_log(path: Path, data: bytes) -> None:
    text, _ = _redact_text(data.decode("utf-8", "replace"))
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as handle:
        handle.seek(0, 2)
        handle.write(text.encode("utf-8"))
        if handle.tell() > 1024 * 1024:
            handle.seek(-512 * 1024, 2)
            tail = handle.read().partition(b"\n")[2]
            handle.seek(0); handle.write(tail); handle.truncate()


async def _read_limited(stream: asyncio.StreamReader | None, limit: int, proc: asyncio.subprocess.Process,
                        *, tail: bool = False, captured: bytearray | None = None,
                        log_path: Path | None = None) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    kept = captured if captured is not None else bytearray()
    truncated = False
    pending = b""
    dropping = False

    def emit(line: bytes) -> None:
        nonlocal truncated
        # Redact before truncating: a tail must not start in the middle of a secret.
        text, _ = _redact_text(line.decode("utf-8", "replace"))
        data = text.encode("utf-8")
        if tail:
            kept.extend(data)
            if len(kept) > limit:
                del kept[:-limit]
                truncated = True
        if log_path is not None:
            _append_job_log(log_path, data)

    try:
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            if not tail:
                room = max(0, limit - len(kept))
                kept.extend(chunk[:room])
                truncated |= len(chunk) > room
            if tail or log_path is not None:
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not dropping:
                        emit(line + b"\n")
                    dropping = False
                if len(pending) > 262144:
                    emit(b"[oversized output line omitted]\n")
                    pending = b""
                    dropping = True
                    truncated = True
    finally:
        if (tail or log_path is not None) and pending and not dropping:
            emit(pending)
    return bytes(kept), truncated


async def _run_process(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
                       timeout: float = 5.0, output_limit: int = MAX_PROCESS_OUTPUT,
                       capture_tail: bool = False, log_dir: Path | None = None) -> ProcessResult:
    if not argv or not argv[0].startswith("/"):
        raise ProbeError("runner_policy", "trusted executable must be absolute")
    started = time.monotonic()
    async with _PROCESS_SEMAPHORE:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd) if cwd else None, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        out, err = bytearray(), bytearray()
        out_task = asyncio.create_task(_read_limited(proc.stdout, output_limit, proc,
            tail=capture_tail, captured=out, log_path=log_dir / "stdout.log" if log_dir else None))
        err_task = asyncio.create_task(_read_limited(proc.stderr, min(output_limit, 32 * 1024), proc,
            tail=capture_tail, captured=err, log_path=log_dir / "stderr.log" if log_dir else None))
        finished = asyncio.gather(proc.wait(), out_task, err_task)
        try:
            # The timeout includes pipe EOF, not only leader exit.
            await asyncio.wait_for(asyncio.shield(finished), timeout=timeout)
        except BaseException as exc:
            await _kill_process(proc)
            try:
                await asyncio.wait_for(asyncio.shield(finished), timeout=2)
            except BaseException:
                finished.cancel()
                out_task.cancel(); err_task.cancel()
                await asyncio.gather(finished, out_task, err_task, return_exceptions=True)
            if isinstance(exc, asyncio.TimeoutError):
                failure = ProbeError("timeout", "trusted subprocess timed out", status="timeout")
                failure.partial_result = ProcessResult(bytes(out), bytes(err), proc.returncode or -9, True,
                                                        int((time.monotonic() - started) * 1000))
                raise failure from exc
            raise
        stdout, out_truncated = out_task.result()
        stderr, err_truncated = err_task.result()
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


async def _tracked_workspace_digest(root: Path) -> str:
    """Digest HEAD + Git index + tracked working-tree changes; untracked files are excluded."""
    head_res = await _git(root, "rev-parse", "--verify", "HEAD", ok_codes=(0, 128), output_limit=256)
    head = head_res.stdout.strip() if head_res.exit_code == 0 else b"<unborn>"
    index = await _git(root, "ls-files", "-s", "-z", output_limit=8 * 1024 * 1024)
    names = await _git(
        root, "diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", "--",
        output_limit=4 * 1024 * 1024,
    )
    raw = await _git(
        root, "diff", "--raw", "-z", "--no-ext-diff", "--no-textconv", "--",
        output_limit=8 * 1024 * 1024,
    )
    if index.truncated or names.truncated or raw.truncated:
        raise ProbeError(
            "workspace_digest",
            "tracked workspace state exceeds the bounded digest input; refusing an incomplete digest",
            status="denied",
        )

    digest = hashlib.sha256()
    digest.update(b"head\0" + head + b"\0index\0" + index.stdout + b"\0worktree-raw\0" + raw.stdout)
    changed_paths = sorted({item for item in names.stdout.split(b"\0") if item})
    for raw_path in changed_paths:
        digest.update(b"\0path\0" + raw_path)
        path = root / os.fsdecode(raw_path)
        try:
            st = path.lstat()
        except FileNotFoundError:
            digest.update(b"\0deleted")
            continue
        digest.update(f"\0mode:{st.st_mode & 0o170777:o}\0".encode("ascii"))
        if statmod.S_ISLNK(st.st_mode):
            digest.update(b"symlink\0" + os.fsencode(os.readlink(path)))
        elif statmod.S_ISREG(st.st_mode):
            digest.update(b"file-sha256\0" + _sha256_path(path).encode("ascii"))
        elif statmod.S_ISDIR(st.st_mode):
            digest.update(b"directory")
        else:
            digest.update(b"special")
    return digest.hexdigest()


def _append_direct_receipt(entry: dict[str, Any]) -> None:
    """Best-effort bounded observability; receipt failure must never block the requested operation."""
    try:
        path = DIRECT_RECEIPT_LOG
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = dict(entry)
        payload.setdefault("observed_at", dt.datetime.now(dt.timezone.utc).isoformat())
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0, 2)
            handle.write(line)
            handle.flush()
            size = handle.tell()
            if size > MAX_DIRECT_RECEIPT_LOG_BYTES:
                keep = MAX_DIRECT_RECEIPT_LOG_BYTES // 2
                handle.seek(max(0, size - keep))
                tail = handle.read()
                tail = tail.partition("\n")[2]
                handle.seek(0)
                handle.write(tail)
                handle.truncate()
    except (OSError, ValueError, TypeError):
        return


def _recent_direct_receipts(root: Path, *, limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    path = DIRECT_RECEIPT_LOG
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {"entries": [], "limit": limit, "complete": True}
    except OSError:
        return {"entries": [], "limit": limit, "complete": False}
    try:
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            text = handle.read(MAX_DIRECT_RECEIPT_LOG_BYTES + 1)
    except OSError:
        return {"entries": [], "limit": limit, "complete": False}
    complete = len(text) <= MAX_DIRECT_RECEIPT_LOG_BYTES
    target = str(root.resolve())
    entries: list[dict[str, Any]] = []
    for line in reversed(text[-MAX_DIRECT_RECEIPT_LOG_BYTES:].splitlines()):
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(item, dict) or item.get("cwd") != target:
            continue
        entries.append(item)
        if len(entries) >= limit:
            break
    return {"entries": entries, "limit": limit, "complete": complete}


async def _resolve_commit(root: Path, ref: str) -> str:
    ref = _validate_ref(ref)
    result = await _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}", ok_codes=(0, 128), output_limit=256)
    if result.exit_code != 0:
        raise ProbeError("not_found", "local commit object is absent", status="not_found", exit_code=result.exit_code)
    sha = result.stdout.decode().strip().lower()
    if not HEX40_RE.fullmatch(sha):
        raise ProbeError("git", "resolved commit id is invalid")
    return sha
