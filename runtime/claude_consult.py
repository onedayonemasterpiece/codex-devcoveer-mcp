"""One supplied-context Claude Code consultation, with no model tools or fallback.

The CLI keeps its existing subscription login. It is not a project agent: safe
mode suppresses ordinary customizations, restricted mode suppresses user/project
settings, and built-in plus MCP tools are absent. Administrator-managed policy
remains trusted; these flags are not an operating-system sandbox. Unsupported flags fail closed.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from model_consult import (
    consultation_metadata, consultation_prompt, consultation_request_fingerprint,
    persist_consultation,
)

RUN_ROOT = Path("/home/dev/.local/share/openai-codex-mcp/claude-consultations")
BINARY_PATHS = (Path("/home/dev/.local/bin/claude"), Path("/usr/local/bin/claude"))
VSCODE_EXTENSIONS = Path("/home/dev/.vscode-server/extensions")
VSCODE_CLAUDE_RE = re.compile(r"anthropic\.claude-code-([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})-linux-x64")
MAX_EXTENSION_ENTRIES = 512
MODEL = "claude-opus-5-5"
TASK_RE = re.compile(r"^dvt_[0-9a-f]{32}$")
MAX_PROMPT_CHARS = 120_000
MAX_OUTPUT_BYTES = 2_000_000
TIMEOUT_SECONDS = 900
TERMINAL = {"completed", "failed", "interrupted"}


class ClaudeConsultError(RuntimeError):
    pass


def _now() -> int:
    return int(time.time() * 1000)


def _directory(task_id: str) -> Path:
    if TASK_RE.fullmatch(task_id) is None:
        raise ClaudeConsultError("invalid consultation task id")
    path = RUN_ROOT / task_id
    if RUN_ROOT.is_symlink() or path.is_symlink():
        raise ClaudeConsultError("unsafe consultation directory")
    return path


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _locked(task_id: str):
    directory = _directory(task_id)
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    fd = os.open(directory / "state.lock", flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(task_id: str) -> dict[str, Any] | None:
    path = _directory(task_id) / "status.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 64_000:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _update(task_id: str, **changes: Any) -> dict[str, Any]:
    with _locked(task_id):
        state = _read(task_id) or {"schema": 1, "model": MODEL, "createdAt": _now()}
        # Parent dispatch, cancellation and late worker completion cannot overwrite
        # an already terminal outcome.
        if state.get("status") in TERMINAL:
            return state
        state.update(changes)
        _atomic_json(_directory(task_id) / "status.json", state)
        return state


def _worker_matches(task_id: str, pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return b"--claude-consult-worker" in command and task_id.encode() in command


def _binary() -> Path:
    for path in BINARY_PATHS:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    registered = shutil.which("claude", path="/home/dev/.local/bin:/usr/local/bin:/usr/bin:/bin")
    if registered:
        return Path(registered)
    # Only the observed official VS Code installer layout, one directory level.
    # Discover installation versions before dispatch; provider failure never retries
    # an older executable or another model.
    candidates = []
    if VSCODE_EXTENSIONS.is_dir() and not VSCODE_EXTENSIONS.is_symlink():
        with os.scandir(VSCODE_EXTENSIONS) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_EXTENSION_ENTRIES:
                    raise ClaudeConsultError("VS Code extension catalog exceeds bounded discovery limit")
                match = VSCODE_CLAUDE_RE.fullmatch(entry.name)
                if match and entry.is_dir(follow_symlinks=False):
                    candidates.append((tuple(int(part) for part in match.groups()), Path(entry.path)))
        for _version, extension in sorted(candidates, reverse=True):
            path = extension / "resources" / "native-binary" / "claude"
            if (path.is_file() and os.access(path, os.X_OK)
                    and path.resolve().is_relative_to(VSCODE_EXTENSIONS.resolve())):
                return path
    raise ClaudeConsultError("Claude Code is not installed at a registered binary path")


def _environment() -> dict[str, str]:
    # Do not inherit provider routing, API keys, proxy overrides, project variables,
    # NODE_OPTIONS, plugins, or nested-agent configuration from the bridge.
    return {
        "HOME": "/home/dev", "USER": "dev", "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def command(binary: Path) -> list[str]:
    # --bare deliberately is NOT used: it discards subscription OAuth credentials.
    # Never retry with fewer flags. Never pass --fallback-model or permission bypass.
    return [
        str(binary), "--safe-mode", "--restricted", "--print",
        "--tools", "", "--disallowedTools", "*",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "", "--permission-mode", "dontAsk",
        "--no-session-persistence", "--output-format", "json",
        "--max-turns", "1", "--model", MODEL,
    ]


def decode_result(raw: str, exit_code: int) -> tuple[str | None, str | None, dict[str, Any]]:
    """Accept a visible successful result only when the CLI identifies this model."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None, "invalid_provider_output", {}
    if not isinstance(value, dict):
        return None, "invalid_provider_output", {}
    if exit_code != 0 or value.get("is_error") is True or value.get("subtype") != "success":
        return None, "provider_error", {}
    usage = value.get("modelUsage")
    if not isinstance(usage, dict) or set(usage) != {MODEL}:
        return None, "model_identity_unverified", {}
    if value.get("permission_denials"):
        return None, "unexpected_tool_request", {}
    result = value.get("result")
    if not isinstance(result, str) or not result.strip():
        return None, "empty_output", {}
    if len(result.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return None, "output_limit", {}
    return result.strip(), None, {"actualModel": MODEL}


def start_consultation(*, task_id: str, prompt: str) -> dict[str, Any]:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        raise ClaudeConsultError("invalid consultation prompt")
    selected_binary = _binary()  # Pin one installed executable, without inference.
    directory = _directory(task_id)
    RUN_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        return public_status(task_id)
    workspace = directory / "workspace"
    workspace.mkdir(mode=0o700)
    _update(task_id, status="dispatching", createdAt=_now(), binaryPath=str(selected_binary))
    try:
        worker = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--claude-consult-worker", task_id,
             str(len(prompt)), hashlib.sha256(prompt.encode("utf-8")).hexdigest(), str(selected_binary)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, close_fds=True, start_new_session=True,
        )
        _update(task_id, workerPid=worker.pid)
        assert worker.stdin is not None
        worker.stdin.write(prompt)
        worker.stdin.close()
    except (OSError, BrokenPipeError):
        _update(task_id, status="failed", errorCategory="worker_start_failed", finishedAt=_now())
    return public_status(task_id)


def public_status(task_id: str) -> dict[str, Any]:
    state = _read(task_id)
    if state is None:
        return {"status": "dispatching", "taskId": task_id}
    status = state.get("status")
    if status in {"running", "dispatching"} and not _worker_matches(task_id, state.get("workerPid")):
        # A brief dispatch window allows the registered worker to be spawned.
        if status == "running" or _now() - int(state.get("createdAt") or 0) > 30_000:
            state = _update(task_id, status="failed", errorCategory="worker_lost", finishedAt=_now())
    payload = {k: state[k] for k in ("status", "model", "actualModel", "errorCategory", "finishedAt") if k in state}
    payload["taskId"] = task_id
    if state.get("status") == "completed":
        path = _directory(task_id) / "final.md"
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_OUTPUT_BYTES:
            return {**payload, "status": "failed", "errorCategory": "result_unavailable"}
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return {**payload, "status": "failed", "errorCategory": "result_unavailable"}
        if not text:
            return {**payload, "status": "failed", "errorCategory": "empty_output"}
        payload.update(finalResponse=text, latestTurn={"status": "completed", "finalResponse": text})
    return payload


def cancel_consultation(task_id: str) -> dict[str, Any]:
    state = _read(task_id)
    if state is None or state.get("status") in TERMINAL:
        return public_status(task_id)
    # Mark first, so a concurrently finishing worker cannot reverse cancellation.
    _update(task_id, status="interrupted", finishedAt=_now())
    if _worker_matches(task_id, state.get("workerPid")):
        try:
            os.killpg(state["workerPid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    return public_status(task_id)


def dispatch(registry: Any, project: Any, arguments: dict[str, Any], spec: dict[str, str]) -> dict[str, Any]:
    if arguments.get("context_mode") != "provided_only":
        return {"status": "failed", "errorCategory": "isolation_required",
                "content": "Claude consultations require context_mode=provided_only."}
    if not isinstance(arguments.get("request_key"), str):
        return {"status": "failed", "errorCategory": "request_key_required",
                "content": "Claude consultations require a stable request_key."}
    prompt = consultation_prompt(purpose=arguments["purpose"], question=arguments["question"],
                                 context_mode="provided_only", evidence_bundle=arguments.get("evidence_bundle"))
    fingerprint = consultation_request_fingerprint(
        alias=arguments["model"], purpose=arguments["purpose"], question=arguments["question"],
        context_mode="provided_only", evidence_bundle=arguments.get("evidence_bundle"))
    metadata = consultation_metadata(
        alias=arguments["model"], purpose=arguments["purpose"], question=arguments["question"],
        context_mode="provided_only", request_key=arguments["request_key"], request_fingerprint=fingerprint)
    try:
        record, replayed = registry.reserve_request(
            request_kind="consult_model", request_key=arguments["request_key"],
            request_fingerprint=fingerprint, backend="claude_consult", provider="anthropic",
            project=project.canonical, cwd=project.path, selection=MODEL, model=MODEL,
            access="read", status="dispatching", consultation=metadata,
            title=f"{project.canonical}: Claude Opus 5.5 consultation ({arguments['purpose']})")
    except ValueError as exc:
        return {"status": "failed", "errorCategory": "idempotency_conflict", "content": str(exc)}
    if not replayed:
        try:
            started = start_consultation(task_id=record["taskId"], prompt=prompt)
            record = registry.update(record["taskId"], status=started["status"])
        except ClaudeConsultError:
            record = registry.update(record["taskId"], status="failed",
                                     errorCategory="claude_unavailable", finishedAt=_now())
    payload = task_operation(registry, record, "read_task")
    return {**payload, "replayed": replayed, "automaticFallback": False,
            "contextMode": "provided_only", "access": "read", "markdownOnCompletion": True}


def task_operation(registry: Any, record: dict[str, Any], action: str) -> dict[str, Any]:
    if action == "continue_task":
        return {"status": "conflict", "taskId": record["taskId"],
                "content": "Consultations are immutable; use a new request_key for a new question."}
    missing_state = _read(record["taskId"]) is None
    if missing_state and record.get("status") not in TERMINAL and action == "cancel_task":
        current = {"status": "interrupted", "finishedAt": _now()}
    elif (missing_state and record.get("status") not in TERMINAL
          and _now() - int(record.get("createdAt") or 0) > 30_000):
        current = {"status": "failed", "errorCategory": "dispatch_lost", "finishedAt": _now()}
    else:
        current = cancel_consultation(record["taskId"]) if action == "cancel_task" else public_status(record["taskId"])
    if missing_state and current["status"] in TERMINAL and record.get("status") not in TERMINAL:
        # A late dispatch from the original request sees this terminal state and
        # cannot launch a provider after recovery or cancellation.
        RUN_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        _directory(record["taskId"]).mkdir(exist_ok=True, mode=0o700)
        _update(record["taskId"], **current)
    if current["status"] == "dispatching" and record.get("status") in TERMINAL:
        current = {"status": record["status"], "errorCategory": record.get("errorCategory")}
    changes = {"status": current["status"]}
    if current["status"] in TERMINAL:
        changes.update(finishedAt=current.get("finishedAt") or record.get("finishedAt") or _now(),
                       errorCategory=current.get("errorCategory"))
    if isinstance(current.get("actualModel"), str):
        changes["actualModel"] = current["actualModel"]
    record = registry.update(record["taskId"], **changes)
    payload = {**current, "taskId": record["taskId"], "taskReference": record["taskId"],
               "backend": "claude_consult", "provider": "anthropic", "model": MODEL,
               "advisory": True, "content": current.get("finalResponse") or
               f"Claude consultation: {current['status']}."}
    if current["status"] in TERMINAL:
        payload["executionStatus"] = current["status"]
        try:
            payload["consultationArtifact"] = persist_consultation(
                record, current["status"], current.get("finalResponse"))
        except (OSError, ValueError):
            payload["consultationArtifact"] = {"status": "failed", "errorCategory": "artifact_persistence"}
    return payload


def _worker(task_id: str, prompt_length: int, prompt_sha256: str, selected_binary: Path) -> int:
    directory = _directory(task_id)
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    prompt = sys.stdin.read(MAX_PROMPT_CHARS + 1)
    if (len(prompt) != prompt_length
            or hashlib.sha256(prompt.encode("utf-8")).hexdigest() != prompt_sha256):
        _update(task_id, status="failed", errorCategory="incomplete_input", finishedAt=_now())
        return 2
    if not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        _update(task_id, status="failed", errorCategory="invalid_prompt", finishedAt=_now())
        return 2
    state = _update(task_id, status="running", workerPid=os.getpid(), startedAt=_now())
    if state["status"] in TERMINAL:
        return 1
    raw_path, error_path = directory / "response.json", directory / "stderr.log"
    process = None
    class Interrupted(Exception):
        pass

    def interrupt(_signum, _frame):
        raise Interrupted()

    previous_handler = signal.signal(signal.SIGTERM, interrupt)
    try:
        with raw_path.open("w") as output, error_path.open("w") as error:
            os.chmod(raw_path, 0o600)
            os.chmod(error_path, 0o600)
            process = subprocess.Popen(command(selected_binary), stdin=subprocess.PIPE,
                                       stdout=output, stderr=error, text=True, close_fds=True,
                                       cwd=directory / "workspace", env=_environment())
            process.communicate(prompt, timeout=TIMEOUT_SECONDS)
        text, category, metadata = decode_result(raw_path.read_text(encoding="utf-8"), process.returncode)
        if text is None:
            _update(task_id, status="failed", errorCategory=category, finishedAt=_now())
            return 1
        final = directory / "final.md"
        final.write_text(text, encoding="utf-8")
        os.chmod(final, 0o600)
        _update(task_id, status="completed", finishedAt=_now(), **metadata)
        return 0
    except (subprocess.TimeoutExpired, Interrupted) as exc:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        interrupted = isinstance(exc, Interrupted)
        _update(task_id, status="interrupted" if interrupted else "failed",
                errorCategory=None if interrupted else "timeout", finishedAt=_now())
        return 143 if interrupted else 124
    except (OSError, UnicodeError, ClaudeConsultError):
        _update(task_id, status="failed", errorCategory="execution_failed", finishedAt=_now())
        return 1
    finally:
        # Do not retain provider debug output or its copy of response metadata.
        raw_path.unlink(missing_ok=True)
        error_path.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "--claude-consult-worker":
        raise SystemExit(_worker(sys.argv[2], int(sys.argv[3]), sys.argv[4], Path(sys.argv[5])))
    raise SystemExit("claude_consult.py is an internal fixed-model worker")
