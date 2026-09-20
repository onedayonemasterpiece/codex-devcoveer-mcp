from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from direct_probe_core import ProbeError, _redact_text, _run_process, _safe_worktree_path
from direct_ops2_worktrees import resolve_repo_worktree

SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{1,254}$")
COMPONENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]+/[A-Za-z0-9_.$]+$")
EXTRA_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
RUNNER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]+/[A-Za-z0-9_.$]+$")


def _adb() -> str:
    for candidate in (
        "/usr/local/bin/adb",
        "/usr/bin/adb",
        str(Path.home() / "Android" / "Sdk" / "platform-tools" / "adb"),
    ):
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise ProbeError("tool_unavailable", "adb is not installed", status="unavailable")


def _serial(raw: str | None) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, str) or SERIAL_RE.fullmatch(raw) is None:
        raise ProbeError("invalid_input", "invalid adb serial")
    return ["-s", raw]


def _package(raw: str) -> str:
    if not isinstance(raw, str) or PACKAGE_RE.fullmatch(raw) is None:
        raise ProbeError("invalid_input", "invalid Android package name")
    return raw


def _env() -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/home/dev"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "ANDROID_SDK_ROOT": os.environ.get("ANDROID_SDK_ROOT", str(Path.home() / "Android" / "Sdk")),
    }


async def _run(args: list[str], *, timeout: float = 60, output_limit: int = 256 * 1024) -> tuple[Any, str, str, bool]:
    result = await _run_process([_adb(), *args], env=_env(), timeout=timeout, output_limit=output_limit)
    stdout, out_red = _redact_text(result.stdout.decode("utf-8", "replace"))
    stderr, err_red = _redact_text(result.stderr.decode("utf-8", "replace"))
    return result, stdout, stderr, out_red or err_red


async def adb_devices(_check: dict[str, Any]) -> dict[str, Any]:
    result, stdout, stderr, redacted = await _run(["devices", "-l"], timeout=15, output_limit=32 * 1024)
    if result.exit_code != 0:
        raise ProbeError("adb", stderr[-1200:] or "adb devices failed", exit_code=result.exit_code)
    devices = []
    for line in stdout.splitlines():
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        meta: dict[str, str] = {}
        for token in parts[2:]:
            key, sep, value = token.partition(":")
            if sep and key in {"product", "model", "device", "transport_id"}:
                meta[key] = value[:200]
        devices.append({"serial": serial[:128], "state": state[:64], **meta})
    return {"devices": devices, "count": len(devices), "redacted": redacted}


async def adb_package_state(check: dict[str, Any]) -> dict[str, Any]:
    package = _package(check["package"])
    serial = check.get("serial")
    prefix = _serial(serial)
    path_result, paths, path_err, path_red = await _run(
        [*prefix, "shell", "pm", "path", package], timeout=15, output_limit=16 * 1024
    )
    installed = path_result.exit_code == 0 and any(line.startswith("package:") for line in paths.splitlines())
    pid_result, pid_text, _pid_err, pid_red = await _run(
        [*prefix, "shell", "pidof", package], timeout=10, output_limit=4096
    )
    pids = [int(x) for x in pid_text.split() if x.isdigit()] if pid_result.exit_code == 0 else []
    return {
        "package": package, "serial": serial, "installed": installed, "pids": pids[:32],
        "path_count": sum(1 for line in paths.splitlines() if line.startswith("package:")) if installed else 0,
        "redacted": path_red or pid_red,
        "pm_error": None if installed else path_err[-500:],
    }


async def adb_package_logcat(check: dict[str, Any]) -> dict[str, Any]:
    package = _package(check["package"])
    serial = check.get("serial")
    prefix = _serial(serial)
    pid_result, pid_text, pid_err, _ = await _run(
        [*prefix, "shell", "pidof", package], timeout=10, output_limit=4096
    )
    pids = [x for x in pid_text.split() if x.isdigit()]
    if pid_result.exit_code != 0 or not pids:
        raise ProbeError("not_running", pid_err[-500:] or "package has no running process", status="not_found")
    lines = min(int(check.get("lines", 200)), 1000)
    # Android logcat accepts one --pid flag per process.
    pid_args = [arg for pid in pids[:8] for arg in ("--pid", pid)]
    result, stdout, stderr, redacted = await _run(
        [*prefix, "logcat", "-d", "-t", str(lines), *pid_args],
        timeout=20, output_limit=512 * 1024,
    )
    if result.exit_code != 0:
        raise ProbeError("adb", stderr[-1200:] or "logcat failed", exit_code=result.exit_code)
    return {
        "package": package, "serial": serial, "pids": [int(x) for x in pids[:8]],
        "text": stdout, "redacted": redacted, "truncated": result.truncated,
        "complete": not result.truncated,
    }


async def _project_root_for_action(root: Path, action: dict[str, Any]) -> Path:
    raw = action.get("worktree")
    if raw is None:
        return root.resolve(strict=True)
    return await resolve_repo_worktree(root, raw)


def _extras(action: dict[str, Any]) -> list[str]:
    raw = action.get("extras") or {}
    if not isinstance(raw, dict) or len(raw) > 32:
        raise ProbeError("invalid_input", "Android extras must be a bounded object")
    args: list[str] = []
    for key, value in raw.items():
        if not isinstance(key, str) or EXTRA_KEY_RE.fullmatch(key) is None:
            raise ProbeError("invalid_input", "invalid Android extra key")
        if isinstance(value, bool):
            args.extend(["--ez", key, "true" if value else "false"])
        elif isinstance(value, int) and not isinstance(value, bool):
            args.extend(["--ei", key, str(value)])
        elif isinstance(value, str) and len(value) <= 1000 and "\0" not in value and "\n" not in value and "\r" not in value:
            args.extend(["--es", key, value])
        else:
            raise ProbeError("invalid_input", "Android extra values must be bounded string/int/bool")
    return args


async def adb_action(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    operation = action["operation"]
    serial = action.get("serial")
    prefix = _serial(serial)

    if operation == "install":
        project_root = await _project_root_for_action(root, action)
        raw_path = action.get("apk")
        if not isinstance(raw_path, str) or not raw_path:
            raise ProbeError("invalid_input", "install requires apk path")
        path = _safe_worktree_path(project_root, raw_path, require_file=True)
        if path.suffix.lower() != ".apk" or path.is_symlink():
            raise ProbeError("invalid_input", "install requires a regular repository APK")
        result, stdout, stderr, redacted = await _run(
            [*prefix, "install", "-r", "--no-streaming", str(path)],
            timeout=float(action.get("timeout_seconds", 300)), output_limit=128 * 1024,
        )
        if result.exit_code != 0:
            raise ProbeError("adb_install", stderr[-1200:] or stdout[-1200:] or "adb install failed", exit_code=result.exit_code)
        return {
            "status": "ok", "action": "adb", "operation": operation, "serial": serial,
            "apk": raw_path, "stdout": stdout, "redacted": redacted,
        }

    raw_package = action.get("package")
    if not isinstance(raw_package, str):
        raise ProbeError("invalid_input", f"{operation} requires package")
    package = _package(raw_package)
    if operation == "uninstall":
        result, stdout, stderr, redacted = await _run([*prefix, "uninstall", package], timeout=60)
    elif operation == "force_stop":
        result, stdout, stderr, redacted = await _run([*prefix, "shell", "am", "force-stop", package], timeout=30)
    elif operation == "clear_data":
        if action.get("confirm_clear_data") is not True:
            raise ProbeError("confirmation_required", "clear_data requires confirm_clear_data=true", status="denied")
        result, stdout, stderr, redacted = await _run([*prefix, "shell", "pm", "clear", package], timeout=60)
    elif operation == "start_activity":
        component = action.get("component")
        if not isinstance(component, str) or COMPONENT_RE.fullmatch(component) is None or not component.startswith(package + "/"):
            raise ProbeError("invalid_input", "component must belong to the requested package")
        result, stdout, stderr, redacted = await _run(
            [*prefix, "shell", "am", "start", "-W", "-n", component, *_extras(action)],
            timeout=float(action.get("timeout_seconds", 60)), output_limit=128 * 1024,
        )
    elif operation == "instrument":
        runner = action.get("runner")
        if not isinstance(runner, str) or RUNNER_RE.fullmatch(runner) is None or not runner.startswith(package + "/"):
            raise ProbeError("invalid_input", "instrumentation runner must belong to the requested package")
        result, stdout, stderr, redacted = await _run(
            [*prefix, "shell", "am", "instrument", "-w", "-r", *_extras(action), runner],
            timeout=float(action.get("timeout_seconds", 900)), output_limit=512 * 1024,
        )
    else:
        raise ProbeError("invalid_input", "unsupported adb action")

    if result.exit_code != 0:
        raise ProbeError("adb", stderr[-1200:] or stdout[-1200:] or f"adb {operation} failed", exit_code=result.exit_code)
    return {
        "status": "ok", "action": "adb", "operation": operation, "package": package,
        "serial": serial, "stdout": stdout, "redacted": redacted,
    }
