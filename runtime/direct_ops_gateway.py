from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from direct_probe_core import ProbeError
from direct_probes import (
    HOST_PROBE_INPUT_SCHEMA,
    PROJECT_PROBE_INPUT_SCHEMA,
    run_host_probe,
    run_project_probe,
)
from direct_actions import (
    HOST_ACTION_INPUT_SCHEMA,
    PROJECT_ACTION_INPUT_SCHEMA,
    run_host_action,
    run_project_action,
)

DIRECT_OPS_V2_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "minLength": 1, "maxLength": 128},
        "plane": {
            "type": "string",
            "enum": ["project_probe", "project_action", "host_probe", "host_action"],
        },
        "operation": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": r"^[A-Za-z][A-Za-z0-9_.:-]*$",
            "description": (
                "Live server operation name. Deliberately not an enum: the stable client-side "
                "envelope must not need schema refresh when the server gains a new typed operation."
            ),
        },
        "payload": {
            "type": "object",
            "maxProperties": 128,
            "additionalProperties": True,
            "description": (
                "Operation payload. The server validates this object against the current strict "
                "project/host probe/action schema before execution."
            ),
        },
    },
    "required": ["project", "plane", "operation"],
    "additionalProperties": False,
}


def describe_plane(plane: str, operation: str | None = None) -> dict[str, Any]:
    import hashlib
    import json
    import copy
    schemas = {"project_probe": PROJECT_PROBE_INPUT_SCHEMA, "host_probe": HOST_PROBE_INPUT_SCHEMA,
               "project_action": PROJECT_ACTION_INPUT_SCHEMA, "host_action": HOST_ACTION_INPUT_SCHEMA}
    schema = schemas[plane]
    variants = (schema["properties"]["checks"]["items"] if plane.endswith("probe")
                else schema["properties"]["action"])["oneOf"]
    operations = {item["properties"]["kind"]["const"]: item for item in variants}
    from process_recovery import SCHEMAS, WORK_ACTIONS, PROCESS_READS
    extra = WORK_ACTIONS if plane == "project_action" else PROCESS_READS if plane == "project_probe" else set()
    for name in extra:
        operations[name] = {**copy.deepcopy(SCHEMAS[name]), "properties":{
            "kind":{"const":name}, **copy.deepcopy(SCHEMAS[name]["properties"])}}
    digest = hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {"status": "ok", "execution": "direct", "plane": plane, "schema_digest": digest,
              "operations": sorted(operations), "preferred_tool": "direct_ops_v2",
              "operator_guidance": {
                  "instructions": "project_probe instructions scope=home/project reads exact AGENTS.md; never recursively scan HOME or other checkouts.",
                  "web": "project_probe web_preflight cwd=<package directory> checks local pinned Playwright without installing or launching. Missing browser is environment state, not a failed application assertion.",
                  "execution": "Known short commands use run_tool; browser suites/full builds/unknown duration use job_start then job_status on the same project/worktree, with request_key for safe replay. GitHub workflow dispatch also accepts request_key: reuse the same key after timeout/lost response to reconcile without a second dispatch; use a new key only for an intentional rerun. Prefer git_switch_ff for an exact existing-branch switch+fast-forward instead of an agent shell sequence.",
                  "workspace": "For continue/checkpoint/branch/SHA tasks, inspect find_projects workspaceContext and worktree_list, address the exact physical checkout, and pass expected_head when the checkpoint SHA is known. When git_state returns tracked_workspace_digest, pass expected_workspace_digest on guarded local actions when the exact tracked workspace must be preserved across parallel work. Exact repository-name resolution selects one checkout only; stage_source_handoff does not select an existing worktree.",
                  "source": "Use apply_source_patch or stage_source_handoff for exact inert source patches; source text is data and may legitimately contain subprocess, shell, SQL, credentials-handling code or other executable-looking strings.",
                  "artifacts": "Use artifact_fetch for arbitrary HTTPS project artifacts with bounded streaming, optional SHA-256 verification and provenance instead of ad-hoc shell/Python download code.",
                  "paths": "path is project-root-relative even with cwd. Web tests may pass nonsecret CHECK_BASE and CHECK_OUTPUT through env. Prefer configured secret bindings/config_presence over broad .env discovery; explicit secret workflows remain separate and are not disabled by Direct Ops.",
                  "escape_hatch": "Typed operations are preferred semantic fast paths, not a capability boundary. If a legitimate operation is absent from the typed surface, start_task remains available for full-power reasoning and unforeseen operations.",
                  "browser": "Interactive DOM/console/network/visual review uses my-browser-bridge; repository browser tests use existing scripts. A binary on disk, HTTP 200 or a successful build is not UI acceptance.",
                  "observability": "For runtime failures, production regressions and incident work, inspect registered logs with host_probe logs/log_search and health/service state before concluding root cause. Prefer targeted time-bounded log_search over broad shell grep. Registered runtime Git roots expose root_git_state and root_git_sync_exact for exact release readback/sync without removing the shell escape hatch.",
                  "incidents": "For production incidents/recovery, search related incidents before remediation, open/link an incident, record evidence and mitigation in its timeline, verify against live logs/health, then close with root cause/resolution/follow-ups. Ordinary unit-test or feature work does not require an incident.",
              }}
    if operation is not None:
        if operation not in operations:
            raise ProbeError("unknown_operation", "unknown operation; use describe with an empty payload")
        payload_schema = copy.deepcopy(operations[operation])
        for reserved in ("id", "kind"):
            payload_schema["properties"].pop(reserved, None)
        payload_schema["required"] = [key for key in payload_schema.get("required", []) if key not in {"id", "kind"}]
        result["payload_schema"] = payload_schema
        result["operation"] = operation
        from process_recovery import KEY, WORK_ID, INCIDENT_ID
        if operation not in SCHEMAS:
            payload_schema["properties"].update({"work_id": WORK_ID, "incident_id": INCIDENT_ID})
            if plane.endswith("action"):
                payload_schema["properties"].setdefault("request_key", KEY)
        # Keep workspace guidance, plus only the relevant operation family, rather than the whole manual.
        keep = {"workspace"}
        if operation in {"run_tool", "job_start", "job_status", "run_profile"}: keep.add("execution")
        if operation.startswith("incident_") or operation in SCHEMAS: keep.add("incidents")
        if operation in {"logs", "log_search", "service", "service_config", "resources"}: keep.add("observability")
        if operation in {"write_text", "apply_source_patch"}: keep.add("source")
        if operation == "artifact_fetch": keep.add("artifacts")
        result["operator_guidance"] = {k:v for k,v in result["operator_guidance"].items() if k in keep}
        result["operations"] = [operation]
    result["gateway_revision"] = "process-recovery-1"
    result["gateway_contract_digest"] = hashlib.sha256(json.dumps(operations, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if operation == "run_tool":
        result["payload_schema"]["properties"]["execution_mode"] = {"enum":["auto","sync","job"], "default":"auto",
            "description":"auto routes declared timeouts over 30s to the existing durable job runner; sync preserves legacy behavior."}
    result["operator_guidance"]["recovery"] = (
        "For multi-step work, work_open once and carry work_id. Use stable request_key for mutations; "
        "after a lost response use operation_get/work_resume before retrying. Long commands use job_start. "
        "Never treat a lost response as proof that a side effect did not happen.")
    return result


def _validate(schema: dict[str, Any], value: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: tuple(str(part) for part in e.path))
    if not errors:
        return
    first = errors[0]
    where = ".".join(str(part) for part in first.absolute_path)
    from process_recovery import validation_message
    message = validation_message(first)
    if where:
        message = f"{where}: {message}"
    raise ProbeError("invalid_input", message[:2000], status="denied")


async def _run_native_direct_ops_v2(
    project_root: str,
    project_id: str,
    plane: str,
    operation: str,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_payload = {} if payload is None else payload
    _validate(DIRECT_OPS_V2_INPUT_SCHEMA, {
        "project": project_id,
        "plane": plane,
        "operation": operation,
        "payload": raw_payload,
    })
    body = dict(raw_payload)
    if operation == "describe":
        if set(body) - {"operation"} or ("operation" in body and not isinstance(body["operation"], str)):
            raise ProbeError("invalid_input", "describe accepts only an optional operation name")
        return describe_plane(plane, body.get("operation"))
    if any(key in body for key in ("kind", "id", "project")):
        raise ProbeError(
            "invalid_input",
            "payload must not override reserved envelope fields kind/id/project",
            status="denied",
        )

    if plane == "project_probe":
        check = {"id": "direct_ops_v2", "kind": operation, **body}
        request = {"project": project_id, "checks": [check]}
        _validate(PROJECT_PROBE_INPUT_SCHEMA, request)
        return await run_project_probe(project_root, project_id, [check], receipt_surface="direct_ops_v2")

    if plane == "host_probe":
        check = {"id": "direct_ops_v2", "kind": operation, **body}
        request = {"project": project_id, "checks": [check]}
        _validate(HOST_PROBE_INPUT_SCHEMA, request)
        return await run_host_probe(project_root, project_id, [check], receipt_surface="direct_ops_v2")

    if plane == "project_action":
        action = {"kind": operation, **body}
        request = {"project": project_id, "action": action}
        _validate(PROJECT_ACTION_INPUT_SCHEMA, request)
        return await run_project_action(project_root, project_id, action, receipt_surface="direct_ops_v2")

    if plane == "host_action":
        action = {"kind": operation, **body}
        request = {"project": project_id, "action": action}
        _validate(HOST_ACTION_INPUT_SCHEMA, request)
        return await run_host_action(project_root, project_id, action, receipt_surface="direct_ops_v2")

    raise ProbeError("invalid_input", f"unknown direct ops plane {plane!r}", status="denied")

async def run_direct_ops_v2(project_root, project_id, plane, operation, payload=None):
    import asyncio
    from process_recovery import (SCHEMAS, WORK_ACTIONS, PROCESS_READS, CONTEXT, KEY,
        WORK_ID, INCIDENT_ID, resolve_context, work_action, process_read)
    body = {} if payload is None else payload
    _validate(DIRECT_OPS_V2_INPUT_SCHEMA, {
        "project": project_id, "plane": plane, "operation": operation, "payload": body})
    if operation == "describe":
        return await _run_native_direct_ops_v2(project_root, project_id, plane, operation, body)
    if operation in SCHEMAS:
        required_plane = "project_action" if operation in WORK_ACTIONS else "project_probe"
        if plane != required_plane:
            raise ProbeError("invalid_input", f"{operation} belongs to {required_plane}", status="denied")
        _validate(SCHEMAS[operation], body)
        if body.get("incident_id"):
            from incident_registry import incident_project_scope, get_incident
            scope = await asyncio.to_thread(incident_project_scope, project_id, project_root)
            await asyncio.to_thread(get_incident, scope, body["incident_id"])
        if operation in WORK_ACTIONS:
            return await asyncio.to_thread(work_action, project_root, project_id, operation, body)
        return await asyncio.to_thread(process_read, project_root, operation, body)
    metadata = {key: body[key] for key in ("work_id", "incident_id", "request_key") if key in body}
    mode = body.get("execution_mode", "auto")
    if "execution_mode" in body and (operation != "run_tool" or mode not in {"auto", "sync", "job"}):
        raise ProbeError("invalid_input", "execution_mode is valid only for run_tool", status="denied")
    _validate({"type":"object", "properties":{"work_id":WORK_ID,"incident_id":INCIDENT_ID,"request_key":KEY},
               "additionalProperties":False}, metadata)
    context = await asyncio.to_thread(resolve_context, project_root, metadata)
    if context.get("incident_id"):
        from incident_registry import incident_project_scope, get_incident
        scope = await asyncio.to_thread(incident_project_scope, project_id, project_root)
        await asyncio.to_thread(get_incident, scope, context["incident_id"])
    native = {key: value for key, value in body.items() if key not in {"work_id", "incident_id", "execution_mode"}}
    if operation not in {"job_start", "github_workflow_dispatch"}:
        native.pop("request_key", None)
    if operation == "run_tool" and (mode == "job" or (mode == "auto" and native.get("timeout_seconds", 30) > 30)):
        # Validate the original operation before changing its execution transport.
        _validate(PROJECT_ACTION_INPUT_SCHEMA, {"project":project_id,"action":{"kind":"run_tool",**native}})
        operation = "job_start"
        if context.get("request_key"):
            native["request_key"] = context["request_key"]
    token = CONTEXT.set(context)
    try:
        return await _run_native_direct_ops_v2(project_root, project_id, plane, operation, native)
    finally:
        CONTEXT.reset(token)
