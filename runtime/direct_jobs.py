from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from direct_probe_core import ProbeError, _git, _redact_text, _run_process

JOB_ID_RE = re.compile(r"^job_[0-9a-f]{24}$")
JOB_ROOT = Path.home() / ".local" / "share" / "openai-codex-mcp" / "direct-jobs"


def _systemd_env() -> dict[str, str]:
    uid = os.getuid()
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/home/dev"),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "XDG_RUNTIME_DIR": f"/run/user/{uid}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus"}


def _job_dir(job_id: str) -> Path:
    if JOB_ID_RE.fullmatch(job_id) is None:
        raise ProbeError("invalid_input", "invalid direct job id")
    directory = JOB_ROOT / job_id
    if directory.is_symlink():
        raise ProbeError("job_denied", "job directory must not be a symlink", status="denied")
    return directory


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".job-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def _atomic_json_if_absent(path: Path, data: dict[str, Any]) -> bool:
    """Publish a terminal receipt only if the runner has not published one."""
    fd, tmp = tempfile.mkstemp(prefix=".job-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ProbeError("job_not_found", "direct job metadata is missing", status="not_found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProbeError("job_invalid", "direct job metadata is invalid") from exc
    if not isinstance(data, dict):
        raise ProbeError("job_invalid", "direct job metadata is invalid")
    return data


def _owned(root: Path, job_id: str) -> tuple[Path, dict[str, Any]]:
    directory = _job_dir(job_id)
    spec = _read_json(directory / "spec.json")
    if spec.get("jobId") != job_id or spec.get("root") != str(root.resolve()):
        raise ProbeError("job_denied", "direct job belongs to another project/worktree", status="denied")
    return directory, spec


async def source_state(root: Path) -> dict[str, Any]:
    """Small receipt, not a claim that untracked contents are fully fingerprinted."""
    try:
        head = await _git(root, "rev-parse", "HEAD", output_limit=128)
        state = await _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
        diff = await _git(root, "diff", "--binary", "HEAD", "--")
        complete = not state.truncated and not diff.truncated and not any(
            line.startswith(b"?? ") for line in state.stdout.splitlines())
        return {"head": head.stdout.decode().strip(), "dirty": bool(state.stdout),
                "tracked_diff_sha256": hashlib.sha256(diff.stdout).hexdigest() if not diff.truncated else None,
                "complete": complete}
    except (ProbeError, OSError):
        return {"head": None, "complete": False}


async def start_job(root: Path, action: dict[str, Any], *, project_id: str | None = None) -> dict[str, Any]:
    run_action = {k: action[k] for k in ("tool", "path", "script", "cwd", "args", "python_env", "env") if k in action}
    timeout = int(action.get("timeout_seconds", 900))
    run_action.update(kind="run_tool", timeout_seconds=timeout)
    request_key = action.get("request_key")
    fingerprint = hashlib.sha256(json.dumps(run_action, sort_keys=True).encode()).hexdigest()
    root_name = str(root.resolve(strict=True))
    suffix = hashlib.sha256((root_name + "\0" + request_key).encode()).hexdigest()[:24] if request_key else secrets.token_hex(12)
    job_id = "job_" + suffix
    directory = _job_dir(job_id)
    try:
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError:
        # mkdir is the launch reservation: concurrent identical requests never spawn twice.
        if not (directory / "spec.json").exists():
            return {"status": "initializing", "action": "job_start", "job_id": job_id, "reused": True}
        _directory, previous = _owned(root, job_id)
        if previous.get("fingerprint") != fingerprint:
            raise ProbeError("idempotency_conflict", "request_key was already used for a different command", status="denied")
        result = await job_status(root, job_id)
        return {**result, "action": "job_start", "reused": True}
    unit = "devcoveer-job-" + suffix
    spec = {"version": 2, "jobId": job_id, "root": root_name, "unit": unit, "action": run_action,
            "created_at": time.time(), "fingerprint": fingerprint, "idempotent": bool(request_key),
            "project": project_id or root.name}
    _atomic_json(directory / "spec.json", spec)
    runner = Path(__file__).with_name("direct_job_runner.py").resolve(strict=True)
    argv = ["/usr/bin/systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
            f"--property=RuntimeMaxSec={timeout + 60}s", "--property=KillMode=control-group",
            "--property=TimeoutStopSec=10s", sys.executable, str(runner), str(directory / "spec.json")]
    try:
        result = await _run_process(argv, env=_systemd_env(), timeout=15, output_limit=16 * 1024)
    except BaseException:
        # An interrupted RPC is not evidence that systemd did not accept the job.
        # Preserve the reservation; status/readback resolves it, never automatic re-launch.
        raise
    if result.exit_code != 0:
        err, _ = _redact_text(result.stderr.decode("utf-8", "replace"))
        _atomic_json(directory / "result.json", {"status": "failed", "finished_at": time.time(),
                     "error": {"category": "job_start", "message": err[-1200:]}})
        raise ProbeError("job_start", err[-1200:] or "could not start direct job", exit_code=result.exit_code)
    return {"status": "running", "action": "job_start", "job_id": job_id, "unit": unit,
            "timeout_seconds": timeout, "reused": False}


def _logs(directory: Path, tail_lines: int) -> dict[str, Any]:
    output: dict[str, Any] = {}
    latest = None
    for name in ("stdout", "stderr"):
        path = directory / (name + ".log")
        if not path.is_file() or path.is_symlink():
            continue
        st = path.stat()
        latest = max(latest or 0, st.st_mtime)
        with path.open("rb") as handle:
            handle.seek(max(0, st.st_size - 32768))
            data = handle.read(32768).decode("utf-8", "replace")
        text, _ = _redact_text(data)
        output[name] = "\n".join(text.splitlines()[-tail_lines:]) if tail_lines else ""
    return {"output_tail": output, "last_output_at": latest}


def _public_result(path: Path, tail_lines: int) -> dict[str, Any]:
    payload = _read_json(path)
    result = payload.get("result")
    if isinstance(result, dict):
        for name in ("stdout", "stderr"):
            text = result.get(name)
            if isinstance(text, str):
                result[name] = "\n".join(text.splitlines()[-tail_lines:])[-32768:] if tail_lines else ""
                if result[name] != text:
                    result["readback_truncated"] = True
    return payload


async def job_status(root: Path, job_id: str, *, tail_lines: int = 40) -> dict[str, Any]:
    directory, spec = _owned(root, job_id)
    base = {"action": "job_status", "job_id": job_id, "created_at": spec.get("created_at"),
            "cwd": spec["root"], **_logs(directory, max(0, min(tail_lines, 200)))}
    result_path = directory / "result.json"
    if result_path.is_file():
        return {**base, **_public_result(result_path, tail_lines)}
    if (directory / "cancelled").is_file():
        return {**base, "status": "cancelled"}
    result = await _run_process(
        ["/usr/bin/systemctl", "--user", "show", spec["unit"],
         "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus", "--no-pager"],
        env=_systemd_env(), timeout=10, output_limit=8192)
    props = dict(line.split("=", 1) for line in result.stdout.decode("utf-8", "replace").splitlines() if "=" in line)
    if result.exit_code != 0:
        return {**base, "status": "unknown", "unit_state": props, "retryable": True}
    if props.get("ActiveState") in {"active", "activating", "reloading", "deactivating"}:
        return {**base, "status": "running", "unit_state": props}
    if time.time() - spec.get("created_at", 0) < 20 and props.get("LoadState") == "not-found":
        return {**base, "status": "starting", "unit_state": props}
    # Recheck after observing the unit: the runner may just have committed its result.
    if result_path.is_file():
        return {**base, **_public_result(result_path, tail_lines)}
    category = {"timeout": "timeout", "oom-kill": "oom"}.get(props.get("Result"), "result_missing")
    terminal = {"status": "interrupted", "finished_at": time.time(), "unit_state": props,
                "error": {"category": category, "message": "job exited without a complete result; inspect output_tail"}}
    if _atomic_json_if_absent(result_path, terminal):
        return {**base, **terminal}
    return {**base, **_public_result(result_path, tail_lines)}


def list_jobs(root: Path, *, limit: int = 20, before: str | None = None) -> dict[str, Any]:
    """Metadata-only, cursor-paginated listing; no expensive status poll of every unit."""
    rows = []
    for directory in JOB_ROOT.glob("job_*"):
        try:
            _directory, spec = _owned(root, directory.name)
            result = _read_json(directory / "result.json") if (directory / "result.json").is_file() else {}
        except (ProbeError, OSError):
            continue
        if result.get("cleaned"):
            continue
        cursor = f'{spec.get("created_at", 0):020.6f}:{directory.name}'
        if before is not None and cursor >= before:
            continue
        rows.append({"job_id": directory.name, "created_at": spec.get("created_at"), "cursor": cursor,
                     "status": result.get("status", "not_polled"), "tool": spec["action"].get("tool"),
                     "path": spec["action"].get("path"), "script": spec["action"].get("script")})
    rows.sort(key=lambda row: row["cursor"], reverse=True)
    limit = max(1, min(limit, 100))
    return {"status": "ok", "action": "job_list", "jobs": rows[:limit],
            "next_cursor": rows[limit - 1]["cursor"] if len(rows) > limit else None}


async def cancel_job(root: Path, job_id: str) -> dict[str, Any]:
    directory, spec = _owned(root, job_id)
    if (directory / "result.json").is_file():
        return {"status": "terminal", "action": "job_cancel", "job_id": job_id}
    result = await _run_process(["/usr/bin/systemctl", "--user", "stop", spec["unit"]],
                                env=_systemd_env(), timeout=15, output_limit=8192)
    if result.exit_code != 0:
        err, _ = _redact_text(result.stderr.decode("utf-8", "replace"))
        raise ProbeError("job_cancel", err[-1200:] or "could not cancel direct job", exit_code=result.exit_code)
    if (directory / "result.json").is_file():
        return {"status": "terminal", "action": "job_cancel", "job_id": job_id}
    if not _atomic_json_if_absent(directory / "result.json", {"status": "cancelled", "finished_at": time.time()}):
        return {"status": "terminal", "action": "job_cancel", "job_id": job_id}
    return {"status": "cancelled", "action": "job_cancel", "job_id": job_id}


def cleanup_job(root: Path, job_id: str) -> dict[str, Any]:
    import shutil
    directory, spec = _owned(root, job_id)
    if not (directory / "result.json").is_file() and not (directory / "cancelled").is_file():
        raise ProbeError("job_active", "active direct job cannot be cleaned", status="denied")
    if spec.get("idempotent"):
        result = _read_json(directory / "result.json") if (directory / "result.json").is_file() else {"status": "cancelled"}
        # Retain a small receipt so reusing a cleaned request_key never launches it again.
        compact = {key: result[key] for key in ("status", "finished_at", "error") if key in result}
        _atomic_json(directory / "result.json", {**compact, "cleaned": True})
        for name in ("stdout.log", "stderr.log", "cancelled"):
            (directory / name).unlink(missing_ok=True)
        return {"status": "ok", "action": "job_cleanup", "job_id": job_id, "artifacts_removed": True, "receipt_retained": True}
    shutil.rmtree(directory)
    return {"status": "ok", "action": "job_cleanup", "job_id": job_id, "removed": True}
