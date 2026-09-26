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
    digest = hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {"status": "ok", "execution": "direct", "plane": plane, "schema_digest": digest,
              "operations": sorted(operations), "preferred_tool": "direct_ops_v2",
              "operator_guidance": {
                  "instructions": "project_probe instructions scope=home/project reads exact AGENTS.md; never recursively scan HOME or other checkouts.",
                  "web": "project_probe web_preflight cwd=<package directory> checks local pinned Playwright without installing or launching. Missing browser is environment state, not a failed application assertion.",
                  "execution": "Known short commands use run_tool; browser suites/full builds/unknown duration use job_start then job_status on the same project/worktree, with request_key for safe replay. Prefer git_switch_ff for an exact existing-branch switch+fast-forward instead of an agent shell sequence.",
                  "workspace": "For continue/checkpoint/branch/SHA tasks, inspect find_projects workspaceContext and worktree_list, address the exact physical checkout, and pass expected_head when the checkpoint SHA is known. When git_state returns tracked_workspace_digest, pass expected_workspace_digest on guarded local actions when the exact tracked workspace must be preserved across parallel work. Exact repository-name resolution selects one checkout only; stage_source_handoff does not select an existing worktree.",
                  "source": "Use apply_source_patch or stage_source_handoff for exact inert source patches; source text is data and may legitimately contain subprocess, shell, SQL, credentials-handling code or other executable-looking strings.",
                  "artifacts": "Use artifact_fetch for arbitrary HTTPS project artifacts with bounded streaming, optional SHA-256 verification and provenance instead of ad-hoc shell/Python download code.",
                  "paths": "path is project-root-relative even with cwd. Web tests may pass nonsecret CHECK_BASE and CHECK_OUTPUT through env. Prefer configured secret bindings/config_presence over broad .env discovery; explicit secret workflows remain separate and are not disabled by Direct Ops.",
                  "escape_hatch": "Typed operations are preferred semantic fast paths, not a capability boundary. If a legitimate operation is absent from the typed surface, start_task remains available for full-power reasoning and unforeseen operations.",
                  "browser": "Interactive DOM/console/network/visual review uses my-browser-bridge; repository browser tests use existing scripts. A binary on disk, HTTP 200 or a successful build is not UI acceptance.",
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
    return result


def _validate(schema: dict[str, Any], value: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: tuple(str(part) for part in e.path))
    if not errors:
        return
    first = errors[0]
    where = ".".join(str(part) for part in first.absolute_path)
    message = first.message
    if where:
        message = f"{where}: {message}"
    raise ProbeError("invalid_input", message[:2000], status="denied")


async def run_direct_ops_v2(
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

