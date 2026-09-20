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


def _validate(schema: dict[str, Any], value: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
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
        return await run_project_probe(project_root, project_id, [check])

    if plane == "host_probe":
        check = {"id": "direct_ops_v2", "kind": operation, **body}
        request = {"project": project_id, "checks": [check]}
        _validate(HOST_PROBE_INPUT_SCHEMA, request)
        return await run_host_probe(project_root, project_id, [check])

    if plane == "project_action":
        action = {"kind": operation, **body}
        request = {"project": project_id, "action": action}
        _validate(PROJECT_ACTION_INPUT_SCHEMA, request)
        return await run_project_action(project_root, project_id, action)

    if plane == "host_action":
        action = {"kind": operation, **body}
        request = {"project": project_id, "action": action}
        _validate(HOST_ACTION_INPUT_SCHEMA, request)
        return await run_host_action(project_root, project_id, action)

    raise ProbeError("invalid_input", f"unknown direct ops plane {plane!r}", status="denied")
