"""Isolated native-Codex execution for read-only model consultations."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CODEX_BINARY = "/home/dev/.local/bin/codex"
RUN_ROOT = Path("/home/dev/.local/share/openai-codex-mcp/codex-consultations")
TASK_RE = re.compile(r"^dvt_[0-9a-f]{32}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
MAX_PROMPT_CHARS = 120_000
MAX_RESULT_CHARS = 120_000
WORKER_TIMEOUT_SECONDS = 900


class CodexConsultError(RuntimeError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _run_dir(task_id: str) -> Path:
    if TASK_RE.fullmatch(task_id) is None:
        raise CodexConsultError("invalid consultation task id")
    return RUN_ROOT / task_id


def _status_path(task_id: str) -> Path:
    return _run_dir(task_id) / "status.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, tmp = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _read_status(task_id: str) -> dict[str, Any] | None:
    path = _status_path(task_id)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 64_000:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _worker_matches(task_id: str, pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    proc = Path(f"/proc/{pid}/cmdline")
    try:
        raw = proc.read_bytes()
    except OSError:
        return False
    if not raw:
        return False
    text = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    return "--codex-consult-worker" in text and task_id in text


def start_consultation(*, task_id: str, prompt: str, model: str, effort: str) -> dict[str, Any]:
    run_dir = _run_dir(task_id)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        raise CodexConsultError("invalid consultation prompt")
    if MODEL_RE.fullmatch(model or "") is None:
        raise CodexConsultError("invalid Codex model id")
    if effort not in EFFORTS:
        raise CodexConsultError("invalid reasoning effort")
    existing = _read_status(task_id)
    if isinstance(existing, dict) and existing.get("status") in {
        "running", "completed", "failed", "interrupted"
    }:
        return public_status(task_id)

    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(run_dir, 0o700)
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(workspace, 0o700)

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--codex-consult-worker",
        task_id,
        model,
        effort,
    ]
    try:
        worker = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            close_fds=True,
            start_new_session=True,
        )
        assert worker.stdin is not None
        worker.stdin.write(prompt)
        worker.stdin.close()
    except (OSError, BrokenPipeError) as exc:
        raise CodexConsultError("isolated Codex consultation worker could not start") from exc

    _atomic_json(_status_path(task_id), {
        "schema": 1,
        "status": "running",
        "workerPid": worker.pid,
        "model": model,
        "reasoningEffort": effort,
        "startedAt": _now_ms(),
    })
    return public_status(task_id)


def public_status(task_id: str) -> dict[str, Any]:
    run_dir = _run_dir(task_id)
    value = _read_status(task_id)
    if value is None:
        return {"status": "dispatching", "taskId": task_id}

    status = str(value.get("status") or "failed")
    if status == "running" and not _worker_matches(task_id, value.get("workerPid")):
        value = {
            **value,
            "status": "failed",
            "errorCategory": "worker_lost",
            "finishedAt": _now_ms(),
        }
        _atomic_json(_status_path(task_id), value)
        status = "failed"

    payload: dict[str, Any] = {
        "status": status,
        "taskId": task_id,
        "model": value.get("model"),
        "reasoningEffort": value.get("reasoningEffort"),
    }
    if isinstance(value.get("errorCategory"), str):
        payload["errorCategory"] = value["errorCategory"]
    if status == "completed":
        final_path = run_dir / "final.md"
        if final_path.is_file() and not final_path.is_symlink() and final_path.stat().st_size <= 1_000_000:
            try:
                text = final_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                text = ""
            if text:
                payload["finalResponse"] = text[:MAX_RESULT_CHARS]
                payload["latestTurn"] = {
                    "status": "completed",
                    "finalResponse": payload["finalResponse"],
                }
            else:
                payload["status"] = "failed"
                payload["errorCategory"] = "empty_output"
    return payload


def cancel_consultation(task_id: str) -> dict[str, Any]:
    value = _read_status(task_id)
    if value is None:
        return {"status": "not_running", "taskId": task_id}
    if value.get("status") != "running":
        return {"status": "not_running", "taskId": task_id}
    pid = value.get("workerPid")
    if not _worker_matches(task_id, pid):
        return public_status(task_id)
    try:
        os.killpg(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        return public_status(task_id)
    except OSError as exc:
        raise CodexConsultError("isolated Codex consultation could not be cancelled") from exc
    interrupted = {
        **value,
        "status": "interrupted",
        "errorCategory": None,
        "finishedAt": _now_ms(),
    }
    _atomic_json(_status_path(task_id), interrupted)
    return {"status": "interrupted", "taskId": task_id}


def _worker(task_id: str, model: str, effort: str) -> int:
    run_dir = _run_dir(task_id)
    workspace = run_dir / "workspace"
    final_path = run_dir / "final.md"
    events_path = run_dir / "events.jsonl"
    stderr_path = run_dir / "stderr.log"
    prompt = sys.stdin.read(MAX_PROMPT_CHARS + 1)
    if len(prompt) > MAX_PROMPT_CHARS or not prompt.strip():
        _atomic_json(_status_path(task_id), {
            "schema": 1, "status": "failed", "workerPid": os.getpid(),
            "model": model, "reasoningEffort": effort,
            "errorCategory": "invalid_prompt", "finishedAt": _now_ms(),
        })
        return 2

    command = [
        CODEX_BINARY,
        "--ask-for-approval", "never",
        "-c", 'default_permissions="devcoveer-provided-only"',
        "-c", 'permissions.devcoveer-provided-only.filesystem={":root"="deny",":minimal"="read",":tmpdir"="deny",":slash_tmp"="deny"}',
        "-c", "permissions.devcoveer-provided-only.network.enabled=false",
        "-c", 'shell_environment_policy.inherit="none"',
        "-c", 'web_search="disabled"',
        "-c", f'model_reasoning_effort="{effort}"',
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C", str(workspace),
        "--json",
        "--output-last-message", str(final_path),
        "--model", model,
        "-",
    ]
    child_env = {
        "HOME": "/home/dev",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "USER": "dev",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    _atomic_json(_status_path(task_id), {
        "schema": 1, "status": "running", "workerPid": os.getpid(),
        "model": model, "reasoningEffort": effort, "startedAt": _now_ms(),
    })
    try:
        with events_path.open("wb") as out, stderr_path.open("wb") as err:
            os.chmod(events_path, 0o600)
            os.chmod(stderr_path, 0o600)
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
                cwd=workspace,
                env=child_env,
                text=True,
                start_new_session=False,
            )
            try:
                process.communicate(input=prompt, timeout=WORKER_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                _atomic_json(_status_path(task_id), {
                    "schema": 1, "status": "failed", "workerPid": os.getpid(),
                    "model": model, "reasoningEffort": effort,
                    "errorCategory": "timeout", "finishedAt": _now_ms(),
                })
                return 124
    except OSError:
        _atomic_json(_status_path(task_id), {
            "schema": 1, "status": "failed", "workerPid": os.getpid(),
            "model": model, "reasoningEffort": effort,
            "errorCategory": "execution_failed", "finishedAt": _now_ms(),
        })
        return 1

    ok = process.returncode == 0 and final_path.is_file() and final_path.stat().st_size > 0
    _atomic_json(_status_path(task_id), {
        "schema": 1,
        "status": "completed" if ok else "failed",
        "workerPid": os.getpid(),
        "model": model,
        "reasoningEffort": effort,
        "exitCode": int(process.returncode),
        "errorCategory": None if ok else "model_error",
        "finishedAt": _now_ms(),
    })
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--codex-consult-worker":
        raise SystemExit(_worker(sys.argv[2], sys.argv[3], sys.argv[4]))
    raise SystemExit("codex_consult.py is an internal worker")
