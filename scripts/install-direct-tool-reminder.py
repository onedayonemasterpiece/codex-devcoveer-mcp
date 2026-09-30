#!/usr/bin/env python3
"""Install ChatGPT routing and source-authorship reminders into the DevCoveer bridge."""
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
LEGACY_INSTALLED_BRIDGE_SHA256 = "7d61e2bdb2938c8ca0c13b5487798bd81d643818a8c0b55f2520fd45c17e4548"
LEGACY_MARKER = "_DIRECT_TOOL_REMINDER_STRONG"
MARKER = "_CHATGPT_OPERATING_REMINDER_V2"

DESCRIPTION_OLD = (
    '            "creation, and updates. Use read_task later to check status and obtain the result."),\n'
)
DESCRIPTION_V1 = (
    '            "creation, and updates. Before start_task for deterministic Git/host/runtime/model/task-state "\n'
    '            "checks and deterministic operations, use direct tools first: project_probe, host_probe, project_action, host_action, list_models, and list_tasks/read_task. "\n'
    '            "start_task is for work that actually needs an agent. Use read_task later to check status and "\n'
    '            "obtain the result."),\n'
)
DESCRIPTION_NEW = (
    '            "creation, and updates. Before start_task for deterministic Git/host/runtime/model/task-state checks and other deterministic work, route to typed/direct tools: "\n'
    '            "project_probe for Git/source/read/search/diff/remote/CI facts; host_probe for service/log/listener/resource/runtime facts; direct_ops_v2, project_action, and host_action for deterministic mutations, tests, builds, Git, GitHub, and registered host actions. "\n'
    '            "If a current direct operation name or parameter schema is unknown, call direct_ops_v2 with operation=describe before choosing an agent. "\n'
    '            "Use project_action for registered project test/build/deploy profiles. Known short deterministic tests/builds use run_tool; known long-running deterministic tests/builds use job_start then job_status, with python_env=auto to reuse the existing project environment rather than create a venv. "\n'
    '            "Use git_fetch for fetch without checkout; use git_switch_ff for an exact existing-branch switch+fast-forward; use typed github_workflow_dispatch/github_run_cancel for applicable GitHub workflow operations. Use apply_source_patch or stage_source_handoff for inert source patches and artifact_fetch for arbitrary HTTPS project artifacts with hashing/provenance. Use list_models for model state and list_tasks/read_task for task state. "\n'
    '            "These typed operations are preferred semantic fast paths, not a capability boundary. If a legitimate operation is absent from the typed surface, start_task remains available as the full-power reasoning escape hatch; do not invent a restriction merely because a typed primitive is missing. "\n'
    '            "For continuation work that names or implies a checkpoint, branch, SHA or prior worktree, first inspect find_projects workspaceContext and project_probe worktree_list; exact repository-name resolution identifies only one physical checkout. Address the matching project before read/write/test/build and pass expected_head on guarded direct actions when a checkpoint SHA is known. When project_probe git_state provides tracked_workspace_digest, pass expected_workspace_digest on guarded local actions when the exact tracked workspace must be preserved across parallel work. stage_source_handoff isolates a ChatGPT-authored patch; it does not select an existing worktree. "\n'
    '            "For browser UI interaction, DOM observation, console/network inspection, screenshots, navigation or clicks, use my-browser-bridge directly when it is available to ChatGPT instead of starting OpenCode solely to drive Playwright. "\n'
    '            "Before web work, use project_probe instructions with scope=home/project for exact AGENTS.md reads and web_preflight with the package cwd. Browser suites and unknown-duration checks use job_start then job_status; pass nonsecret CHECK_BASE/CHECK_OUTPUT through env, with path relative to project root even when cwd is set. Prefer exact probes over broad HOME enumeration; use broader agent investigation only when it is actually required, and do not borrow browser binaries from other checkouts. "\n'
    '            "For a clear production/live incident, material regression, failed recovery/deploy, repeated live failure, or unreproduced production bug, start_task automatically opens or reuses an incident, injects related history into agent context, and returns incidentId; this is fail-open and never an approval gate. Use incident_search/incident_get, host_probe inventory plus logs/log_search/health, safe service/config metadata probes, and registered-root Git facts before remediation. Direct Ops receipts correlate to the active incident. Ordinary local/unit-test failures do not require an incident. "\n'
    '            "When changing failure-prone live behavior, ensure concise structured diagnostic logging exists at external boundaries, decisions, retries, state transitions, failures and successful outcomes; include correlation/release/component context when useful and never log secrets. "\n'
    '            "In normal ChatGPT-led mode, do not delegate repository code or documentation authoring to OpenCode. ChatGPT should author the exact code/docs change itself, use direct project_action writes when available, and use stage_source_handoff when direct repository writing is unavailable; use the supplied patch for verification/commit/push rather than rewrite source/docs. "\n'
    '            "For multi-step work, use direct_ops_v2 work_open once, carry work_id, and recover with work_resume after an interruption. Use stable request_key on mutations and read the saved operation result before retrying. Do not restart discovery or create a new agent task solely because a response was lost. Use start_task only when the work requires reasoning beyond typed operations or an unforeseen operation absent from the typed surface. Use read_task later to check status and obtain the result."),\n'
)

TOOL_RESULT_ANCHOR = (
    "def _tool_result(payload: dict[str, Any], *, error: bool = False) -> types.CallToolResult:\n"
)

HELPER_BLOCK_V1 = '''_DIRECT_TOOL_REMINDER_STRONG = (
    "project_probe", "host_probe", "project_action", "host_action", "list_models", "list_tasks", "read_task",
    "git status", "git log", "git diff", "git show", "git rev-parse", "fast-forward",
    "remote head", "github status", "systemctl status", "journalctl",
    "service status", "listener", "runtime", "health", "sha256",
    "pytest", "test runner", "run tests", "build", "deploy", "restart",
    "статус сервиса", "логи", "порт", "диск", "ветка", "коммит",
)
_DIRECT_TOOL_REMINDER_SOFT = (
    "probe", "bash", "shell", "grep", "find", "cat", "status", "log",
    "read", "check", "провер", "посмотр", "прочит",
)
_DIRECT_TOOL_REMINDER_TEXT = (
    "Direct DevCoveer tools should be used first: project_probe for Git/read/search/diff/remote/CI facts; "
    "host_probe for services/logs/listeners/resources/runtime; project_action for registered sync/test/build/deploy "
    "and source-handoff commit/push; host_action for registered host operations/restarts; list_models for live model "
    "health; list_tasks/read_task for task state. Start an agent only for work that needs reasoning beyond typed operations."
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

HELPER_POLICY_V1 = '''_DIRECT_TOOL_REMINDER_TEXT = (
    "Direct DevCoveer tools should be used first: project_probe for Git/read/search/diff/remote/CI facts; "
    "host_probe for services/logs/listeners/resources/runtime; project_action for registered sync/test/build/deploy "
    "and source-handoff commit/push; host_action for registered host operations/restarts; list_models for live model "
    "health; list_tasks/read_task for task state. Start an agent only for work that needs reasoning beyond typed operations."
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
HELPER_POLICY_V2 = '''_CHATGPT_OPERATING_REMINDER_V2 = True
_DIRECT_TOOL_REMINDER_TEXT = (
    "Direct DevCoveer tools should be used first: project_probe for Git/read/search/diff/remote/CI facts; "
    "host_probe for services/logs/listeners/resources/runtime; direct_ops_v2/project_action/host_action for deterministic "
    "mutations, tests, builds, Git, GitHub and registered host actions, including source-handoff commit/push through project_action. If an operation name or parameters are unknown, "
    "use direct_ops_v2 operation=describe. Use run_tool for known short tests/builds and job_start then job_status for long "
    "deterministic tests/builds, with python_env=auto to reuse the project environment. Use git_fetch for fetch without checkout "
    "and typed github_workflow_dispatch/github_run_cancel for applicable workflow operations; use git_switch_ff for exact existing-branch switching, "
    "apply_source_patch/stage_source_handoff for inert source transport, and artifact_fetch for arbitrary HTTPS project artifacts. list_models and list_tasks/read_task "
    "cover model/task state. For continuation/checkpoint work, inspect find_projects workspaceContext plus worktree_list, address the exact "
    "physical checkout, and pass expected_head when the checkpoint SHA is known; stage_source_handoff is patch isolation, not worktree selection. "
    "Typed operations are preferred fast paths, not a capability boundary: if a legitimate operation is absent, start_task remains available as the full-power reasoning escape hatch."
)
_BROWSER_TOOL_REMINDER_STRONG = (
    "playwright", "browser ui", "browser interaction", "dom", "console", "network",
    "screenshot", "navigate", "click", "fill", "upload", "download",
    "браузер", "скриншот", "консоль",
)
_BROWSER_TOOL_REMINDER_TEXT = (
    "For browser UI interaction, DOM observation, console/network inspection, screenshots, navigation, "
    "clicks, fills, uploads or downloads, use the available my-browser-bridge MCP directly instead of "
    "starting OpenCode solely to drive Playwright. Use project_action for a registered project Playwright test/build/webServer profile; "
    "use GitHub Actions for CI-specific execution, and start an agent only when "
    "the browser/project investigation actually requires reasoning. First use project_probe web_preflight at the package cwd; "
    "browser suites use job_start then job_status. CHECK_BASE/CHECK_OUTPUT are supported nonsecret env flags. "
    "Use instructions scope=home/project for exact AGENTS.md reads; prefer exact probes over broad filesystem scans, while retaining agent investigation when broader discovery is genuinely required."
)
_INCIDENT_REMINDER_STRONG = (
    "incident", "outage", "production regression", "production recovery", "failed recovery",
    "live failure", "postmortem", "root cause", "инцидент", "авария", "продакшен", "корневая причина",
)
_INCIDENT_REMINDER_TEXT = (
    "For real production/live incident work, do not jump straight to code: search/get related incidents, open or resume "
    "the incident, inspect registered service health and targeted logs with host_probe log_search, preserve evidence, "
    "record observations/hypotheses/mitigation, and verify live logs/health before incident_close. Ordinary local/unit-test "
    "failures do not need an incident."
)
_SOURCE_AUTHORSHIP_REMINDER_TEXT = (
    "Repository code and documentation should normally be authored by ChatGPT itself, not delegated to weaker "
    "OpenCode models. If ChatGPT cannot write the repository directly, author the exact code/docs patch here and "
    "use apply_source_patch or stage_source_handoff; then prefer project_action for registered tests and direct commit/push, plus host_action "
    "for registered rollout/restart operations. Use an agent only for genuinely non-deterministic investigation or "
    "when the user explicitly requests agent authoring."
)


def _direct_tool_reminder(prompt: str, access: str) -> str | None:
    text = prompt.casefold()
    hints: list[str] = []
    if any(marker in text for marker in _DIRECT_TOOL_REMINDER_STRONG):
        hints.append(_DIRECT_TOOL_REMINDER_TEXT)
    else:
        soft_hits = sum(1 for marker in _DIRECT_TOOL_REMINDER_SOFT if marker in text)
        if soft_hits >= 2 or (access == "read" and soft_hits >= 1):
            hints.append(_DIRECT_TOOL_REMINDER_TEXT)
    if any(marker in text for marker in _BROWSER_TOOL_REMINDER_STRONG):
        hints.append(_BROWSER_TOOL_REMINDER_TEXT)
    if any(marker in text for marker in _INCIDENT_REMINDER_STRONG):
        hints.append(_INCIDENT_REMINDER_TEXT)
    if access == "write":
        hints.append(_SOURCE_AUTHORSHIP_REMINDER_TEXT)
    return " ".join(hints) if hints else None
'''
HELPER_BLOCK = HELPER_BLOCK_V1.replace(HELPER_POLICY_V1, HELPER_POLICY_V2)
if HELPER_BLOCK == HELPER_BLOCK_V1:
    raise RuntimeError("failed to build v2 ChatGPT operating reminder")


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
                if isinstance(incident_preflight, dict):
                    incident = incident_preflight.get("incident")
                    if isinstance(incident, dict):
                        payload["incidentId"] = incident.get("incident_id")
                        payload["incidentReused"] = bool(incident_preflight.get("reused"))
                        payload["relatedIncidents"] = [
                            row.get("incident_id") for row in incident_preflight.get("related", [])
                            if isinstance(row, dict) and row.get("incident_id")
                        ]
                direct_tools_hint = _direct_tool_reminder(arguments["prompt"], access)
                if direct_tools_hint is not None:
                    payload["directToolsHint"] = direct_tools_hint
                    payload["content"] += "\\n" + direct_tools_hint
                return _tool_result(payload)
'''


START_PREFLIGHT_OLD = """            task_name = _task_title(project, arguments["prompt"])
            if route["backend"] == "opencode":
"""
START_PREFLIGHT_NEW = """            task_name = _task_title(project, arguments["prompt"])
            incident_preflight = None
            task_prompt = arguments["prompt"]
            try:
                incident_preflight = _preflight_incident(
                    project.canonical, project.path, task_name, arguments["prompt"]
                )
            except Exception as exc:
                log.error("incident preflight failed open (%s)", type(exc).__name__)
            if isinstance(incident_preflight, dict) and incident_preflight.get("agent_context"):
                task_prompt = str(incident_preflight["agent_context"]) + "\\n\\n" + task_prompt
            if route["backend"] == "opencode":
"""
OPENCODE_PROMPT_OLD = """                        prompt=arguments["prompt"], access=access,
"""
OPENCODE_PROMPT_NEW = """                        prompt=task_prompt, access=access,
"""
NATIVE_PROMPT_OLD = """                    "text": _first_prompt(project.canonical, arguments["prompt"], access),
"""
NATIVE_PROMPT_NEW = """                    "text": _first_prompt(project.canonical, task_prompt, access),
"""
NATIVE_INCIDENT_RESULT_OLD = """            if naming_warning is not None:
                payload["warning"] = naming_warning
            return _tool_result(payload)
"""
NATIVE_INCIDENT_RESULT_NEW = """            if isinstance(incident_preflight, dict):
                incident = incident_preflight.get("incident")
                if isinstance(incident, dict):
                    payload["incidentId"] = incident.get("incident_id")
                    payload["incidentReused"] = bool(incident_preflight.get("reused"))
                    payload["relatedIncidents"] = [
                        row.get("incident_id") for row in incident_preflight.get("related", [])
                        if isinstance(row, dict) and row.get("incident_id")
                    ]
            if naming_warning is not None:
                payload["warning"] = naming_warning
            return _tool_result(payload)
"""

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
    if LEGACY_MARKER in source:
        source = replace_once(source, DESCRIPTION_V1, DESCRIPTION_NEW, "legacy start_task description")
        source = replace_once(source, HELPER_BLOCK_V1, HELPER_BLOCK, "legacy reminder helper")
        return source
    source = replace_once(source, DESCRIPTION_OLD, DESCRIPTION_NEW, "start_task description")
    source = replace_once(
        source, TOOL_RESULT_ANCHOR, HELPER_BLOCK + TOOL_RESULT_ANCHOR, "tool result helper anchor"
    )
    source = replace_once(source, START_RETURN_OLD, START_RETURN_NEW, "OpenCode start_task result")
    source = replace_once(source, START_PREFLIGHT_OLD, START_PREFLIGHT_NEW, "incident task preflight")
    source = replace_once(source, OPENCODE_PROMPT_OLD, OPENCODE_PROMPT_NEW, "OpenCode incident prompt context")
    source = replace_once(source, NATIVE_PROMPT_OLD, NATIVE_PROMPT_NEW, "native incident prompt context")
    source = replace_once(source, NATIVE_INCIDENT_RESULT_OLD, NATIVE_INCIDENT_RESULT_NEW, "native incident result")
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
    legacy_installed = not already_installed and LEGACY_MARKER in source
    expected_hash = LEGACY_INSTALLED_BRIDGE_SHA256 if legacy_installed else EXPECTED_BRIDGE_SHA256
    if not already_installed and current_hash != expected_hash:
        raise SystemExit(
            f"installed bridge SHA-256 {current_hash} does not match expected {expected_hash}"
        )

    candidate = transform_bridge(source)
    compile_candidate(candidate)
    candidate_hash = sha256(candidate.encode("utf-8"))
    print(
        {
            "status": "ok",
            "applyRequested": args.apply,
            "alreadyInstalled": already_installed,
            "legacyInstalled": legacy_installed,
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
