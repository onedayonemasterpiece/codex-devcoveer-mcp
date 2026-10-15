#!/usr/bin/env python3
"""Fail-closed installer for the DevCoveer default-OpenCode execution policy.

Targets exactly the production bridge observed on 2026-09-12. Default mode is a
read-only dry run. --apply atomically replaces the bridge after exact input hash
verification, exact-one source transformations, and a py_compile check.

The installer never restarts services and never reads credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import py_compile
import tempfile
from pathlib import Path

BRIDGE = Path("/home/dev/.local/libexec/openai-codex-mcp/codex-chatgpt-bridge.py")
EXPECTED_BRIDGE_SHA256 = "954b153c97252371fec2574d87725d640da6f9e795f24a412b67316d3f57e60f"

OLD_ROUTING = """async def _routing_decision(
    history: NativeHistoryClient, *, project: ProjectEntry,
    provider: str | None, model: str | None, reasoning_effort: str | None,
) -> dict[str, Any]:
    \"\"\"Resolve one backend exactly; never fall back after a backend failure.\"\"\"
    if provider == "codex":
        selected_model, selected_effort = await _resolve_model_selection(
            history, model, reasoning_effort,
        )
        return {"backend": "codex", "provider": "codex", "model": selected_model,
                "reasoningEffort": selected_effort, "reason": "explicit_codex_provider"}
    if provider is not None:
        if reasoning_effort is not None:
            raise _invalid_params("reasoning_effort",
                "reasoning_effort is supported only by the Codex backend")
        selected = await OPENCODE_BACKEND.resolve_model(
            provider=provider, model=model, directory=project.path,
        )
        _assert_model_not_excluded(selected["selection"])
        return {"backend": "opencode", "provider": selected["provider"],
                "model": selected["nativeModelId"], "selection": selected["selection"],
                "reasoningEffort": None, "reason": "explicit_opencode_provider"}
    if model is None:
        selected_model, selected_effort = await _resolve_model_selection(
            history, None, reasoning_effort,
        )
        return {"backend": "codex", "provider": "codex", "model": selected_model,
                "reasoningEffort": selected_effort, "reason": "default_codex"}
    codex_models = await _codex_model_records(history)
    if any(model == item.get("id") for item in codex_models):
        selected_model, selected_effort = await _resolve_model_selection(
            history, model, reasoning_effort,
        )
        return {"backend": "codex", "provider": "codex", "model": selected_model,
                "reasoningEffort": selected_effort, "reason": "codex_model_match"}
    if reasoning_effort is not None:
        raise _invalid_params("reasoning_effort",
            "reasoning_effort is supported only by the Codex backend")
    selected = await OPENCODE_BACKEND.resolve_model(
        provider=None, model=model, directory=project.path,
    )
    _assert_model_not_excluded(selected["selection"])
    return {"backend": "opencode", "provider": selected["provider"],
            "model": selected["nativeModelId"], "selection": selected["selection"],
            "reasoningEffort": None, "reason": "opencode_model_match"}
"""

NEW_ROUTING = """async def _routing_decision(
    history: NativeHistoryClient, *, project: ProjectEntry,
    provider: str | None, model: str | None, reasoning_effort: str | None,
) -> dict[str, Any]:
    \"\"\"Resolve one backend exactly; never fall back after a backend failure.\"\"\"
    if provider == "codex":
        selected_model, selected_effort = await _resolve_model_selection(
            history, model, reasoning_effort,
        )
        return {"backend": "codex", "provider": "codex", "model": selected_model,
                "reasoningEffort": selected_effort, "reason": "explicit_codex_provider"}
    if reasoning_effort is not None:
        raise _invalid_params(
            "reasoning_effort",
            'native Codex requires explicit provider="codex"; OpenCode is the default backend',
        )
    if provider is not None:
        selected = await OPENCODE_BACKEND.resolve_model(
            provider=provider, model=model, directory=project.path,
        )
        _assert_model_not_excluded(selected["selection"])
        return {"backend": "opencode", "provider": selected["provider"],
                "model": selected["nativeModelId"], "selection": selected["selection"],
                "reasoningEffort": None, "reason": "explicit_opencode_provider"}
    if model is None:
        discovery = await _model_discovery_payload(history, None, False)
        routing = discovery.get("routing") if isinstance(discovery, dict) else {}
        suggestions = routing.get("suggestedSelections") if isinstance(routing, dict) else []
        adaptive_model = (
            suggestions[0]
            if isinstance(suggestions, list) and suggestions and isinstance(suggestions[0], str)
            else None
        )
        selected = await OPENCODE_BACKEND.resolve_model(
            provider=None if adaptive_model is not None else "opencode",
            model=adaptive_model, directory=project.path,
        )
        reason = "adaptive_opencode_default" if adaptive_model is not None else "default_opencode_fallback"
    else:
        selected = await OPENCODE_BACKEND.resolve_model(
            provider=None, model=model, directory=project.path,
        )
        reason = "opencode_model_match"
    _assert_model_not_excluded(selected["selection"])
    return {"backend": "opencode", "provider": selected["provider"],
            "model": selected["nativeModelId"], "selection": selected["selection"],
            "reasoningEffort": None, "reason": reason}
"""

OLD_START_TOOL = """    types.Tool(name="start_task", title="Start Codex task",
        description=("Start a named Codex task asynchronously in the uniquely resolved physical project. "
            "This returns threadId and turnId immediately while Codex continues in the background. Infer "
            "access=read for analysis, review, reading, planning, and status; infer "
            "access=write for implementation, fixes, creation, and updates. The user "
            "does not need to provide a path, sandbox, approval policy, or thread ID. "
            "When the user explicitly names a Codex model or reasoning level, pass the exact "
            "model and reasoning_effort fields; never treat those words as prompt-only hints. "
            "Use read_task later to check status and obtain the result."),
        input_schema=START_TASK_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
            idempotent_hint=False, open_world_hint=True)),"""

NEW_START_TOOL = """    types.Tool(name="start_task", title="Start DevCoveer task",
        description=("Start a task asynchronously in the uniquely resolved physical project. "
            "OpenCode is the default backend when provider is omitted. Native Codex has limited "
            "capacity and MUST be used only after an explicit owner request for native Codex. "
            "Select provider='codex' only for that request; no verbatim user-quote relay is required. "
            "The plugin/product name codex-devcoveer alone is never authorization. Naming a Codex "
            "model without explicit provider='codex' routes to OpenCode or fails closed. "
            "Never silently fall back from OpenCode to native Codex. Infer access=read for analysis, "
            "review, reading, planning, and status; infer access=write for implementation, fixes, "
            "creation, and updates. Before start_task for deterministic Git/host/runtime/model/task-state checks and other deterministic work, route to typed/direct tools: "
            "project_probe for Git/source/read/search/diff/remote/CI facts; host_probe for service/log/listener/resource/runtime facts; "
            "direct_ops_v2 as the primary cache-resistant surface for deterministic mutations, tests, builds, Git, GitHub, and registered host actions; project_action and host_action remain available typed aliases. "
            "If a current direct operation name or parameter schema is unknown or a long-lived client has stale top-level schemas, call direct_ops_v2 with operation=describe before choosing an agent or shell workaround. "
            "Use project_action for registered project test/build/deploy profiles. Known short deterministic tests/builds use run_tool; "
            "known long-running deterministic tests/builds use job_start then job_wait, with python_env=auto to reuse the existing project environment rather than create a venv. "
            "Use git_fetch for fetch without checkout; use typed github_workflow_dispatch/github_run_wait/github_run_cancel for applicable GitHub workflow operations. "
            "Use list_models for model state and list_tasks/read_task for task state. "
            "For continuation work that names or implies a checkpoint, branch, SHA or prior worktree, first inspect find_projects workspaceContext and project_probe worktree_list; exact repository-name resolution identifies only one physical checkout. "
            "Address the matching project before read/write/test/build and pass expected_head on guarded direct actions when a checkpoint SHA is known. stage_source_handoff isolates a ChatGPT-authored patch; it does not select an existing worktree and should not be created for work that direct operations can do in the selected checkout. Managed handoffs are temporary and staging performs bounded safe cleanup, so do not add manual cleanup ceremony to every task. "
            "When the project has .devcoveer/requirements.json, project_probe and direct_ops_v2 describe expose requirementsContext and agent tasks receive the same high-level contract automatically. Preserve those requirements across implementation changes; if the current task conflicts with them, surface the conflict instead of silently weakening or reinterpreting the contract. Change the contract only through requirements_update after explicit user requirement-change intent. "
            "For browser UI interaction, DOM observation, console/network inspection, screenshots, navigation or clicks, "
            "use my-browser-bridge directly when it is available to ChatGPT instead of starting OpenCode solely to drive Playwright. "
            "Before web work, use project_probe instructions with scope=home/project for exact AGENTS.md reads and web_preflight with the package cwd. "
            "Browser suites and unknown-duration checks use job_start then job_wait; pass nonsecret CHECK_BASE/CHECK_OUTPUT through env, "
            "with path relative to project root even when cwd is set. Never scan HOME or borrow browser binaries from other checkouts. "
            "In normal ChatGPT-led mode, do not delegate repository code or documentation authoring to OpenCode. "
            "ChatGPT should author the exact code/docs change itself, use direct project_action writes when available, and use stage_source_handoff "
            "when direct repository writing is unavailable; use the supplied patch for verification/commit/push rather than rewrite source/docs. "
            "Prefer one semantic typed operation over helper scripts or repeated low-level probes; use text_edit for exact replacements. Batch independent read-only checks into one project_probe/host_probe call. For long deterministic work use durable job_start/job_wait. After a denied call, timeout, lost response, or safety interruption, call recovery_snapshot before another mutation; safety_trace is advisory-only and may explain recent orchestration patterns. Do not re-attempt the same side effect through an equivalent alternate mechanism. "
            "Use start_task only when the work requires reasoning beyond typed operations "
            "or an unforeseen operation absent from the typed surface. Use read_task later to check status and obtain the result."),
        input_schema=START_TASK_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
            idempotent_hint=False, open_world_hint=True)),"""

OLD_HANDOFF_TOOL = """    types.Tool(name="stage_source_handoff", title="Stage source handoff",
        description=(
            "Materialize a Git patch in an isolated detached worktree. The patch must be "
            "equivalent to `git diff --cached --binary <base_sha>`. The project must be an "
            "existing DevCoveer project identifier. The patch is consumed through stdin and "
            "is never persisted to disk. Returns a handoff:<id> project identifier for use "
            "with start_task."
        ),
        input_schema=STAGE_SOURCE_HANDOFF_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False)),"""

NEW_HANDOFF_TOOL = """    types.Tool(name="stage_source_handoff", title="Stage source handoff",
        description=(
            "Use this direct transport when ChatGPT has uncommitted source and the ChatGPT-side "
            "GitHub connector cannot reliably write it. Materialize an exact-base Git patch in an "
            "isolated worktree; the patch is consumed through stdin and is never persisted. Returns "
            "a handoff:<id> project for start_task, which defaults to OpenCode. The patch is authored "
            "by ChatGPT; the handoff agent should verify/test/commit/push/deploy it, not independently "
            "rewrite repository code or documentation. If verification finds a source defect, return "
            "evidence to ChatGPT for a revised handoff. Managed handoffs are temporary: identical input reuses "
            "the same workspace and staging performs bounded cleanup of stale handoffs only when their state is already "
            "durable. Use source_handoff_list/source_handoff_gc when lifecycle inspection is actually useful; do not add "
            "cleanup bookkeeping to every task. cleanup_source_handoff remains available for explicit one-off teardown when "
            "the exact handoff is known. Do not create an archive, persistent patch storage, or a separate venv."
        ),
        input_schema=STAGE_SOURCE_HANDOFF_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False)),"""

def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one source match, found {count}")
    return source.replace(old, new, 1)

def transform_bridge(source: str) -> str:
    source = _replace_once(source, OLD_ROUTING, NEW_ROUTING, "routing")
    source = _replace_once(source, OLD_START_TOOL, NEW_START_TOOL, "start_task tool")
    source = _replace_once(source, OLD_HANDOFF_TOOL, NEW_HANDOFF_TOOL, "source handoff tool")
    return source

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _compile_candidate(source: str) -> None:
    fd, candidate_path = tempfile.mkstemp(prefix="devcoveer-default-opencode-", suffix=".py")
    os.close(fd)
    pyc_path = candidate_path + ".pyc"
    try:
        Path(candidate_path).write_text(source, encoding="utf-8")
        py_compile.compile(candidate_path, cfile=pyc_path, doraise=True)
    finally:
        for path in (candidate_path, pyc_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

def _atomic_write(path: Path, source: str) -> None:
    mode = path.stat().st_mode & 0o777
    fd, temp_name = tempfile.mkstemp(prefix=".default-opencode-", dir=path.parent)
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

    if not BRIDGE.is_file() or BRIDGE.is_symlink():
        raise SystemExit(f"unsafe or missing bridge: {BRIDGE}")
    current = BRIDGE.read_text(encoding="utf-8")
    old_sha = sha256_text(current)
    if old_sha != EXPECTED_BRIDGE_SHA256:
        raise SystemExit(
            f"bridge SHA mismatch: expected {EXPECTED_BRIDGE_SHA256}, got {old_sha}"
        )
    candidate = transform_bridge(current)
    _compile_candidate(candidate)
    new_sha = sha256_text(candidate)

    if args.apply:
        _atomic_write(BRIDGE, candidate)
        readback = sha256_text(BRIDGE.read_text(encoding="utf-8"))
        if readback != new_sha:
            raise SystemExit(f"bridge readback mismatch: expected {new_sha}, got {readback}")

    print(json.dumps({
        "status": "ok",
        "applied": bool(args.apply),
        "oldSha256": old_sha,
        "newSha256": new_sha,
        "bridge": str(BRIDGE),
        "restartRequired": bool(args.apply),
    }, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
