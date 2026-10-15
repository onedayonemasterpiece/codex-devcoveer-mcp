"""Guarded continuation of an exact, locally verified external Codex Native thread.

This is not task creation, session import, or a broad native-thread discovery API.
The existing exact Codex UUID is resumed through the official app-server, then
receives one continuation turn only after effective policy and model verification.
"""
from __future__ import annotations

import anyio
from typing import Any, Awaitable, Callable


def _response(thread_id: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "threadId": thread_id,
        "threadUrl": f"codex://threads/{thread_id}",
        "taskReference": thread_id,
        "backend": "codex",
        "content": message,
        **extra,
    }


async def continue_external_native_thread(
    *,
    thread_id: str,
    arguments: dict[str, Any],
    history: Any,
    requested_project_cwd: str | None,
    authorized_project_cwds: set[str],
    read_native: Callable[..., dict[str, Any]],
    resolve_model: Callable[..., Awaitable[tuple[str | None, str | None]]],
    sandbox_matches: Callable[..., bool],
    model_matches: Callable[..., tuple[str, str | None] | None],
    sandbox_policy: Callable[..., dict[str, Any]],
    continuation_access: Callable[[dict[str, Any]], str | None],
    sandbox_by_access: dict[str, str],
    write_profile: str,
    prompt_prefix: str = "",
) -> dict[str, Any]:
    if arguments.get("provider") not in (None, "codex"):
        return _response(thread_id, "conflict",
                         "An external native Codex thread cannot switch to another provider.")
    if arguments.get("work_id") is not None:
        return _response(thread_id, "conflict",
                         "A native VS Code thread cannot be silently bound to an unrelated DevCoveer work ID.")
    try:
        # Never infer ownership from UUID alone. Require the verified local
        # rollout, not a name fragment or arbitrary caller-supplied path.
        evidence = await anyio.to_thread.run_sync(
            lambda: read_native(thread_id, detail="summary", include_tools=False)
        )
    except Exception as exc:
        if getattr(exc, "status", None) in ("not_found", "invalid_source", "ambiguous"):
            return _response(thread_id, "not_found",
                             "No uniquely verified native rollout exists for this exact thread.")
        return _response(thread_id, "failed", "Native thread verification failed.")

    source = evidence.get("task") if isinstance(evidence, dict) else None
    if not isinstance(source, dict) or source.get("source") != "vscode":
        return _response(thread_id, "conflict",
                         "Only locally verified VS Code native threads use this external continuation path.")
    cwd = source.get("cwd")
    if not isinstance(cwd, str) or (
        cwd != "/home/dev" and cwd not in authorized_project_cwds
    ):
        return _response(thread_id, "project_mismatch",
                         "Native session cwd is outside trusted DevCoveer project/home roots.")
    if requested_project_cwd is not None and cwd not in (
        requested_project_cwd, "/home/dev"
    ):
        return _response(thread_id, "project_mismatch",
                         "Requested project differs from the original native session cwd.")

    try:
        observed = await history.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
    except Exception:
        return _response(thread_id, "not_found",
                         "The Codex app-server cannot read the existing VS Code thread.")
    thread = observed.get("thread") if isinstance(observed, dict) else None
    if (not isinstance(thread, dict)
            or thread.get("id") != thread_id
            or thread.get("cwd") != cwd
            or thread.get("source") != "vscode"):
        return _response(thread_id, "conflict",
                         "Codex app-server metadata disagrees with the verified native rollout.")
    if thread.get("modelProvider") not in (None, "openai"):
        return _response(thread_id, "conflict",
                         "The existing native thread uses an unsupported model provider.")

    turns = thread.get("turns")
    latest = next((item for item in reversed(turns)
                   if isinstance(item, dict)), None) if isinstance(turns, list) else None
    if isinstance(latest, dict) and latest.get("status") == "inProgress":
        return _response(thread_id, "already_running",
                         "The VS Code turn is still running. No competing turn was launched.",
                         turnId=latest.get("id"))

    requested_access = continuation_access(arguments) or "read"
    if requested_access not in sandbox_by_access:
        return _response(thread_id, "invalid_input", "Unsupported continuation access.")
    original_model = thread.get("model")
    original_effort = thread.get("reasoningEffort")
    model = arguments.get("model") or original_model
    effort = arguments.get("reasoning_effort") or original_effort
    try:
        selected_model, selected_effort = await resolve_model(history, model, effort)
    except Exception:
        return _response(thread_id, "model_unavailable",
                         "The original or explicitly requested native model/effort is not available. No turn was started.")

    metadata = {
        "cwd": cwd,
        "access": requested_access,
        "sandbox": sandbox_by_access[requested_access],
        "permissions": write_profile if requested_access == "write" else None,
        "model": selected_model,
        "reasoningEffort": selected_effort,
    }

    # Force a verifiable resume rather than trusting state from an unrelated
    # VS Code app-server. Never force-restart another running bridge task.
    try:
        resumed = await history.ensure_thread_loaded(thread_id, metadata, force_resume=True)
    except Exception:
        return _response(thread_id, "resume_failed",
                         "Codex could not load the existing native thread; no new thread was created.")
    resumed_thread = resumed.get("thread") if isinstance(resumed, dict) else None
    if not isinstance(resumed_thread, dict) or resumed_thread.get("id") != thread_id:
        return _response(thread_id, "resume_failed",
                         "Codex did not confirm the exact original thread; no continuation was started.")
    if not sandbox_matches(
        resumed, cwd=cwd, sandbox=metadata["sandbox"],
        permissions=metadata["permissions"],
    ):
        return _response(thread_id, "policy_conflict",
                         "Codex did not apply the requested safe access policy; no turn was started.")
    effective_model = model_matches(
        resumed, requested_model=selected_model, requested_effort=selected_effort,
    )
    if effective_model is None:
        return _response(thread_id, "model_conflict",
                         "Codex did not preserve the verified model/effort; no turn was started.")
    selected_model, selected_effort = effective_model

    turn_params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt_prefix + arguments["prompt"]}],
        "cwd": cwd,
        "approvalPolicy": "never",
    }
    if metadata["permissions"] is None:
        turn_params["sandboxPolicy"] = sandbox_policy(metadata["sandbox"], cwd)
    if selected_model is not None:
        turn_params["model"] = selected_model
    if selected_effort is not None:
        turn_params["effort"] = selected_effort
    try:
        turn_started = await history.request("turn/start", turn_params)
    except Exception:
        # The RPC may have succeeded before a lost response. Never retry
        # automatically, start another task, or lose the original UUID.
        return _response(thread_id, "outcome_unknown",
                         "Continuation RPC outcome is uncertain. Read the same native thread before retrying.")

    turn = turn_started.get("turn") if isinstance(turn_started, dict) else None
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        return _response(thread_id, "outcome_unknown",
                         "Codex returned no verified continuation turn ID. Inspect the same thread before retrying.")
    return _response(
        thread_id, "running",
        "The existing VS Code Codex thread was continued. No new task or thread was created.",
        action="continued_existing_native_thread",
        turnId=turn_id,
        cwd=cwd,
        access=requested_access,
        model=selected_model,
        reasoningEffort=selected_effort,
        nextReadTool="read_native_thread",
    )
