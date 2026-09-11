async def _model_discovery_payload(
    history: NativeHistoryClient, provider: str | None, verified_only: bool
) -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    warnings: list[str] = []
    provider_status: dict[str, dict[str, Any]] = {}
    verification: dict[str, Any] | None = None
    try:
        verification = _load_model_verification()
    except Exception:
        warnings.append("Inference-verification receipt is unavailable; live catalogs remain visible.")

    # Observability only: never lock models globally. DevCoveer serializes writers
    # only where the existing per-project policy requires it.
    active_usage: dict[str, dict[str, Any]] = {}
    try:
        for task in TASK_REGISTRY.list(limit=5000):
            if task.get("backend") != "opencode" or task.get("status") not in {"running", "cancelling"}:
                continue
            selection = task.get("selection")
            if not isinstance(selection, str):
                p = task.get("provider")
                m = task.get("model")
                if isinstance(p, str) and isinstance(m, str):
                    selection = f"{p}/{m}"
            if not isinstance(selection, str):
                continue
            usage = active_usage.setdefault(selection, {"activeTasks": 0, "sessionIds": set()})
            usage["activeTasks"] += 1
            session_id = task.get("backendSessionId")
            if isinstance(session_id, str):
                usage["sessionIds"].add(session_id)
    except Exception:
        warnings.append("Active model usage telemetry is temporarily unavailable.")

    if provider in (None, "codex"):
        try:
            codex_models = await _codex_model_records(history)
            models.extend(codex_models)
            provider_status["codex"] = {"status": "available", "count": len(codex_models)}
        except Exception:
            provider_status["codex"] = {"status": "degraded", "error": "catalog_unavailable"}
            warnings.append("The live Codex model catalog is unavailable.")

    if provider != "codex":
        try:
            catalog = await OPENCODE_BACKEND.catalog()
            connected = catalog["connected"]
            provider_health = catalog.get("providerHealth", {}) if isinstance(catalog, dict) else {}
            requested_providers = connected if provider is None else [provider]
            for provider_id in requested_providers:
                provider_models = [item for item in catalog["models"] if item["provider"] == provider_id]
                if not provider_models:
                    provider_status[provider_id] = {
                        "status": "unavailable", "connected": provider_id in connected, "count": 0,
                    }
                    continue
                receipt_state = (
                    verification.get("providers", {}).get(provider_id, {})
                    if verification is not None else {}
                )
                inferred = set(receipt_state.get("inferenceVerified") or [])
                live_snapshot = set(receipt_state.get("liveCatalogVerified") or [])
                accepted = inferred | live_snapshot
                added = 0
                for item in provider_models:
                    selection = item["selection"]
                    exclusion_reason = (
                        receipt_state.get("excluded", {}).get(selection)
                        if isinstance(receipt_state.get("excluded"), dict) else None
                    )
                    if isinstance(exclusion_reason, str):
                        warnings.append(f"Excluded unavailable model {selection}: {exclusion_reason}")
                        continue
                    verified = selection in inferred
                    snapshot = selection in live_snapshot
                    if verified_only and accepted and selection not in accepted:
                        continue
                    runtime_health = item.get("runtimeHealth") if isinstance(item.get("runtimeHealth"), dict) else {}
                    current_runtime = runtime_health.get("currentRuntimeStatus")
                    historical = (
                        "inference_verified" if verified else
                        "provider_live_verified" if snapshot else
                        "connected_catalog_active"
                    )
                    # Historical receipts never overwrite fresh runtime state. In particular,
                    # probe_due means the cooldown expired but recovery has not been proven yet.
                    if current_runtime in {"eol", "temporarily_unavailable", "probe_due", "degraded"}:
                        availability = current_runtime
                    elif current_runtime == "available_recent_success":
                        availability = "inference_verified_recent"
                    else:
                        availability = historical
                    usage = active_usage.get(selection, {"activeTasks": 0, "sessionIds": set()})
                    record = dict(item)
                    record.update({
                        "availability": availability,
                        "historicalVerification": historical,
                        "verifiedAt": verification.get("verifiedAt") if (verified or snapshot) and verification else None,
                        "default": selection == DEFAULT_OPENCODE_MODEL,
                        "taskRoutingReady": availability != "eol",
                        "activeTasks": int(usage.get("activeTasks", 0)),
                        "activeSessions": len(usage.get("sessionIds", set())),
                        "concurrencyPolicy": "parallel_by_default_project_writer_serialized",
                    })
                    models.append(record)
                    added += 1
                phealth = provider_health.get(provider_id, {}) if isinstance(provider_health, dict) else {}
                provider_status[provider_id] = {
                    "status": "available", "connected": True, "count": added,
                    "runtimeHealth": phealth,
                }
            if provider is not None and provider not in connected:
                provider_status[provider] = {"status": "unavailable", "connected": False, "count": 0}
        except Exception:
            target = provider or "opencode"
            provider_status[target] = {
                "status": "degraded", "connected": False, "error": "catalog_unavailable",
            }
            warnings.append("The localhost OpenCode model catalog is unavailable.")

    def routing_key(item: dict[str, Any]) -> tuple[Any, ...]:
        if item.get("backend") != "opencode":
            return (1, 99, 1.0, 10**12, 10**9, str(item.get("id", "")))
        health = item.get("runtimeHealth") if isinstance(item.get("runtimeHealth"), dict) else {}
        windows = health.get("windows") if isinstance(health.get("windows"), dict) else {}
        one_hour = windows.get("1h") if isinstance(windows.get("1h"), dict) else {}
        success_rate = one_hour.get("successRate")
        p95 = one_hour.get("p95LatencyMs")
        resource_priority = health.get("resourcePriority")
        recommended = item.get("routingRecommended") is not False and item.get("availability") != "eol"
        # Current health is the hard gate. Resource class priority is applied only
        # after an endpoint is actually eligible, so exhausted Zen never outranks
        # a healthy NVIDIA endpoint merely because Zen is normally preferred.
        return (
            0 if recommended else 1,
            int(resource_priority) if isinstance(resource_priority, int) else 90,
            -(float(success_rate) if isinstance(success_rate, (int, float)) else 0.5),
            int(p95) if isinstance(p95, int) else 10**9,
            int(item.get("activeTasks") or 0),
            str(item.get("id", "")),
        )

    open_code_candidates = [item for item in models if item.get("backend") == "opencode"]
    suggestable = [
        item for item in open_code_candidates
        if item.get("routingRecommended") is not False and item.get("availability") != "eol"
    ]
    suggested = [item["id"] for item in sorted(suggestable, key=routing_key)[:8]]
    overall = "ok" if provider_status and all(
        item.get("status") == "available" for item in provider_status.values()
    ) else "degraded"
    return {
        "status": overall,
        "providerFilter": provider,
        "verifiedOnly": verified_only,
        "defaultOpenCodeModel": DEFAULT_OPENCODE_MODEL,
        "verifiedAt": verification.get("verifiedAt") if verification else None,
        "probesPerformed": False,
        "providers": provider_status,
        "count": len(models),
        "models": models,
        "routing": {
            "policy": "zen_then_nvidia_then_metered",
            "automaticFallback": False,
            "globalModelLock": False,
            "suggestedSelections": suggested,
        },
        "warnings": warnings,
        "content": f"Found {len(models)} models. OpenCode default: {DEFAULT_OPENCODE_MODEL}.",
    }
