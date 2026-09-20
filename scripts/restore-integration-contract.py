"""Canonical builder transform for live routing and terminal acceptance."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def transform(source, replace_once):
    def replace(old, new, label):
        nonlocal source
        source = replace_once(source, old, new, label)
    replace('from opencode_backend import (', 'from session_evidence import capture as _capture_evidence, read as _read_evidence, redact as _redact_evidence\nfrom opencode_routing import RoutingBackend, candidates as _routing_candidates\nfrom opencode_backend import (', 'integration imports')
    replace('OPENCODE_BACKEND = OpenCodeBackend()', 'OPENCODE_BACKEND = RoutingBackend()', 'health backend')
    replace('if initialized.get("codexHome") != "/home/dev/.codex":', 'if not isinstance(initialized.get("codexHome"), str) or Path(initialized["codexHome"]).resolve() != Path("/home/dev/.codex").resolve():', 'canonical auth home')
    start = source.index('async def _routing_decision(')
    end = source.index('\n\ndef _registry_task(', start)
    original = source[start:end]
    # Keep explicit provider behavior; remove the implicit native Codex model fallback.
    boundary = original.index('    if model is None:')
    replacement = original[:boundary] + '''    if reasoning_effort is not None:
        raise _invalid_params("reasoning_effort", 'Native Codex requires explicit provider="codex"')
    if model is None:
        catalog = await OPENCODE_BACKEND.catalog(directory=project.path)
        eligible = [m for m in _routing_candidates(catalog['models']) if not _known_model_exclusion(m['selection'])]
        if not eligible:
            raise OpenCodeError("unavailable", "No eligible provider in Zen -> Groq -> NVIDIA -> GigaChat")
        selected = eligible[0]
        reason = "adaptive_opencode_default"
    else:
        selected = await OPENCODE_BACKEND.resolve_model(provider=None, model=model, directory=project.path)
        reason = "opencode_model_match"
    _assert_model_not_excluded(selected["selection"])
    return {"backend": "opencode", "provider": selected["provider"],
            "model": selected["nativeModelId"], "selection": selected["selection"],
            "reasoningEffort": None, "reason": reason}
'''
    source = source[:start] + replacement + source[end:]
    schemas = '''
GET_TASK_EVIDENCE_INPUT_SCHEMA = {
    "type": "object", "properties": {
        "task": {"type": "string", "minLength": 1, "maxLength": 256},
        "project": {"type": "string", "minLength": 1, "maxLength": 128},
        "scope": {"enum": ["summary", "suspicious", "full_safe"]}},
    "required": ["task"], "additionalProperties": False}
RECORD_TASK_REVIEW_INPUT_SCHEMA = {
    "type": "object", "properties": {
        "task": {"type": "string", "minLength": 1, "maxLength": 256},
        "project": {"type": "string", "minLength": 1, "maxLength": 128},
        "evidence_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "verdict": {"enum": ["accepted", "rejected"]},
        "findings": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 4000}}},
    "required": ["task", "evidence_sha256", "verdict"], "additionalProperties": False}
'''
    replace('\nTOOLS = [\n', '\n' + schemas + '\nTOOLS = [\n', 'evidence schemas')
    anchor = '    types.Tool(name="council_run", title="Run multi-model council",\n'
    tool_defs = '''    types.Tool(name="get_task_evidence", title="Read OpenCode evidence",
        description="Read immutable redacted terminal session evidence. summary returns hashes; full_safe returns complete safe content.",
        input_schema=GET_TASK_EVIDENCE_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)),
    types.Tool(name="record_task_review", title="Review OpenCode evidence",
        description="External reviewer only: record accepted/rejected verdict for the exact current evidence digest. OpenCode must not call this tool.",
        input_schema=RECORD_TASK_REVIEW_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)),
'''
    replace(anchor, tool_defs+anchor, 'evidence tools')
    anchor = '    "council_run": Draft202012Validator(COUNCIL_RUN_INPUT_SCHEMA),\n'
    replace(anchor, '    "get_task_evidence": Draft202012Validator(GET_TASK_EVIDENCE_INPUT_SCHEMA),\n    "record_task_review": Draft202012Validator(RECORD_TASK_REVIEW_INPUT_SCHEMA),\n'+anchor, 'evidence validators')
    anchor = '        if name == "council_run":\n'
    replace(anchor, '''        if name in {"get_task_evidence", "record_task_review"}:
            result = await _evidence_tool(name, arguments, project)
            return _tool_result(result, error=result.get("status") != "ok")

'''+anchor, 'evidence dispatch')
    # The adaptive OpenCode readback is source-controlled as one coherent
    # fragment because routing fallback, cancellation lifecycle, live progress,
    # and evidence semantics must evolve together.  Keeping the fragment merely
    # testable but not materialized into the canonical bridge would leave the
    # installed runtime on the historical readback implementation.
    read_start = source.index('async def _read_opencode_task(')
    read_end = source.index('\n\n_COUNCIL_BACKGROUND: dict[str, asyncio.Task[Any]] = {}', read_start)
    read_fragment = (ROOT/'runtime/bridge_fragments/read_opencode_task_adaptive.py').read_text()
    source = source[:read_start] + read_fragment.rstrip() + source[read_end:]

    replace('\n_COUNCIL_BACKGROUND: dict[str, asyncio.Task[Any]] = {}', '\n'+(ROOT/'runtime/bridge_fragments/session_evidence.py').read_text()+'\n_COUNCIL_BACKGROUND: dict[str, asyncio.Task[Any]] = {}', 'evidence lifecycle')
    replace('"error": inspected.get("error"),\n        },', '"error": inspected.get("error"),\n            "providerErrorCategory": inspected.get("providerErrorCategory"),\n            "providerState": inspected.get("providerState"),\n        },', 'runtime diagnostics')
    replace('"start_task", "continue_task", "cancel_task"}', '"start_task", "continue_task", "cancel_task", "record_task_review"}', 'review lifecycle serialization')
    anchor = '                return _tool_result({\n                    "status": "running", "taskId": task_record["taskId"],'
    replace(anchor, '                _watch_opencode_evidence(task_record["taskId"])\n'+anchor, 'automatic capture start')
    replace('finishedAt=None, errorCategory=None)\n            return _tool_result({', 'finishedAt=None, errorCategory=None, sessionEvidence={}, acceptanceStatus="not_ready", executionStatus="running")\n            _watch_opencode_evidence(updated["taskId"])\n            return _tool_result({', 'capture continuation')
    replace('"routingReason", "title",', '"routingReason", "title", "routingTransitions", "attemptedSelections", "executionStatus", "acceptanceStatus",', 'public routing history')
    replace('    if status in {"completed", "failed", "interrupted"}:\n        changes["finishedAt"] = int(time.time() * 1000)', '    if status in {"completed", "failed", "interrupted"}:\n        changes["finishedAt"] = record.get("finishedAt") or int(time.time() * 1000)', 'stable terminal timestamp')
    replace('    server = build_server(history)\n    try:', '    server = build_server(history)\n    for record in TASK_REGISTRY.list(limit=5000):\n        if record.get("backend") == "opencode" and record.get("status") in {"running", "cancelling"}:\n            _watch_opencode_evidence(record["taskId"])\n    try:', 'recover monitors after restart')
    # Advertise the actual ordered policy and health in the model catalog.
    start = source.index('async def _model_discovery_payload(')
    end = source.index('\n\nasync def _resolve_model_selection(', start)
    fragment = (ROOT/'runtime/bridge_fragments/model_discovery_adaptive.py').read_text()
    fragment = fragment.replace('"automaticFallbackScope": "groq_key_pool"', '"automaticFallbackScope": "ordered_providers_for_automatic_tasks"')
    # Sorting and admission must be identical in discovery and actual execution.
    a = fragment.index('    open_code_candidates =')
    b = fragment.index('    overall =', a)
    fragment = fragment[:a] + '    suggested = [m["selection"] for m in _routing_candidates(models)][:8]\n' + fragment[b:]
    source = source[:start] + fragment.rstrip() + source[end:]
    return source
