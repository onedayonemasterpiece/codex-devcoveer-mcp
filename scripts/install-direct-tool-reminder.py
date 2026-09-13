#!/usr/bin/env python3
"""Install a small start_task reminder that points ChatGPT at direct DevCoveer tools."""
from __future__ import annotations

import argparse
import hashlib
import os
import py_compile
import shutil
import tempfile
import time
from pathlib import Path

BRIDGE = Path("/home/dev/.local/libexec/openai-codex-mcp/codex-chatgpt-bridge.py")
EXPECTED_BRIDGE_SHA256 = "42917bd76dcb03e4d18ec85ec5b42424022f31debf2ac721fbf20e60e9748c42"
MARKER = "_DIRECT_TOOL_REMINDER_STRONG"

DESCRIPTION_OLD = (
    '            "creation, and updates. Use read_task later to check status and obtain the result."),\n'
)
DESCRIPTION_NEW = (
    '            "creation, and updates. Before start_task for deterministic Git/host/runtime/model/task-state "\n'
    '            "checks, use direct tools first: project_probe, host_probe, list_models, and list_tasks/read_task. "\n'
    '            "start_task is for work that actually needs an agent. Use read_task later to check status and "\n'
    '            "obtain the result."),\n'
)

TOOL_RESULT_ANCHOR = (
    "def _tool_result(payload: dict[str, Any], *, error: bool = False) -> types.CallToolResult:\n"
)

HELPER_BLOCK = '''_DIRECT_TOOL_REMINDER_STRONG = (
    "project_probe", "host_probe", "list_models", "list_tasks", "read_task",
    "git status", "git log", "git diff", "git show", "git rev-parse",
    "remote head", "github status", "systemctl status", "journalctl",
    "service status", "listener", "runtime", "health", "sha256",
    "статус сервиса", "логи", "порт", "диск", "ветка", "коммит",
)
_DIRECT_TOOL_REMINDER_SOFT = (
    "probe", "bash", "shell", "grep", "find", "cat", "status", "log",
    "read", "check", "провер", "посмотр", "прочит",
)
_DIRECT_TOOL_REMINDER_TEXT = (
    "Direct DevCoveer tools may be faster here: project_probe for Git/read/search/diff/remote/CI facts; "
    "host_probe for services/logs/listeners/resources/runtime; list_models for live model health; "
    "list_tasks/read_task for task state. Prefer those for deterministic checks before starting another task."
)


def _direct_tool_reminder(prompt: str, access: str) -> str | None:
    text = prompt.casefold()
    if any(marker in text for marker in _DIRECT_TOOL_REMINDER_STRONG):
        return _DIRECT_TOOL_REMINDER_TEXT
    soft_hits = sum(1 for marker in _DIRECT_TOOL_REMINDER_SOFT if marker in text)
    if soft_hits >= 2 or (access == "read" and soft_hits >= 1):
        return _DIRECT_TOOL_REMINDER_TEXT
    return None


'''

START_RETURN_OLD = '''                return _tool_result({
                    "status": "running", "taskId": task_record["taskId"],
                    "taskReference": task_record["taskId"], "backend": "opencode",
                    "routedTo": "opencode", "routingReason": route["reason"],
                    "provider": route["provider"], "model": route["selection"],
                    "canonicalProject": project.canonical, "cwd": project.path,
                    "access": access, "selectionVerified": True,
                    "content": "OpenCode task started in the background. Use read_task with "
                               "the returned taskId to obtain the result.",
                })
'''

START_RETURN_NEW = '''                payload = {
                    "status": "running", "taskId": task_record["taskId"],
                    "taskReference": task_record["taskId"], "backend": "opencode",
                    "routedTo": "opencode", "routingReason": route["reason"],
                    "provider": route["provider"], "model": route["selection"],
                    "canonicalProject": project.canonical, "cwd": project.path,
                    "access": access, "selectionVerified": True,
                    "content": "OpenCode task started in the background. Use read_task with "
                               "the returned taskId to obtain the result.",
                }
                direct_tools_hint = _direct_tool_reminder(arguments["prompt"], access)
                if direct_tools_hint is not None:
                    payload["directToolsHint"] = direct_tools_hint
                    payload["content"] += "\\n" + direct_tools_hint
                return _tool_result(payload)
'''


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one source match, found {count}")
    return source.replace(old, new, 1)


def transform_bridge(source: str) -> str:
    if MARKER in source:
        return source
    source = replace_once(source, DESCRIPTION_OLD, DESCRIPTION_NEW, "start_task description")
    source = replace_once(
        source, TOOL_RESULT_ANCHOR, HELPER_BLOCK + TOOL_RESULT_ANCHOR, "tool result helper anchor"
    )
    source = replace_once(source, START_RETURN_OLD, START_RETURN_NEW, "OpenCode start_task result")
    return source


def compile_candidate(source: str) -> None:
    fd, path = tempfile.mkstemp(prefix="devcoveer-direct-tool-reminder-", suffix=".py")
    os.close(fd)
    pyc = path + ".pyc"
    try:
        Path(path).write_text(source, encoding="utf-8")
        py_compile.compile(path, cfile=pyc, doraise=True)
    finally:
        for item in (path, pyc):
            try:
                os.unlink(item)
            except FileNotFoundError:
                pass


def atomic_write(path: Path, source: str) -> None:
    mode = path.stat().st_mode & 0o7777
    fd, temp_name = tempfile.mkstemp(prefix=".direct-tool-reminder-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(source)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    current_bytes = BRIDGE.read_bytes()
    current_hash = sha256(current_bytes)
    source = current_bytes.decode("utf-8")
    already_installed = MARKER in source
    if not already_installed and current_hash != EXPECTED_BRIDGE_SHA256:
        raise SystemExit(
            f"installed bridge SHA-256 {current_hash} does not match expected {EXPECTED_BRIDGE_SHA256}"
        )

    candidate = transform_bridge(source)
    compile_candidate(candidate)
    candidate_hash = sha256(candidate.encode("utf-8"))
    print(
        {
            "status": "ok",
            "applyRequested": args.apply,
            "alreadyInstalled": already_installed,
            "bridgeOldSha256": current_hash,
            "bridgeNewSha256": candidate_hash,
        }
    )
    if not args.apply or already_installed:
        return 0

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = BRIDGE.with_name(BRIDGE.name + f".bak.direct-tool-reminder-{stamp}")
    shutil.copy2(BRIDGE, backup)
    try:
        atomic_write(BRIDGE, candidate)
        py_compile.compile(str(BRIDGE), doraise=True)
    except BaseException:
        shutil.copy2(backup, BRIDGE)
        raise
    print({"status": "installed", "restartRequired": True, "backup": backup.name})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
