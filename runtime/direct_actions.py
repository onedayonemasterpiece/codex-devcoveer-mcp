from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat as statmod
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from direct_probe_core import (
    DEFAULT_CONFIG_PATH,
    ProbeError,
    _append_direct_receipt,
    _git,
    _binding,
    _binding_for_root,
    _git_env,
    _load_config,
    _redact_text,
    _run_process,
    _safe_worktree_path,
    _tracked_workspace_digest,
    _validate_alias,
    _validate_branch,
)
from direct_probe_host_services import _host_section, _systemd_env
from direct_ops2 import (
    _mutation_path, capture_handoff, chmod_executable, delete_file, git_commit_push, git_create_branch,
    git_stage, mkdir, move_path, resolve_github_remote, runner_argv, service_action,
    write_registered_file, write_text,
)
from direct_ops2_host import daemon_reload, delete_registered_file, root_git_sync_exact
from direct_ops2_worktrees import capture_worktree_handoff, owned_service_action, reconcile_worktree
from direct_jobs import cancel_job, cleanup_job, job_status, start_job, list_jobs
from direct_github_actions import delete_remote_branch, pr_create, pr_merge, pr_update, git_fetch, workflow_dispatch, run_cancel
from incident_registry import active_incident_id, close_incident, incident_project_scope, note_incident, open_incident

HANDOFF_ID_RE = re.compile(r"^src_[0-9a-f]{24}$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SECRET_ENV_RE = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|AUTH|COOKIE)", re.I)
MAX_ACTION_OUTPUT = 256 * 1024
MAX_ACTION_TIMEOUT = 3600.0
ARG_RE = re.compile(r"^[^\x00\r\n]{0,500}$")
NPM_SCRIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,79}$")
DIRECT_OPS_COMPAT_PATH = "@direct_ops_v2"
MAX_SOURCE_PATCH_BYTES = 16 * 1024 * 1024
DEFAULT_ARTIFACT_MAX_BYTES = 1024 * 1024 * 1024
MAX_ARTIFACT_MAX_BYTES = 8 * 1024 * 1024 * 1024

_PROJECT_ACTION_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "kind": {"const": "sync_ff"},
                "remote": {"type": "string", "minLength": 1, "maxLength": 64,
                           "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                "branch": {"type": "string", "minLength": 1, "maxLength": 200,
                           "pattern": r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"},
                "expected_sha": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
            },
            "required": ["kind", "remote", "branch", "expected_sha"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "run_tool"},
                "tool": {"type": "string", "enum": [
                    "pytest", "bash_script", "python_script", "node_script", "npm_script",
                    "pnpm_script", "yarn_script", "gradle_task", "make_target", "python_module"
                ]},
                "path": {"type": "string", "minLength": 1, "maxLength": 500},
                "script": {"type": "string", "minLength": 1, "maxLength": 80,
                           "pattern": r"^[A-Za-z0-9][A-Za-z0-9:._-]*$"},
                "cwd": {"type": "string", "maxLength": 500},
                "args": {"type": "array", "maxItems": 64,
                         "items": {"type": "string", "maxLength": 500}},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
            },
            "required": ["kind", "tool"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "run_profile"},
                "profile": {"type": "string", "minLength": 1, "maxLength": 64,
                            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
            },
            "required": ["kind", "profile"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "handoff_commit_push"},
                "remote": {"type": "string", "minLength": 1, "maxLength": 64,
                           "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                "branch": {"type": "string", "minLength": 9, "maxLength": 200,
                           "pattern": r"^chatgpt/[A-Za-z0-9][A-Za-z0-9._/-]*$"},
                "commit_message": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "required": ["kind", "remote", "branch", "commit_message"],
            "additionalProperties": False,
        },
        {"type":"object","properties":{"kind":{"const":"write_text"},"path":{"type":"string","minLength":1,"maxLength":500},"content":{"type":"string","maxLength":2097152},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"},"must_not_exist":{"type":"boolean"}},"required":["kind","path","content"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"mkdir"},"path":{"type":"string","minLength":1,"maxLength":500},"parents":{"type":"boolean"}},"required":["kind","path"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"move_path"},"source":{"type":"string","minLength":1,"maxLength":500},"target":{"type":"string","minLength":1,"maxLength":500},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"}},"required":["kind","source","target"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"delete_file"},"path":{"type":"string","minLength":1,"maxLength":500},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"}},"required":["kind","path"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"chmod_executable"},"path":{"type":"string","minLength":1,"maxLength":500},"executable":{"type":"boolean"},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"}},"required":["kind","path","executable"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"git_create_branch"},"branch":{"type":"string","minLength":9,"maxLength":200,"pattern":r"^chatgpt/[A-Za-z0-9][A-Za-z0-9._/-]*$"},"start_ref":{"type":"string","minLength":1,"maxLength":200}},"required":["kind","branch"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"git_stage"},"paths":{"type":"array","minItems":1,"maxItems":128,"items":{"type":"string","minLength":1,"maxLength":500}}},"required":["kind","paths"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"git_commit_push"},"remote":{"type":"string","minLength":1,"maxLength":64,"pattern":r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},"commit_message":{"type":"string","minLength":1,"maxLength":200}},"required":["kind","commit_message"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"capture_handoff"},"base_sha":{"type":"string","pattern":r"^[0-9a-f]{40}$"},"include_untracked":{"type":"boolean"},"paths":{"type":"array","minItems":1,"maxItems":256,"items":{"type":"string","minLength":1,"maxLength":500}}},"required":["kind","base_sha"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"worktree_capture_handoff"},"worktree":{"type":"string","minLength":1,"maxLength":1000},"base_sha":{"type":"string","pattern":r"^[0-9a-f]{40}$"},"include_untracked":{"type":"boolean"},"paths":{"type":"array","minItems":1,"maxItems":256,"items":{"type":"string","minLength":1,"maxLength":500}}},"required":["kind","worktree","base_sha"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"worktree_reconcile"},"worktree":{"type":"string","minLength":1,"maxLength":1000},"handoff_id":{"type":"string","pattern":r"^src_[0-9a-f]{24}$"}},"required":["kind","worktree","handoff_id"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"owned_service_action"},"unit":{"type":"string","minLength":1,"maxLength":200,"pattern":r"^[A-Za-z0-9_.@:-]+\.service$"},"operation":{"type":"string","enum":["status","start","stop","restart","reload","enable","disable"]}},"required":["kind","unit","operation"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"github_pr_create"},"remote":{"type":"string","minLength":1,"maxLength":64},"head":{"type":"string","minLength":9,"maxLength":200,"pattern":r"^chatgpt/[A-Za-z0-9][A-Za-z0-9._/-]*$"},"base":{"type":"string","minLength":1,"maxLength":200},"expected_head_sha":{"type":"string","pattern":r"^[0-9a-f]{40}$"},"title":{"type":"string","minLength":1,"maxLength":256},"body":{"type":"string","maxLength":20000}},"required":["kind","head","expected_head_sha","title"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"github_pr_update"},"remote":{"type":"string","minLength":1,"maxLength":64},"pr":{"type":"integer","minimum":1},"expected_head_sha":{"type":"string","pattern":r"^[0-9a-f]{40}$"},"title":{"type":"string","minLength":1,"maxLength":256},"body":{"type":"string","maxLength":20000}},"required":["kind","pr","expected_head_sha"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"github_pr_merge"},"remote":{"type":"string","minLength":1,"maxLength":64},"pr":{"type":"integer","minimum":1},"expected_head_sha":{"type":"string","pattern":r"^[0-9a-f]{40}$"},"expected_base":{"type":"string","minLength":1,"maxLength":200},"method":{"type":"string","enum":["merge","squash","rebase"]}},"required":["kind","pr","expected_head_sha"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"github_branch_delete"},"remote":{"type":"string","minLength":1,"maxLength":64},"branch":{"type":"string","minLength":9,"maxLength":200,"pattern":r"^chatgpt/[A-Za-z0-9][A-Za-z0-9._/-]*$"},"expected_sha":{"type":"string","pattern":r"^[0-9a-f]{40}$"}},"required":["kind","branch","expected_sha"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"job_start"},"tool":{"type":"string","enum":["pytest","bash_script","python_script","node_script","npm_script","pnpm_script","yarn_script","gradle_task","make_target","python_module"]},"path":{"type":"string","minLength":1,"maxLength":500},"script":{"type":"string","minLength":1,"maxLength":200},"cwd":{"type":"string","maxLength":500},"args":{"type":"array","maxItems":64,"items":{"type":"string","maxLength":500}},"timeout_seconds":{"type":"integer","minimum":1,"maximum":3600}},"required":["kind","tool"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"job_status"},"job_id":{"type":"string","pattern":r"^job_[0-9a-f]{24}$"}},"required":["kind","job_id"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"job_cancel"},"job_id":{"type":"string","pattern":r"^job_[0-9a-f]{24}$"}},"required":["kind","job_id"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"job_cleanup"},"job_id":{"type":"string","pattern":r"^job_[0-9a-f]{24}$"}},"required":["kind","job_id"],"additionalProperties":False},
    ]
}

# Extend the existing contracts before constructing the batch schema. No new runner.
_ALLOWED_TOOL_ENV = (
    "CI", "NODE_ENV", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
    "TZ", "LANG", "LC_ALL", "CHECK_BASE", "CHECK_OUTPUT",
)
_EXECUTION_OPTIONS = {
    "python_env": {"type": "string", "minLength": 1, "maxLength": 500,
                   "description": "auto (cwd/.venv, root/.venv, system), system, or a project-relative venv directory"},
    "env": {"type": "object", "maxProperties": len(_ALLOWED_TOOL_ENV),
            "propertyNames": {"enum": list(_ALLOWED_TOOL_ENV)},
            "additionalProperties": {"type": "string", "maxLength": 500}},
}
_EXPECTED_HEAD_SCHEMA = {
    "type": "string", "pattern": r"^[0-9a-f]{40}$",
    "description": (
        "Exact checkpoint SHA for the resolved physical project/worktree. If provided, execution is denied "
        "before side effects when HEAD differs; use find_projects/worktree_list to select the intended checkout."
    ),
}
_EXPECTED_WORKSPACE_DIGEST_SCHEMA = {
    "type": "string", "pattern": r"^[0-9a-f]{64}$",
    "description": (
        "Exact tracked_workspace_digest from project_probe git_state for this physical checkout. It covers "
        "HEAD, the Git index and tracked working-tree content while excluding untracked files. If provided, "
        "execution is denied before side effects when the tracked workspace differs."
    ),
}
_HEAD_GUARDED_KINDS = {
    "sync_ff", "git_switch_ff", "apply_source_patch", "artifact_fetch",
    "run_tool", "run_profile", "write_text", "mkdir", "move_path", "delete_file",
    "chmod_executable", "git_create_branch", "git_stage", "git_commit_push", "capture_handoff", "job_start",
}
for _variant in _PROJECT_ACTION_SCHEMA["oneOf"]:
    _kind = _variant["properties"]["kind"]["const"]
    if _kind in _HEAD_GUARDED_KINDS:
        _variant["properties"]["expected_head"] = _EXPECTED_HEAD_SCHEMA
        _variant["properties"]["expected_workspace_digest"] = _EXPECTED_WORKSPACE_DIGEST_SCHEMA
    if _kind in {"run_tool", "job_start"}:
        _variant["properties"].update(_EXECUTION_OPTIONS)
        _variant["description"] = (
            "Known short synchronous command. Use job_start instead for browser suites, full builds or unknown duration; do not hold a client RPC open."
            if _kind == "run_tool" else
            "Durable job_start: use a stable request_key, then job_status on the SAME project/worktree. Reuse the key after a lost response; use a new key for an intentional rerun."
        )
        _variant["properties"]["path"]["description"] = "Script file is project-root-relative, independent of cwd. Example: path=site/tests/check.mjs, cwd=site."
        _variant["properties"]["cwd"]["description"] = "Project-relative execution directory. npm_script requires package.json here; path is still relative to project root."
    if _kind == "job_start":
        _variant["properties"]["request_key"] = {"type": "string", "minLength": 1, "maxLength": 128}
    if _kind == "job_status":
        _variant["properties"]["tail_lines"] = {"type": "integer", "minimum": 0, "maximum": 200}
_PROJECT_ACTION_SCHEMA["oneOf"].extend([
    {"type": "object", "properties": {"kind": {"const": "incident_open"},
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
        "summary": {"type": "string", "minLength": 1, "maxLength": 12000},
        "severity": {"type": "string", "enum": ["sev1", "sev2", "sev3", "sev4", "unknown"]},
        "services": {"type": "array", "maxItems": 32, "items": {"type": "string", "minLength": 1, "maxLength": 100}},
        "tags": {"type": "array", "maxItems": 32, "items": {"type": "string", "minLength": 1, "maxLength": 100}},
        "related_incidents": {"type": "array", "maxItems": 32, "items": {"type": "string", "pattern": r"^inc_[0-9a-f]{24}$"}}},
        "required": ["kind", "title", "summary"], "additionalProperties": False},
    {"type": "object", "properties": {"kind": {"const": "incident_note"},
        "incident_id": {"type": "string", "pattern": r"^inc_[0-9a-f]{24}$"},
        "note": {"type": "string", "minLength": 1, "maxLength": 12000},
        "note_kind": {"type": "string", "enum": ["observation", "hypothesis", "mitigation", "verification", "decision", "followup"]},
        "evidence_refs": {"type": "array", "maxItems": 32, "items": {"type": "string", "minLength": 1, "maxLength": 500}}},
        "required": ["kind", "incident_id", "note"], "additionalProperties": False},
    {"type": "object", "properties": {"kind": {"const": "incident_close"},
        "incident_id": {"type": "string", "pattern": r"^inc_[0-9a-f]{24}$"},
        "root_cause": {"type": "string", "minLength": 1, "maxLength": 12000},
        "resolution": {"type": "string", "minLength": 1, "maxLength": 12000},
        "verification": {"type": "string", "minLength": 1, "maxLength": 12000},
        "action_items": {"type": "array", "maxItems": 100, "items": {"type": "string", "minLength": 1, "maxLength": 1000}}},
        "required": ["kind", "incident_id", "root_cause", "resolution", "verification"], "additionalProperties": False},
    {"type": "object", "properties": {"kind": {"const": "git_switch_ff"},
        "remote": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "branch": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"},
        "expected_sha": {"type": "string", "pattern": r"^[0-9a-f]{40}$"}},
        "required": ["kind", "remote", "branch", "expected_sha"], "additionalProperties": False},
    {"type": "object", "properties": {"kind": {"const": "apply_source_patch"},
        "base_sha": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
        "patch": {"type": "string", "minLength": 1, "maxLength": 16777216},
        "patch_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"}},
        "required": ["kind", "base_sha", "patch", "patch_sha256"], "additionalProperties": False},
    {"type": "object", "properties": {"kind": {"const": "artifact_fetch"},
        "url": {"type": "string", "minLength": 8, "maxLength": 4096},
        "path": {"type": "string", "minLength": 1, "maxLength": 500},
        "expected_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
        "expected_target_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
        "replace": {"type": "boolean"}, "create_parents": {"type": "boolean"},
        "max_bytes": {"type": "integer", "minimum": 1, "maximum": 8589934592},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600}},
        "required": ["kind", "url", "path"], "additionalProperties": False},
    {"type": "object", "properties": {"kind": {"const": "job_list"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "before": {"type": "string", "maxLength": 80}}, "required": ["kind"], "additionalProperties": False},
    {"type": "object", "properties": {"kind": {"const": "git_fetch"},
        "remote": {"type": "string", "minLength": 1, "maxLength": 64},
        "branch": {"type": "string", "minLength": 1, "maxLength": 200}},
        "required": ["kind", "branch"], "additionalProperties": False},
    {"type": "object", "properties": {"kind": {"const": "github_workflow_dispatch"},
        "remote": {"type": "string", "minLength": 1, "maxLength": 64},
        "workflow": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$"},
        "ref": {"type": "string", "minLength": 1, "maxLength": 200},
        "inputs": {"type": "object", "maxProperties": 25,
            "propertyNames": {"pattern": r"^[A-Za-z_][A-Za-z0-9_-]{0,79}$"},
            "additionalProperties": {"type": ["string", "boolean", "number"], "maxLength": 500}},
        "request_key": {"type": "string", "minLength": 8, "maxLength": 128,
                        "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"}},
        "required": ["kind", "workflow", "ref"], "additionalProperties": False},
    {"type": "object", "properties": {"kind": {"const": "github_run_cancel"},
        "remote": {"type": "string", "minLength": 1, "maxLength": 64},
        "run_id": {"type": "integer", "minimum": 1},
        "expected_head_sha": {"type": "string", "pattern": r"^[0-9a-f]{40}$"}},
        "required": ["kind", "run_id", "expected_head_sha"], "additionalProperties": False},
])
# Variants appended after the initial guard pass receive the same optimistic-concurrency guards.
for _variant in _PROJECT_ACTION_SCHEMA["oneOf"]:
    _kind = _variant["properties"]["kind"]["const"]
    if _kind in _HEAD_GUARDED_KINDS:
        _variant["properties"].setdefault("expected_head", _EXPECTED_HEAD_SCHEMA)
        _variant["properties"].setdefault("expected_workspace_digest", _EXPECTED_WORKSPACE_DIGEST_SCHEMA)


_BATCH_STEP_SCHEMA = {"oneOf": list(_PROJECT_ACTION_SCHEMA["oneOf"])}
_PROJECT_ACTION_SCHEMA["oneOf"].append({
    "type":"object",
    "properties":{
        "kind":{"const":"batch"},
        "steps":{"type":"array","minItems":1,"maxItems":20,"items":_BATCH_STEP_SCHEMA},
        "expected_head": _EXPECTED_HEAD_SCHEMA,
        "expected_workspace_digest": _EXPECTED_WORKSPACE_DIGEST_SCHEMA,
    },
    "required":["kind","steps"],
    "additionalProperties":False,
})

PROJECT_ACTION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "minLength": 1, "maxLength": 128},
        "action": _PROJECT_ACTION_SCHEMA,
    },
    "required": ["project", "action"],
    "additionalProperties": False,
}

HOST_ACTION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "minLength": 1, "maxLength": 128},
        "action": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "restart_service"},
                        "service": {"type": "string", "minLength": 1, "maxLength": 64,
                                    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                    },
                    "required": ["kind", "service"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "run_profile"},
                        "profile": {"type": "string", "minLength": 1, "maxLength": 64,
                                    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                    },
                    "required": ["kind", "profile"],
                    "additionalProperties": False,
                },
                {"type":"object","properties":{"kind":{"const":"service_action"},"service":{"type":"string","minLength":1,"maxLength":64,"pattern":r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},"operation":{"type":"string","enum":["start","stop","restart","reload","enable","disable"]}},"required":["kind","service","operation"],"additionalProperties":False},
                {"type":"object","properties":{"kind":{"const":"write_registered_file"},"file":{"type":"string","minLength":1,"maxLength":64,"pattern":r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},"content":{"type":"string","maxLength":2097152},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"}},"required":["kind","file","content"],"additionalProperties":False},
                {"type":"object","properties":{"kind":{"const":"delete_registered_file"},"file":{"type":"string","minLength":1,"maxLength":64,"pattern":r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"}},"required":["kind","file"],"additionalProperties":False},
                {"type":"object","properties":{"kind":{"const":"daemon_reload"}},"required":["kind"],"additionalProperties":False},
                {"type":"object","properties":{"kind":{"const":"root_git_sync_exact"},"root":{"type":"string","minLength":1,"maxLength":64},"remote":{"type":"string","minLength":1,"maxLength":64},"branch":{"type":"string","minLength":1,"maxLength":200},"expected_sha":{"type":"string","pattern":r"^[0-9a-f]{40}$"},"expected_head":{"type":"string","pattern":r"^[0-9a-f]{40}$"}},"required":["kind","root","branch","expected_sha"],"additionalProperties":False},
            ]
        },
    },
    "required": ["project", "action"],
    "additionalProperties": False,
}


def _handoff_binding(root: Path, project_id: str) -> tuple[str, bool, dict[str, Any] | None]:
    if not project_id.startswith("handoff:"):
        return project_id, False, None
    handoff_id = project_id.removeprefix("handoff:")
    if HANDOFF_ID_RE.fullmatch(handoff_id) is None:
        raise ProbeError("invalid_input", "invalid source handoff id")
    handoff_root = (Path.home() / ".local" / "share" / "openai-codex-mcp" / "handoffs").resolve()
    root_real = root.resolve(strict=True)
    try:
        relative = root_real.relative_to(handoff_root)
    except ValueError as exc:
        raise ProbeError("handoff_denied", "handoff workspace is outside the managed root", status="denied") from exc
    if len(relative.parts) != 2 or relative.parts != (handoff_id, "worktree"):
        raise ProbeError("handoff_denied", "handoff workspace layout is invalid", status="denied")
    meta_path = root_real.parent / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProbeError("handoff_invalid", "handoff metadata is unavailable") from exc
    if not isinstance(meta, dict) or meta.get("id") != handoff_id or meta.get("worktree") != str(root_real):
        raise ProbeError("handoff_invalid", "handoff metadata does not match workspace")
    original = meta.get("projectId")
    if not isinstance(original, str) or not original:
        raise ProbeError("handoff_invalid", "handoff project binding is missing")
    return original, True, meta


def _registered_remote(binding: dict[str, Any], alias: str) -> str:
    alias = _validate_alias(alias)
    remotes = binding.get("remotes") or {}
    if not isinstance(remotes, dict):
        raise ProbeError("config", "trusted remotes section is invalid")
    repo = remotes.get(alias)
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ProbeError("remote_denied", "remote is not registered", status="denied")
    return repo


def _remote_repo(url: str) -> str | None:
    match = re.search(r"github\.com(?::|/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", url.strip())
    return match.group(1) if match else None


async def _git(root: Path, *args: str, timeout: float = 60.0):
    argv = [
        "/usr/bin/git", "--no-optional-locks",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        "-c", "diff.external=",
        "-C", str(root), *args,
    ]
    return await _run_process(argv, env=_git_env(), timeout=timeout, output_limit=MAX_ACTION_OUTPUT)


def _decode(result, stream: str) -> tuple[str, bool]:
    raw = result.stdout if stream == "stdout" else result.stderr
    text, redacted = _redact_text(raw.decode("utf-8", "replace"))
    return text, redacted


async def _sync_ff(root: Path, binding: dict[str, Any], action: dict[str, Any], *, handoff: bool) -> dict[str, Any]:
    if handoff:
        raise ProbeError("handoff_denied", "sync_ff is only allowed on the canonical checkout", status="denied")
    remote = _validate_alias(action["remote"])
    branch = _validate_branch(action["branch"])
    expected = str(action["expected_sha"]).lower()
    if HEX40_RE.fullmatch(expected) is None:
        raise ProbeError("invalid_input", "expected_sha must be exact 40-hex")

    if binding.get("remotes"):
        registered_repo = _registered_remote(binding, remote)
        remote_url = await _git(root, "remote", "get-url", remote, timeout=15)
        if remote_url.exit_code != 0 or _remote_repo(remote_url.stdout.decode("utf-8", "replace")) != registered_repo:
            raise ProbeError("remote_mismatch", "configured Git remote does not match trusted repository", status="denied")
    else:
        registered_repo, _remote_url = await resolve_github_remote(root, binding, remote)

    current_branch = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=10)
    if current_branch.exit_code != 0 or current_branch.stdout.decode().strip() != branch:
        raise ProbeError("branch_mismatch", "canonical checkout is not on the requested branch", status="denied")

    dirty = await _git(root, "status", "--porcelain=v1", "--untracked-files=no", timeout=15)
    if dirty.exit_code != 0:
        raise ProbeError("git_state", "could not inspect tracked working-tree state", exit_code=dirty.exit_code)
    if dirty.stdout.strip():
        raise ProbeError("dirty_tracked", "tracked/staged changes block direct fast-forward", status="denied")

    old = await _git(root, "rev-parse", "HEAD", timeout=10)
    if old.exit_code != 0:
        raise ProbeError("git_state", "could not resolve current HEAD", exit_code=old.exit_code)
    old_sha = old.stdout.decode().strip()

    fetched = await _git(root, "fetch", "--no-tags", remote, f"refs/heads/{branch}", timeout=120)
    if fetched.exit_code != 0:
        err, red = _decode(fetched, "stderr")
        raise ProbeError("fetch_failed", err[-1200:] or "git fetch failed", exit_code=fetched.exit_code)

    observed = await _git(root, "rev-parse", "FETCH_HEAD", timeout=10)
    observed_sha = observed.stdout.decode().strip() if observed.exit_code == 0 else ""
    if observed_sha != expected:
        raise ProbeError("remote_moved", f"fetched remote SHA {observed_sha or '<unknown>'} differs from expected {expected}", status="denied")

    ancestor = await _git(root, "merge-base", "--is-ancestor", old_sha, expected, timeout=15)
    if ancestor.exit_code != 0:
        raise ProbeError("non_fast_forward", "requested target is not a fast-forward of current HEAD", status="denied")

    changed = old_sha != expected
    if changed:
        merged = await _git(root, "merge", "--ff-only", expected, timeout=60)
        if merged.exit_code != 0:
            err, _ = _decode(merged, "stderr")
            raise ProbeError("fast_forward_failed", err[-1200:] or "git fast-forward failed", exit_code=merged.exit_code)

    untracked = await _git(root, "ls-files", "--others", "--exclude-standard", "-z", timeout=15)
    untracked_count = 0 if not untracked.stdout else len([x for x in untracked.stdout.split(b"\0") if x])
    return {
        "status": "ok",
        "action": "sync_ff",
        "remote": remote,
        "branch": branch,
        "old_sha": old_sha,
        "new_sha": expected,
        "changed": changed,
        "untracked_preserved_count": untracked_count,
    }


async def _git_switch_ff(root: Path, binding: dict[str, Any], action: dict[str, Any], *, handoff: bool) -> dict[str, Any]:
    """Switch to an existing local branch and fast-forward it to one exact remote SHA."""
    if handoff:
        raise ProbeError("handoff_denied", "git_switch_ff is only allowed on a canonical checkout", status="denied")
    remote = _validate_alias(action["remote"])
    branch = _validate_branch(action["branch"])
    expected = str(action["expected_sha"]).lower()
    if HEX40_RE.fullmatch(expected) is None:
        raise ProbeError("invalid_input", "expected_sha must be exact 40-hex")

    if binding.get("remotes"):
        registered_repo = _registered_remote(binding, remote)
        remote_url = await _git(root, "remote", "get-url", remote, timeout=15)
        if remote_url.exit_code != 0 or _remote_repo(remote_url.stdout.decode("utf-8", "replace")) != registered_repo:
            raise ProbeError("remote_mismatch", "configured Git remote does not match trusted repository", status="denied")
    else:
        await resolve_github_remote(root, binding, remote)

    dirty = await _git(root, "status", "--porcelain=v1", "--untracked-files=no", timeout=15)
    if dirty.exit_code != 0:
        raise ProbeError("git_state", "could not inspect tracked working-tree state", exit_code=dirty.exit_code)
    if dirty.stdout.strip():
        raise ProbeError(
            "dirty_tracked",
            "tracked/staged changes block the typed branch switch; use the full-power reasoning/shell path when a deliberate dirty-worktree operation is required",
            status="denied",
        )

    old_head_result = await _git(root, "rev-parse", "HEAD", timeout=10)
    if old_head_result.exit_code != 0:
        raise ProbeError("git_state", "could not resolve current HEAD", exit_code=old_head_result.exit_code)
    old_head = old_head_result.stdout.decode().strip()
    old_branch_result = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=10)
    old_branch = old_branch_result.stdout.decode().strip() if old_branch_result.exit_code == 0 else None

    existing = await _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", timeout=10)
    if existing.exit_code != 0:
        raise ProbeError(
            "branch_not_found",
            "git_switch_ff requires an existing local branch; branch creation remains available through git_create_branch or the reasoning/shell escape hatch",
            status="not_found",
        )
    local_ref = await _git(root, "rev-parse", f"refs/heads/{branch}", timeout=10)
    local_sha = local_ref.stdout.decode().strip() if local_ref.exit_code == 0 else ""
    if HEX40_RE.fullmatch(local_sha) is None:
        raise ProbeError("git_state", "could not resolve existing local target branch")

    fetched = await _git(root, "fetch", "--no-tags", remote, f"refs/heads/{branch}", timeout=120)
    if fetched.exit_code != 0:
        err, _ = _decode(fetched, "stderr")
        raise ProbeError("fetch_failed", err[-1200:] or "git fetch failed", exit_code=fetched.exit_code)
    observed = await _git(root, "rev-parse", "FETCH_HEAD", timeout=10)
    observed_sha = observed.stdout.decode().strip() if observed.exit_code == 0 else ""
    if observed_sha != expected:
        raise ProbeError(
            "remote_moved",
            f"fetched remote SHA {observed_sha or '<unknown>'} differs from expected {expected}",
            status="denied",
        )
    ancestor = await _git(root, "merge-base", "--is-ancestor", local_sha, expected, timeout=15)
    if ancestor.exit_code != 0:
        raise ProbeError("non_fast_forward", "remote target is not a fast-forward of the existing local branch", status="denied")

    if old_branch != branch:
        switched = await _git(root, "switch", branch, timeout=30)
        if switched.exit_code != 0:
            err, _ = _decode(switched, "stderr")
            raise ProbeError("branch_switch_failed", err[-1200:] or "git switch failed", exit_code=switched.exit_code)
    if local_sha != expected:
        merged = await _git(root, "merge", "--ff-only", expected, timeout=60)
        if merged.exit_code != 0:
            err, _ = _decode(merged, "stderr")
            raise ProbeError("fast_forward_failed", err[-1200:] or "git fast-forward failed", exit_code=merged.exit_code)

    untracked = await _git(root, "ls-files", "--others", "--exclude-standard", "-z", timeout=15)
    untracked_count = 0 if not untracked.stdout else len([x for x in untracked.stdout.split(b"\0") if x])
    return {
        "status": "ok", "action": "git_switch_ff", "remote": remote, "branch": branch,
        "old_branch": old_branch, "old_head": old_head, "local_branch_sha": local_sha,
        "new_sha": expected, "changed": old_branch != branch or local_sha != expected,
        "untracked_preserved_count": untracked_count,
    }


async def _apply_source_patch(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    """Apply an inert Git patch as source data; never execute patch contents."""
    patch = action["patch"]
    data = patch.encode("utf-8")
    if len(data) > MAX_SOURCE_PATCH_BYTES:
        raise ProbeError("patch_too_large", "source patch exceeds 16 MiB", status="denied")
    expected_digest = str(action["patch_sha256"]).lower()
    observed_digest = hashlib.sha256(data).hexdigest()
    if observed_digest != expected_digest:
        raise ProbeError("precondition_failed", "source patch SHA-256 does not match patch_sha256", status="denied")
    base_sha = str(action["base_sha"]).lower()
    head = await _git(root, "rev-parse", "HEAD", timeout=15)
    observed_head = head.stdout.decode("utf-8", "replace").strip() if head.exit_code == 0 else ""
    if observed_head != base_sha:
        raise ProbeError(
            "workspace_drift",
            f"source patch base {base_sha} does not match current HEAD {observed_head or '<unknown>'}",
            status="denied",
        )

    fd, patch_path = tempfile.mkstemp(prefix="devcoveer-source-", suffix=".patch")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        checked = await _git(root, "apply", "--check", "--binary", "--whitespace=nowarn", patch_path, timeout=60)
        if checked.exit_code != 0:
            err, _ = _decode(checked, "stderr")
            raise ProbeError("patch_check", err[-2000:] or "git apply --check failed", status="denied")
        applied = await _git(root, "apply", "--binary", "--whitespace=nowarn", patch_path, timeout=120)
        if applied.exit_code != 0:
            err, _ = _decode(applied, "stderr")
            raise ProbeError("patch_apply", err[-2000:] or "git apply failed", exit_code=applied.exit_code)
    finally:
        try:
            os.unlink(patch_path)
        except FileNotFoundError:
            pass
    return {
        "status": "ok", "action": "apply_source_patch", "base_sha": base_sha,
        "patch_sha256": observed_digest, "bytes": len(data),
        "note": "patch contents were treated as inert source data and were not executed",
    }


def _validate_artifact_url(raw: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ProbeError("url_denied", "artifact_fetch accepts arbitrary HTTPS URLs only", status="denied")
    if parsed.username is not None or parsed.password is not None:
        raise ProbeError(
            "url_denied",
            "URL userinfo is not accepted by artifact_fetch; signed HTTPS query parameters remain supported",
            status="denied",
        )
    return parsed


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_artifact_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _artifact_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_fetch_sync(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    source = _validate_artifact_url(action["url"])
    max_bytes = int(action.get("max_bytes", DEFAULT_ARTIFACT_MAX_BYTES))
    if not (1 <= max_bytes <= MAX_ARTIFACT_MAX_BYTES):
        raise ProbeError("invalid_input", "artifact max_bytes is out of bounds")
    timeout = int(action.get("timeout_seconds", 300))
    if not (1 <= timeout <= 3600):
        raise ProbeError("invalid_input", "artifact timeout_seconds is out of bounds")

    raw_path = action["path"]
    parent_raw = str(Path(raw_path).parent).replace("\\", "/")
    if action.get("create_parents", True) and parent_raw not in {"", "."}:
        mkdir(root, {"path": parent_raw, "parents": True})
    path = _mutation_path(root, raw_path)
    old_sha = None
    old_mode = 0o644
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise ProbeError("file_type", "artifact target must be a regular file", status="denied")
        old_sha = _artifact_file_sha256(path)
        expected_target = action.get("expected_target_sha256")
        if expected_target is not None and old_sha != expected_target:
            raise ProbeError("precondition_failed", "artifact target SHA-256 does not match expected_target_sha256", status="denied")
        if action.get("replace") is not True:
            raise ProbeError("precondition_failed", "artifact target exists; set replace=true to replace it", status="denied")
        old_mode = path.stat().st_mode & 0o777
    elif action.get("expected_target_sha256") is not None:
        raise ProbeError("precondition_failed", "expected artifact target is absent", status="denied")

    request = urllib.request.Request(action["url"], headers={"User-Agent": "DevCoveer-artifact-fetch/1"})
    opener = urllib.request.build_opener(_HttpsOnlyRedirect())
    fd, temp_name = tempfile.mkstemp(prefix=".devcoveer-fetch-", dir=path.parent)
    bytes_written = 0
    digest = hashlib.sha256()
    final_url = action["url"]
    content_type = None
    try:
        os.fchmod(fd, old_mode)
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise ProbeError("artifact_http", f"artifact HTTPS request failed with HTTP {exc.code}", exit_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise ProbeError("artifact_network", f"artifact HTTPS request failed for host {source.hostname}") from exc
        with response:
            final_url = response.geturl()
            final = _validate_artifact_url(final_url)
            content_type = response.headers.get("Content-Type")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    announced = int(content_length)
                except ValueError:
                    announced = None
                if announced is not None and announced > max_bytes:
                    raise ProbeError("artifact_too_large", "artifact exceeds max_bytes", status="denied")
            with os.fdopen(fd, "wb", closefd=False) as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > max_bytes:
                        raise ProbeError("artifact_too_large", "artifact exceeds max_bytes", status="denied")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        observed = digest.hexdigest()
        expected = action.get("expected_sha256")
        if expected is not None and observed != expected:
            raise ProbeError("artifact_hash_mismatch", "downloaded artifact SHA-256 does not match expected_sha256", status="denied")
        os.replace(temp_name, path)
        final = _validate_artifact_url(final_url)
        return {
            "status": "ok", "action": "artifact_fetch", "path": raw_path,
            "bytes": bytes_written, "sha256": observed, "old_sha256": old_sha,
            "source_host": source.hostname, "source_path": source.path,
            "final_host": final.hostname, "final_path": final.path,
            "source_url_sha256": hashlib.sha256(action["url"].encode("utf-8")).hexdigest(),
            "content_type": content_type,
            "replaced": old_sha is not None,
            "note": "query strings and credentials are not returned in the result",
        }
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


async def _artifact_fetch(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_artifact_fetch_sync, root, action)


def _project_cwd(root: Path, raw: str | None) -> Path:
    if raw in (None, "", "."):
        return root.resolve(strict=True)
    return _safe_worktree_path(root, raw, require_file=False)


def _args(action: dict[str, Any]) -> list[str]:
    values = action.get("args") or []
    if not isinstance(values, list) or len(values) > 64:
        raise ProbeError("invalid_input", "args must be a bounded array")
    out: list[str] = []
    for value in values:
        if not isinstance(value, str) or not ARG_RE.fullmatch(value):
            raise ProbeError("invalid_input", "argument contains a denied character or is too long")
        out.append(value)
    return out


def _fixed_executable(*candidates: str) -> str:
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return candidate
    raise ProbeError("tool_unavailable", "requested direct project tool is not installed", status="unavailable")


def _project_python(root: Path, cwd: Path, selection: str = "auto") -> str:
    if selection == "system":
        return "/usr/bin/python3"
    candidates = [cwd / ".venv", root / ".venv"] if selection == "auto" else [root / selection]
    for directory in dict.fromkeys(candidates):
        if not directory.exists() and not directory.is_symlink() and selection == "auto":
            continue
        try:
            relative = directory.relative_to(root).as_posix()
        except ValueError as exc:
            raise ProbeError("path_denied", "python_env must be a project-relative venv directory") from exc
        directory = _safe_worktree_path(root, relative)
        _safe_worktree_path(root, (directory / "pyvenv.cfg").relative_to(root).as_posix(), require_file=True)
        _safe_worktree_path(root, (directory / "bin").relative_to(root).as_posix())
        executable = directory / "bin" / "python"
        # A standard venv interpreter is a symlink; the venv and bin directory are not.
        if executable.is_file() and os.access(executable, os.X_OK):
            return str(executable)
        raise ProbeError("environment_unavailable", "selected venv has no executable Python", status="unavailable")
    return "/usr/bin/python3"


def _tool_env(action: dict[str, Any], python: str | None) -> dict[str, str]:
    extra = action.get("env") or {}
    allowed = set(_ALLOWED_TOOL_ENV)
    if not isinstance(extra, dict) or set(extra) - allowed:
        raise ProbeError("invalid_input", "only documented nonsecret execution flags may be overridden")
    env = _action_env({"env": extra})
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if python and python != "/usr/bin/python3":
        env["VIRTUAL_ENV"] = str(Path(python).parent.parent)
        env["PATH"] = str(Path(python).parent) + ":" + env["PATH"]
    return env


async def _run_tool(root: Path, action: dict[str, Any], *, log_dir: Path | None = None) -> dict[str, Any]:
    tool = action["tool"]
    cwd = _project_cwd(root, action.get("cwd"))
    args = _args(action)
    python = _project_python(root, cwd, action.get("python_env", "auto")) if tool in {"pytest", "python_script", "python_module"} or "python_env" in action else None
    timeout = float(action.get("timeout_seconds", 900))
    if not (1 <= timeout <= MAX_ACTION_TIMEOUT):
        raise ProbeError("invalid_input", "tool timeout is out of bounds")

    if tool == "pytest":
        argv = [python, "-m", "pytest", *args]
    elif tool in {"bash_script", "python_script", "node_script"}:
        raw_path = action.get("path")
        if not isinstance(raw_path, str):
            raise ProbeError("invalid_input", f"{tool} requires path")
        script = _safe_worktree_path(root, raw_path, require_file=True)
        if tool == "python_script":
            if script.suffix != ".py":
                raise ProbeError("invalid_input", "python_script requires a .py repository file")
            executable = python
        elif tool == "node_script":
            if script.suffix not in {".js", ".mjs", ".cjs"}:
                raise ProbeError("invalid_input", "node_script requires a .js/.mjs/.cjs repository file")
            executable = _fixed_executable("/home/dev/.local/bin/node", "/usr/local/bin/node", "/usr/bin/node")
        else:
            executable = "/usr/bin/bash"
        argv = [executable, str(script), *args]
    elif tool == "npm_script":
        script = action.get("script")
        if not isinstance(script, str) or NPM_SCRIPT_RE.fullmatch(script) is None:
            raise ProbeError("invalid_input", "npm_script requires a safe script name")
        package_json = cwd / "package.json"
        if not package_json.is_file() or package_json.is_symlink():
            raise ProbeError("not_found", "package.json is missing in requested project cwd", status="not_found")
        executable = _fixed_executable("/home/dev/.local/bin/npm", "/usr/local/bin/npm", "/usr/bin/npm")
        argv = [executable, "run", "--silent", script]
        if args:
            argv.extend(["--", *args])
    else:
        extended = runner_argv(root, action, cwd)
        if extended is None:
            raise ProbeError("invalid_input", f"unknown direct project tool {tool!r}")
        argv = extended
        if tool == "python_module":
            argv[0] = python

    result = await _run_process(
        argv, cwd=cwd, env=_tool_env(action, python), timeout=timeout, output_limit=MAX_ACTION_OUTPUT,
        capture_tail=True, log_dir=log_dir,
    )
    stdout, out_red = _decode(result, "stdout")
    stderr, err_red = _decode(result, "stderr")
    return {
        "status": "ok" if result.exit_code == 0 else "failed",
        "action": "run_tool",
        "tool": tool,
        "executable": argv[0], "cwd": str(cwd), "output_mode": "tail",
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": result.truncated,
        "redacted": out_red or err_red,
    }


def _profile(binding: dict[str, Any], section: str, name: str) -> dict[str, Any]:
    name = _validate_alias(name)
    profiles = binding.get(section) or {}
    if not isinstance(profiles, dict):
        raise ProbeError("config", f"trusted {section} section is invalid")
    profile = profiles.get(name)
    if not isinstance(profile, dict):
        raise ProbeError("profile_denied", "action profile is not registered", status="denied")
    return profile


def _action_env(profile: dict[str, Any]) -> dict[str, str]:
    env = {
        "PATH": "/home/dev/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/home/dev"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "1",
    }
    extra = profile.get("env") or {}
    if not isinstance(extra, dict):
        raise ProbeError("config", "profile env must be an object")
    for key, value in extra.items():
        if not isinstance(key, str) or ENV_NAME_RE.fullmatch(key) is None or SECRET_ENV_RE.search(key):
            raise ProbeError("config", "profile env contains a denied key")
        if not isinstance(value, str) or len(value) > 500 or "\x00" in value:
            raise ProbeError("config", "profile env contains an invalid value")
        env[key] = value
    return env


async def _run_profile(root: Path, binding: dict[str, Any], profile_name: str, *,
                       section: str, handoff: bool) -> dict[str, Any]:
    profile = _profile(binding, section, profile_name)
    if handoff and profile.get("allow_handoff") is not True:
        raise ProbeError("handoff_denied", "profile is not approved for source-handoff worktrees", status="denied")

    argv = profile.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or "\x00" in x for x in argv):
        raise ProbeError("config", "action profile argv is invalid")
    if not Path(argv[0]).is_absolute():
        raise ProbeError("config", "action profile executable must be absolute")

    cwd_raw = profile.get("cwd", ".")
    if not isinstance(cwd_raw, str) or "\x00" in cwd_raw:
        raise ProbeError("config", "action profile cwd is invalid")
    if cwd_raw.startswith("/"):
        cwd = Path(cwd_raw).resolve(strict=True)
        if section == "project_actions":
            try:
                cwd.relative_to(root.resolve(strict=True))
            except ValueError as exc:
                raise ProbeError("config", "project action cwd escapes the project") from exc
    else:
        cwd = (root / cwd_raw).resolve(strict=True)
        try:
            cwd.relative_to(root.resolve(strict=True))
        except ValueError as exc:
            raise ProbeError("config", "action cwd escapes the project") from exc

    timeout = float(profile.get("timeout_seconds", 900))
    if not (1 <= timeout <= MAX_ACTION_TIMEOUT):
        raise ProbeError("config", "action profile timeout is out of bounds")
    output_limit = int(profile.get("output_bytes", 128 * 1024))
    if not (1024 <= output_limit <= MAX_ACTION_OUTPUT):
        raise ProbeError("config", "action profile output limit is out of bounds")

    result = await _run_process(argv, cwd=cwd, env=_action_env(profile),
                                timeout=timeout, output_limit=output_limit)
    stdout, out_red = _decode(result, "stdout")
    stderr, err_red = _decode(result, "stderr")
    return {
        "status": "ok" if result.exit_code == 0 else "failed",
        "action": "run_profile",
        "profile": profile_name,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": result.truncated,
        "redacted": out_red or err_red,
    }


async def _handoff_commit_push(root: Path, binding: dict[str, Any], action: dict[str, Any],
                               meta: dict[str, Any] | None) -> dict[str, Any]:
    if meta is None:
        raise ProbeError("handoff_required", "handoff_commit_push requires a handoff:<id> project", status="denied")
    remote = _validate_alias(action["remote"])
    branch = _validate_branch(action["branch"])
    if not branch.startswith("chatgpt/"):
        raise ProbeError("branch_denied", "direct handoff push is limited to chatgpt/* branches", status="denied")
    message = action["commit_message"].strip()
    if not message or any(ord(ch) < 32 and ch not in "\t" for ch in message):
        raise ProbeError("invalid_input", "commit message contains control characters")
    if binding.get("remotes"):
        registered_repo = _registered_remote(binding, remote)
        remote_url = await _git(root, "remote", "get-url", remote, timeout=15)
        if remote_url.exit_code != 0 or _remote_repo(remote_url.stdout.decode("utf-8", "replace")) != registered_repo:
            raise ProbeError("remote_mismatch", "configured Git remote does not match trusted repository", status="denied")
    else:
        registered_repo, _remote_url = await resolve_github_remote(root, binding, remote)

    base = str(meta.get("baseSha") or "")
    if HEX40_RE.fullmatch(base) is None:
        raise ProbeError("handoff_invalid", "handoff base SHA is invalid")
    head = await _git(root, "rev-parse", "HEAD", timeout=10)
    head_sha = head.stdout.decode().strip() if head.exit_code == 0 else ""
    current_branch = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=10)
    branch_name = current_branch.stdout.decode().strip() if current_branch.exit_code == 0 else ""

    if head_sha != base:
        if branch_name != branch:
            raise ProbeError("handoff_changed", "handoff HEAD moved unexpectedly", status="denied")
        pushed = await _git(root, "push", remote, f"HEAD:refs/heads/{branch}", timeout=120)
        if pushed.exit_code != 0:
            err, _ = _decode(pushed, "stderr")
            raise ProbeError("push_failed", err[-1200:] or "git push failed", exit_code=pushed.exit_code)
        return {"status": "ok", "action": "handoff_commit_push", "branch": branch,
                "commit_sha": head_sha, "reused_commit": True}

    unstaged = await _git(root, "diff", "--quiet", timeout=15)
    if unstaged.exit_code not in (0, 1):
        raise ProbeError("git_state", "could not inspect unstaged diff", exit_code=unstaged.exit_code)
    if unstaged.exit_code == 1:
        raise ProbeError("dirty_unstaged", "unstaged changes block direct handoff commit", status="denied")
    others = await _git(root, "ls-files", "--others", "--exclude-standard", "-z", timeout=15)
    if others.stdout:
        raise ProbeError("untracked_files", "untracked files block direct handoff commit", status="denied")
    staged = await _git(root, "diff", "--cached", "--quiet", base, timeout=15)
    if staged.exit_code == 0:
        raise ProbeError("empty_patch", "handoff has no staged source patch", status="denied")
    if staged.exit_code != 1:
        raise ProbeError("git_state", "could not inspect staged patch", exit_code=staged.exit_code)
    check = await _git(root, "diff", "--cached", "--check", base, timeout=30)
    if check.exit_code != 0:
        out, _ = _decode(check, "stdout")
        raise ProbeError("patch_check", out[-1200:] or "git diff --cached --check failed", status="denied")

    existing = await _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", timeout=10)
    if existing.exit_code == 0:
        raise ProbeError("branch_exists", "target branch already exists locally", status="denied")
    created = await _git(root, "switch", "-c", branch, timeout=20)
    if created.exit_code != 0:
        err, _ = _decode(created, "stderr")
        raise ProbeError("branch_create", err[-1200:] or "could not create branch", exit_code=created.exit_code)

    committed = await _git(root, "-c", "commit.gpgsign=false", "commit", "-m", message, timeout=60)
    if committed.exit_code != 0:
        err, _ = _decode(committed, "stderr")
        raise ProbeError("commit_failed", err[-1200:] or "git commit failed", exit_code=committed.exit_code)
    new_head = await _git(root, "rev-parse", "HEAD", timeout=10)
    commit_sha = new_head.stdout.decode().strip()
    pushed = await _git(root, "push", remote, f"HEAD:refs/heads/{branch}", timeout=120)
    if pushed.exit_code != 0:
        err, _ = _decode(pushed, "stderr")
        raise ProbeError("push_failed", err[-1200:] or "git push failed", exit_code=pushed.exit_code)
    return {"status": "ok", "action": "handoff_commit_push", "branch": branch,
            "commit_sha": commit_sha, "reused_commit": False}


async def _run_direct_ops_compat(root: Path, project_id: str, action: dict[str, Any]) -> dict[str, Any]:
    if action.get("tool") != "python_script" or action.get("path") != DIRECT_OPS_COMPAT_PATH:
        raise ProbeError("invalid_input", "invalid Direct Ops compatibility carrier")
    if action.get("cwd") is not None or action.get("script") is not None:
        raise ProbeError("invalid_input", "Direct Ops compatibility carrier does not accept cwd/script", status="denied")
    args = action.get("args") or []
    if len(args) < 2:
        raise ProbeError("invalid_input", "Direct Ops compatibility carrier requires plane and operation")
    plane, operation = args[0], args[1]
    if plane == "project_action" and operation == "run_tool":
        raise ProbeError("compat_recursion", "nested run_tool through Direct Ops compatibility carrier is denied", status="denied")
    raw_payload = "".join(args[2:]) if len(args) > 2 else "{}"
    if len(raw_payload) > 32 * 1024:
        raise ProbeError("invalid_input", "Direct Ops compatibility payload exceeds 32 KiB", status="denied")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ProbeError("invalid_input", "Direct Ops compatibility payload is not valid JSON", status="denied") from exc
    if not isinstance(payload, dict):
        raise ProbeError("invalid_input", "Direct Ops compatibility payload must be a JSON object", status="denied")
    from direct_ops_gateway import run_direct_ops_v2
    result = await run_direct_ops_v2(str(root), project_id, plane, operation, payload)
    status = "ok"
    if plane.endswith("_action") and isinstance(result, dict):
        inner_status = str(result.get("status") or "ok")
        if inner_status in {"failed", "error", "denied", "timeout"}:
            status = inner_status
    return {
        "status": status,
        "action": "direct_ops_v2_compat",
        "plane": plane,
        "operation": operation,
        "result": result,
    }


async def _guard_expected_head(root: Path, action: dict[str, Any]) -> str | None:
    expected = action.get("expected_head")
    observed = None
    if expected is not None:
        result = await _git(root, "rev-parse", "HEAD", timeout=15)
        observed = result.stdout.decode("utf-8", "replace").strip()
        if observed != expected:
            raise ProbeError(
                "workspace_drift",
                f"resolved project/worktree HEAD {observed} does not match expected checkpoint {expected}; "
                "use find_projects/worktree_list and address the intended physical checkout before execution",
                status="denied",
            )
    expected_workspace = action.get("expected_workspace_digest")
    if expected_workspace is not None:
        observed_workspace = await _tracked_workspace_digest(root)
        if observed_workspace != expected_workspace:
            raise ProbeError(
                "workspace_drift",
                f"tracked workspace digest {observed_workspace} does not match expected {expected_workspace}; "
                "refresh project_probe git_state and preserve the intended tracked workspace before execution",
                status="denied",
            )
    return observed


async def _run_batch(project_root: str, project_id: str, action: dict[str, Any], *,
                     config_path: Path | None) -> dict[str, Any]:
    results=[]
    for index,step in enumerate(action["steps"]):
        try:
            result=await _run_project_action_inner(project_root,project_id,step,config_path=config_path)
        except ProbeError as exc:
            message,redacted=_redact_text(str(exc))
            return {"status":"failed","action":"batch","failed_index":index,"results":results,
                    "error":{"category":exc.category,"message":message[:2000]},"redacted":redacted}
        results.append(result)
        if isinstance(result,dict) and result.get("status") in {"failed","error","denied","timeout"}:
            return {"status":"failed","action":"batch","failed_index":index,"results":results}
    return {"status":"ok","action":"batch","results":results}


async def _run_project_action_inner(project_root: str, project_id: str, action: dict[str, Any], *,
                                    config_path: Path | None = None) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=True)
    binding_id, handoff, meta = _handoff_binding(root, project_id)
    config = _load_config(config_path or DEFAULT_CONFIG_PATH)
    if handoff:
        binding = _binding(config, binding_id)
        binding_inherited = False
    else:
        binding_id, binding, binding_inherited = await _binding_for_root(config, binding_id, root)
    kind = action["kind"]
    await _guard_expected_head(root, action)
    if kind == "batch":
        return await _run_batch(project_root, project_id, action, config_path=config_path)
    incident_project = str(binding.get("github_repo") or binding_id)
    if kind == "incident_open": return open_incident(incident_project, action)
    if kind == "incident_note": return note_incident(incident_project, action)
    if kind == "incident_close": return close_incident(incident_project, action)
    if kind == "github_pr_create":
        return await pr_create(root, binding, action)
    if kind == "github_pr_update":
        return await pr_update(root, binding, action)
    if kind == "github_pr_merge":
        return await pr_merge(root, binding, action)
    if kind == "github_branch_delete":
        return await delete_remote_branch(root, binding, action)
    if kind == "git_fetch":
        return await git_fetch(root, binding, action)
    if kind == "github_workflow_dispatch":
        return await workflow_dispatch(root, binding, action)
    if kind == "github_run_cancel":
        return await run_cancel(root, binding, action)
    if kind == "job_list":
        return list_jobs(root, limit=action.get("limit", 20), before=action.get("before"))
    if kind == "job_start":
        return await start_job(root, action, project_id=project_id)
    if kind == "job_status":
        return await job_status(root, action["job_id"], tail_lines=action.get("tail_lines", 40))
    if kind == "job_cancel":
        return await cancel_job(root, action["job_id"])
    if kind == "job_cleanup":
        return cleanup_job(root, action["job_id"])
    if kind == "sync_ff":
        return await _sync_ff(root, binding, action, handoff=handoff)
    if kind == "git_switch_ff":
        return await _git_switch_ff(root, binding, action, handoff=handoff)
    if kind == "apply_source_patch":
        return await _apply_source_patch(root, action)
    if kind == "artifact_fetch":
        return await _artifact_fetch(root, action)
    if kind == "run_tool":
        if action.get("tool") == "python_script" and action.get("path") == DIRECT_OPS_COMPAT_PATH:
            return await _run_direct_ops_compat(root, project_id, action)
        return await _run_tool(root, action)
    if kind == "run_profile":
        return await _run_profile(root, binding, action["profile"], section="project_actions", handoff=handoff)
    if kind == "handoff_commit_push":
        return await _handoff_commit_push(root, binding, action, meta)
    if kind == "write_text": return write_text(root, action)
    if kind == "mkdir": return mkdir(root, action)
    if kind == "move_path": return move_path(root, action)
    if kind == "delete_file": return delete_file(root, action)
    if kind == "chmod_executable": return chmod_executable(root, action)
    if kind == "git_create_branch": return await git_create_branch(root, action)
    if kind == "git_stage": return await git_stage(root, action)
    if kind == "git_commit_push": return await git_commit_push(root, binding, action)
    if kind == "capture_handoff":
        if handoff: raise ProbeError("handoff_denied", "capture_handoff requires a canonical project", status="denied")
        return await capture_handoff(root, binding_id, action)
    if kind == "worktree_capture_handoff":
        if handoff: raise ProbeError("handoff_denied", "worktree capture requires a canonical project", status="denied")
        return await capture_worktree_handoff(root, binding_id, action)
    if kind == "worktree_reconcile":
        if handoff: raise ProbeError("handoff_denied", "worktree reconcile requires a canonical project", status="denied")
        return await reconcile_worktree(root, binding_id, action)
    if kind == "owned_service_action":
        if handoff: raise ProbeError("handoff_denied", "owned service action requires a canonical project", status="denied")
        return await owned_service_action(root, action)
    raise ProbeError("invalid_input", f"unknown project action kind {kind!r}")


async def _action_receipt_head(root: Path) -> str | None:
    try:
        result = await _git(root, "rev-parse", "HEAD", timeout=15)
    except (OSError, ProbeError):
        return None
    if result.exit_code != 0:
        return None
    value = result.stdout.decode("utf-8", "replace").strip().lower()
    return value if HEX40_RE.fullmatch(value) else None


async def run_project_action(project_root: str, project_id: str, action: dict[str, Any], *,
                             config_path: Path | None = None, receipt_surface: str = "typed") -> dict[str, Any]:
    root = Path(project_root).resolve(strict=True)
    started = time.monotonic()
    head_before = await _action_receipt_head(root)
    incident_scope = incident_project_scope(project_id, str(root))
    incident_id = active_incident_id(incident_scope)
    receipt = {
        "project": project_id,
        "cwd": str(root),
        "surface": receipt_surface,
        "plane": "project_action",
        "operation": action.get("kind"),
        "head": head_before,
        "expected_head": action.get("expected_head"),
        "expected_workspace_digest": action.get("expected_workspace_digest"),
        "incident_id": incident_id,
    }
    try:
        result = await _run_project_action_inner(project_root, project_id, action, config_path=config_path)
    except ProbeError as exc:
        _append_direct_receipt({
            **receipt, "status": exc.status, "error_category": exc.category,
            "duration_ms": int((time.monotonic() - started) * 1000),
        })
        raise
    except Exception:
        _append_direct_receipt({
            **receipt, "status": "error", "error_category": "internal",
            "duration_ms": int((time.monotonic() - started) * 1000),
        })
        raise
    _append_direct_receipt({
        **receipt,
        "status": str(result.get("status") or "ok") if isinstance(result, dict) else "ok",
        "duration_ms": int((time.monotonic() - started) * 1000),
    })
    return result


async def _restart_service(binding: dict[str, Any], service_alias: str) -> dict[str, Any]:
    alias = _validate_alias(service_alias)
    cfg = _host_section(binding, "services").get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "service is not registered", status="denied")
    unit = cfg.get("unit")
    scope = cfg.get("scope", "user")
    if scope != "user" or not isinstance(unit, str) or re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", unit) is None:
        raise ProbeError("target_denied", "only registered user services may be restarted directly", status="denied")
    token = hashlib.sha256(unit.encode("utf-8")).hexdigest()[:12]
    transient = f"devcoveer-direct-restart-{token}"
    argv = [
        "/usr/bin/systemd-run", "--user", "--quiet", "--collect",
        f"--unit={transient}", "--on-active=1s",
        "/usr/bin/systemctl", "--user", "restart", unit,
    ]
    result = await _run_process(argv, env=_systemd_env("user"), timeout=15, output_limit=16 * 1024)
    if result.exit_code != 0:
        err, _ = _decode(result, "stderr")
        raise ProbeError("restart_schedule", err[-1200:] or "could not schedule service restart", exit_code=result.exit_code)
    return {"status": "ok", "action": "restart_service", "service": alias,
            "unit": unit, "scheduled_after_seconds": 1, "transient_unit": transient}


async def _run_host_action_inner(project_root: str, project_id: str, action: dict[str, Any], *,
                                 config_path: Path | None = None) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=True)
    binding_id, handoff, _meta = _handoff_binding(root, project_id)
    if handoff:
        raise ProbeError("handoff_denied", "host actions are unavailable for handoff projects", status="denied")
    config = _load_config(config_path or DEFAULT_CONFIG_PATH)
    binding_id, binding, _binding_inherited = await _binding_for_root(config, binding_id, root)
    kind = action["kind"]
    if kind == "restart_service":
        return await _restart_service(binding, action["service"])
    if kind == "run_profile":
        return await _run_profile(root, binding, action["profile"], section="host_actions", handoff=False)
    if kind == "service_action": return await service_action(binding, action["service"], action["operation"])
    if kind == "write_registered_file": return write_registered_file(binding, action)
    if kind == "delete_registered_file": return delete_registered_file(binding, action)
    if kind == "daemon_reload": return await daemon_reload(binding)
    if kind == "root_git_sync_exact": return await root_git_sync_exact(binding, action)
    raise ProbeError("invalid_input", f"unknown host action kind {kind!r}")


async def run_host_action(project_root: str, project_id: str, action: dict[str, Any], *,
                          config_path: Path | None = None, receipt_surface: str = "typed") -> dict[str, Any]:
    root = Path(project_root).resolve(strict=True)
    started = time.monotonic()
    incident_scope = incident_project_scope(project_id, str(root))
    incident_id = active_incident_id(incident_scope)
    receipt = {
        "project": project_id,
        "cwd": str(root),
        "surface": receipt_surface,
        "plane": "host_action",
        "incident_id": incident_id,
        "operation": action.get("kind"),
    }
    try:
        result = await _run_host_action_inner(project_root, project_id, action, config_path=config_path)
    except ProbeError as exc:
        _append_direct_receipt({
            **receipt, "status": exc.status, "error_category": exc.category,
            "duration_ms": int((time.monotonic() - started) * 1000),
        })
        raise
    except Exception:
        _append_direct_receipt({
            **receipt, "status": "error", "error_category": "internal",
            "duration_ms": int((time.monotonic() - started) * 1000),
        })
        raise
    _append_direct_receipt({
        **receipt,
        "status": str(result.get("status") or "ok") if isinstance(result, dict) else "ok",
        "duration_ms": int((time.monotonic() - started) * 1000),
    })
    return result
