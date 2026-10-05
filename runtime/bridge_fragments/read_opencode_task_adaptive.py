_GROQ_KEY_ORDER = ("groq", "groq2", "groq3")
_GROQ_CAPACITY_CATEGORIES = {"rate_limited", "quota_exhausted"}


async def _maybe_auto_groq_fallback(
    record: dict[str, Any], inspected: dict[str, Any], *, status: str,
) -> dict[str, Any] | None:
    if record.get("status") == "cancelling":
        return None
    provider = record.get("provider")
    category = inspected.get("providerErrorCategory")
    session_status = inspected.get("sessionStatus")
    if (
        provider not in _GROQ_KEY_ORDER
        or category not in _GROQ_CAPACITY_CATEGORIES
        or not (status == "failed" or session_status == "retry")
    ):
        return None
    index = _GROQ_KEY_ORDER.index(provider)
    if index + 1 >= len(_GROQ_KEY_ORDER):
        return None
    model = record.get("model")
    session_id = record.get("backendSessionId")
    if not isinstance(model, str) or not isinstance(session_id, str):
        return None
    next_provider = _GROQ_KEY_ORDER[index + 1]
    selected = await OPENCODE_BACKEND.resolve_model(
        provider=next_provider, model=model, directory=str(record["cwd"]),
    )
    if session_status == "retry":
        await OPENCODE_BACKEND.request(
            "POST", f"/session/{session_id}/abort",
            directory=str(record["cwd"]), timeout=15,
        )
    await OPENCODE_BACKEND.request(
        "POST", f"/session/{session_id}/prompt_async",
        directory=str(record["cwd"]), timeout=15,
        payload={
            "agent": "build",
            "model": {"providerID": selected["provider"], "modelID": selected["nativeModelId"]},
            "parts": [{"type": "text", "text": (
                "Continue the most recent unfinished user request using the existing session history. "
                "The previous Groq key hit a capacity limit. Do not ask the user to restate the task. "
                "Do not repeat completed side effects; finish only the remaining work."
            )}],
        },
    )
    updated = TASK_REGISTRY.update(
        record["taskId"], status="running", provider=selected["provider"],
        model=selected["nativeModelId"], selection=selected["selection"],
        errorCategory=None, routingReason="automatic_groq_key_fallback",
    )
    return {
        "status": "running", "task": _registry_public(updated),
        "automaticFallback": True, "fallbackFrom": provider,
        "fallbackTo": next_provider, "suggestedAction": "wait",
        "content": "Groq capacity limit: continued on the next key.",
    }


async def _read_opencode_task(record: dict[str, Any], *, detail: str = "summary") -> dict[str, Any]:
    session_id = record.get("backendSessionId")
    if not isinstance(session_id, str):
        return {"status": "failed", "task": _registry_public(record),
                "content": "OpenCode task has no valid session ID."}
    try:
        inspected = await OPENCODE_BACKEND.inspect(
            session_id=session_id, directory=str(record["cwd"]),
            include_messages=(detail != "summary"),
            provider_hint=record.get("provider") if isinstance(record.get("provider"), str) else None,
            model_hint=record.get("model") if isinstance(record.get("model"), str) else None,
            selection_hint=record.get("selection") if isinstance(record.get("selection"), str) else None,
        )
        if detail == "summary" and inspected.get("status") == "running" and record.get("status") != "cancelling":
            return {"status":"running", "task":_registry_public(record),
                "executionStatus":"running", "inspectionStatus":"metadata_only",
                "providerState":inspected.get("providerState"),
                "suggestedPollAfterSeconds":5,
                "content":"Task is running. No message history was fetched; use detail=full only for diagnosis."}
        if detail == "summary":
            inspected = await OPENCODE_BACKEND.inspect(
                session_id=session_id, directory=str(record["cwd"]), include_messages=True,
                provider_hint=record.get("provider"), model_hint=record.get("model"),
                selection_hint=record.get("selection"))
    except OpenCodeError as exc:
        # An inability to inspect the localhost OpenCode runtime is not proof that
        # the underlying task itself failed. Preserve the last durable task state
        # for transport/upstream/readback failures and let a later read reconcile it.
        transient_inspection_errors = {
            "unavailable", "rate_limit", "upstream", "invalid_response", "oversized_response",
        }
        if exc.category in transient_inspection_errors:
            durable_status = str(record.get("status") or "running")
            current_status = "running" if durable_status in {"running", "cancelling"} else durable_status
            return {
                "status": current_status,
                "durableTaskStatus": durable_status,
                "task": _registry_public(record),
                "inspectionStatus": "degraded",
                "inspectionErrorCategory": exc.category,
                "suggestedAction": "retry_read_before_cancel",
                "content": (
                    "OpenCode session inspection is temporarily unavailable. "
                    "The durable task state was not changed; retry read_task before cancelling or replacing it."
                ),
            }
        TASK_REGISTRY.update(record["taskId"], status="failed", errorCategory=exc.category,
                             finishedAt=int(time.time() * 1000))
        return {"status": "failed", "task": _registry_public(TASK_REGISTRY.get(record["taskId"]) or record),
                "errorCategory": exc.category, "content": str(exc)}
    status = inspected["status"]
    inspected_error = inspected.get("error")
    if (
        status == "failed"
        and isinstance(inspected_error, dict)
        and inspected_error.get("name") == "MessageAbortedError"
    ):
        status = "interrupted"
    if status == "idle" and inspected.get("finalResponse") is None:
        if record.get("status") == "cancelling":
            status = "interrupted"
        else:
            # Age of a session is not evidence of a provider error.
            status = "idle"

    provider_state = inspected.get("providerState")
    provider_state_type = provider_state.get("type") if isinstance(provider_state, dict) else None
    if (
        record.get("status") == "cancelling"
        and status == "running"
        and (inspected.get("sessionStatus") == "idle" or provider_state_type == "idle")
    ):
        status = "interrupted"

    groq_fallback = await _maybe_auto_groq_fallback(record, inspected, status=status)
    if groq_fallback is not None:
        return groq_fallback
    durable_status = (
        "cancelling"
        if record.get("status") == "cancelling" and status == "running"
        else status
    )
    changes: dict[str, Any] = {"status": durable_status}
    if status in {"completed", "failed", "interrupted"}:
        changes["finishedAt"] = record.get("finishedAt") or inspected.get("updatedAt") or int(time.time() * 1000)
    if status == "failed":
        changes["errorCategory"] = inspected.get("providerErrorCategory") or "model_error"

    # Immutable terminal evidence is owned by the session-evidence wrapper.
    # Keep this base reader focused on provider/session status and raw internal history.
    updated = TASK_REGISTRY.update(record["taskId"], **changes)

    payload = {
        "status": status,
        "task": _registry_public(updated),
        "latestTurn": {
            "messageId": inspected.get("messageId"),
            "status": status,
            "finalResponse": inspected.get("finalResponse"),
            "error": inspected.get("error"),
        },
        "content": (
            "OpenCode task is still running. Check it again with read_task."
            if status == "running" else
            inspected.get("finalResponse") or
            ("OpenCode session is idle with no assistant result." if status == "idle" else "OpenCode task failed.")
        ),
    }

    # Live, bounded operational evidence: enough to decide wait/switch/cancel
    # without exposing the raw transcript and without declaring terminal acceptance.
    for key in (
        "sessionStatus", "providerState", "providerRuntimeStatus", "providerErrorCategory",
        "retryable", "retryAfter", "resetAt", "next", "attempt", "action",
        "observedAt", "lastMessageAt", "lastToolCallAt", "lastProgressAt",
        "messageCount", "toolCallCount", "observedTokenTotal", "stallForMs",
        "stallThresholdMs", "stallSuspected", "suggestedAction", "runtimeHealth",
    ):
        if key in inspected and inspected.get(key) is not None:
            payload[key] = inspected.get(key)

    if status in {"completed", "failed", "interrupted"}:
        payload["executionStatus"] = status

    if detail == "full" or status != "running":
        # Internal contract for the session-evidence wrapper. The wrapper owns
        # redaction, bounded live snapshots and immutable terminal evidence.
        payload["history"] = inspected.get("messages", [])

    if status == "idle":
        payload["executionStatus"] = "awaiting_final"
        payload["suggestedAction"] = "continue_same_task_after_inspection"
    if (
        status in {"completed", "failed", "interrupted"}
        and isinstance(updated.get("consultation"), dict)
        and updated["consultation"].get("kind") == "model_consultation"
    ):
        payload["advisory"] = True
        try:
            payload["consultationArtifact"] = _persist_consultation(
                updated, status, inspected.get("finalResponse"),
            )
        except Exception as exc:
            log.exception("consultation Markdown persistence failed (%s)", type(exc).__name__)
            payload["consultationArtifact"] = {
                "status": "failed", "errorCategory": "artifact_persistence",
            }
    return payload
