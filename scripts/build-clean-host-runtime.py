#!/usr/bin/env python3
"""Build canonical runtime from the checked-in 3.6.1 baseline."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import py_compile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "historical" / "3.6.1" / "source"
BASE_SHA = {
    "codex-chatgpt-bridge.py": "73756c81b852cb183943c880d5918faadeee25ea9b034504a80df9f5f05d2107",
    "opencode_backend.py": "56699a39b2d635cdc397774cc25f122b119e7794599b0d50776c7596e6c9af84",
}
DIRECT_FILES = (
    "direct_probes.py", "direct_actions.py", "direct_probe_core.py", "direct_probe_project.py",
    "direct_probe_project_git.py", "direct_probe_project_files.py",
    "direct_probe_project_github.py", "direct_probe_host.py",
    "direct_probe_host_services.py", "direct_probe_host_runtime.py",
    "direct_probe_disk_forensics.py",
)

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"clean-host bridge anchor {label!r}: expected 1, found {count}")
    return source.replace(old, new, 1)

def transform_bridge(source: str) -> str:
    source = replace_once(source, 'BRIDGE_VERSION = "3.6.1"',
                          'BRIDGE_VERSION = "3.14.0-direct-ops"', "version")
    import_anchor = """from opencode_backend import (
    DEFAULT_OPENCODE_MODEL as BACKEND_DEFAULT_OPENCODE_MODEL,
    OpenCodeBackend,
    OpenCodeError,
    TASK_ID_RE,
    TaskRegistry,
)
"""
    direct_import = import_anchor + """from direct_probes import (
    HOST_PROBE_INPUT_SCHEMA,
    PROJECT_PROBE_INPUT_SCHEMA,
    ProbeError,
    run_host_probe as _run_host_probe,
    run_project_probe as _run_project_probe,
)
from direct_actions import (
    HOST_ACTION_INPUT_SCHEMA,
    PROJECT_ACTION_INPUT_SCHEMA,
    run_host_action as _run_host_action,
    run_project_action as _run_project_action,
)
from source_handoff import (
    SourceHandoffError,
    cleanup_source_handoff as _cleanup_source_handoff,
    resolve_source_handoff as _resolve_source_handoff,
    stage_source_handoff as _stage_source_handoff,
)
from street_story_operator import (
    OperatorError as StreetStoryOperatorError,
    StreetStoryOperator,
)
"""
    source = replace_once(source, import_anchor, direct_import, "direct-probe import")
    schema_anchor = "\nTOOLS = [\n"
    source_handoff_schemas = r'''
STAGE_SOURCE_HANDOFF_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {
            "type": "string", "minLength": 1, "maxLength": 128,
            "description": "Existing DevCoveer project identifier; never an arbitrary filesystem path.",
        },
        "base_sha": {
            "type": "string", "pattern": r"^[0-9a-f]{40}$",
            "description": "Exact 40-hex Git commit used to produce the patch.",
        },
        "patch": {
            "type": "string", "minLength": 1, "maxLength": 16777216,
            "description": "Output equivalent to git diff --cached --binary <base_sha>.",
        },
        "patch_sha256": {
            "type": "string", "pattern": r"^[0-9a-f]{64}$",
            "description": "Exact SHA-256 of the UTF-8 patch bytes.",
        },
    },
    "required": ["project", "base_sha", "patch", "patch_sha256"],
    "additionalProperties": False,
}
CLEANUP_SOURCE_HANDOFF_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handoff_id": {"type": "string", "pattern": r"^src_[0-9a-f]{24}$"},
    },
    "required": ["handoff_id"],
    "additionalProperties": False,
}
_STREET_STORY_OPERATOR: StreetStoryOperator | None = None

def _street_story_operator() -> StreetStoryOperator:
    global _STREET_STORY_OPERATOR
    if _STREET_STORY_OPERATOR is None:
        _STREET_STORY_OPERATOR = StreetStoryOperator()
    return _STREET_STORY_OPERATOR

_STREET_SHA = r"^[0-9a-f]{40}$"
_STREET_HANDOFF = r"^src_[0-9a-f]{24}$"
_STREET_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"

STREET_STORY_MATERIALIZE_RELEASE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "repository": {"type": "string", "const": "onedayonemasterpiece/street-story"},
        "exact_sha": {"type": "string", "pattern": _STREET_SHA},
        "idempotency_key": {"type": "string", "pattern": _STREET_KEY},
    },
    "required": ["repository", "exact_sha", "idempotency_key"],
    "additionalProperties": False,
}
STREET_STORY_DEPLOY_USER_SERVICE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "release_sha": {"type": "string", "pattern": _STREET_SHA},
        "idempotency_key": {"type": "string", "pattern": _STREET_KEY},
    },
    "required": ["release_sha", "idempotency_key"],
    "additionalProperties": False,
}
STREET_STORY_PUBLISH_EDGE_ROUTE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hostname": {"type": "string", "const": "street-story.kenigevents.ru"},
        "loopback_port": {"type": "integer", "const": 8188},
        "idempotency_key": {"type": "string", "pattern": _STREET_KEY},
    },
    "required": ["hostname", "loopback_port", "idempotency_key"],
    "additionalProperties": False,
}
STREET_STORY_SERVICE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["status", "restart"]},
        "service": {"type": "string", "const": "street-story.service"},
    },
    "required": ["action"],
    "additionalProperties": False,
}
STREET_STORY_DEPLOYMENT_RECEIPT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"idempotency_key": {"type": "string", "pattern": _STREET_KEY}},
    "required": ["idempotency_key"],
    "additionalProperties": False,
}

'''
    source = replace_once(
        source, schema_anchor,
        "\n" + source_handoff_schemas.strip() + "\n\nTOOLS = [\n",
        "source-handoff schemas",
    )
    tool_anchor = '    types.Tool(name="council_run", title="Run multi-model council",\n'
    tool_block = '''    types.Tool(name="project_probe", title="Read deterministic project facts",
        description=("Read bounded deterministic project facts without starting an agent task. "
            "Use this for Git state, files, hashes, searches, and narrowly scoped CI status."),
        input_schema=PROJECT_PROBE_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=True)),
    types.Tool(name="host_probe", title="Read deterministic host/runtime facts",
        description=("Read bounded facts from server-registered services, listeners, logs, "
            "health endpoints, runtimes, and installed files without starting an agent task."),
        input_schema=HOST_PROBE_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False)),
    types.Tool(name="project_action", title="Run registered project operation",
        description=("Execute one typed deterministic project operation without an agent: safe "
            "fast-forward sync, a trusted registered test/build/deploy profile, or direct commit/push "
            "of an already staged source handoff. No arbitrary command, cwd, env, or executable is accepted."),
        input_schema=PROJECT_ACTION_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
            idempotent_hint=False, open_world_hint=True)),
    types.Tool(name="host_action", title="Run registered host operation",
        description=("Execute one typed operation against trusted server configuration without an agent. "
            "Supports registered host profiles and deferred restart of registered user services; "
            "no arbitrary shell command, path, environment, or service name is accepted."),
        input_schema=HOST_ACTION_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=True,
            idempotent_hint=False, open_world_hint=False)),
    types.Tool(name="stage_source_handoff", title="Stage source handoff",
        description=("Materialize an exact-base Git binary patch in an isolated detached "
            "worktree without modifying the canonical checkout. Returns handoff:<id> for "
            "direct project_action test/commit/push or, only when real reasoning is needed, "
            "start_task/continue_task. Patch bytes are consumed through stdin and not persisted."),
        input_schema=STAGE_SOURCE_HANDOFF_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False)),
    types.Tool(name="cleanup_source_handoff", title="Cleanup source handoff",
        description=("Remove one managed source-handoff worktree and metadata after its "
            "changes have been accepted/pushed and no continuation is needed."),
        input_schema=CLEANUP_SOURCE_HANDOFF_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=True,
            idempotent_hint=True, open_world_hint=False)),
    types.Tool(name="street_story_materialize_release", title="Materialize Street Story release",
        description=("Materialize only the current exact Street Story PR-branch SHA after "
            "verifying it is still the current remote branch HEAD."),
        input_schema=STREET_STORY_MATERIALIZE_RELEASE_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
            idempotent_hint=True, open_world_hint=True)),
    types.Tool(name="street_story_deploy_user_service", title="Deploy Street Story user service",
        description=("Deploy only the verified Street Story release to the fixed loopback "
            "street-story.service using current DevCoveer provider and VibePublish infrastructure."),
        input_schema=STREET_STORY_DEPLOY_USER_SERVICE_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
            idempotent_hint=True, open_world_hint=True)),
    types.Tool(name="street_story_publish_edge_route", title="Publish Street Story edge route",
        description=("Publish only street-story.kenigevents.ru to fixed loopback port 8188 through "
            "the existing DevCoveer VPN/DNS/TLS renderer after exact-SHA local health passes."),
        input_schema=STREET_STORY_PUBLISH_EDGE_ROUTE_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
            idempotent_hint=True, open_world_hint=True)),
    types.Tool(name="street_story_service", title="Read or restart Street Story service",
        description="Read status or restart only the allowlisted street-story.service user unit.",
        input_schema=STREET_STORY_SERVICE_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
            idempotent_hint=False, open_world_hint=False)),
    types.Tool(name="street_story_deployment_receipt", title="Read Street Story deployment receipt",
        description="Read one sanitized durable Street Story deployment capability receipt.",
        input_schema=STREET_STORY_DEPLOYMENT_RECEIPT_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False)),

'''
    source = replace_once(source, tool_anchor, tool_block + tool_anchor, "tool catalog")
    validator_anchor = '    "council_run": Draft202012Validator(COUNCIL_RUN_INPUT_SCHEMA),\n'
    validators = ('    "project_probe": Draft202012Validator(PROJECT_PROBE_INPUT_SCHEMA),\n'
                  '    "host_probe": Draft202012Validator(HOST_PROBE_INPUT_SCHEMA),\n'
                  '    "project_action": Draft202012Validator(PROJECT_ACTION_INPUT_SCHEMA),\n'
                  '    "host_action": Draft202012Validator(HOST_ACTION_INPUT_SCHEMA),\n'
                  '    "stage_source_handoff": Draft202012Validator(STAGE_SOURCE_HANDOFF_INPUT_SCHEMA),\n'
                  '    "cleanup_source_handoff": Draft202012Validator(CLEANUP_SOURCE_HANDOFF_INPUT_SCHEMA),\n'
                  '    "street_story_materialize_release": Draft202012Validator(STREET_STORY_MATERIALIZE_RELEASE_INPUT_SCHEMA),\n'
                  '    "street_story_deploy_user_service": Draft202012Validator(STREET_STORY_DEPLOY_USER_SERVICE_INPUT_SCHEMA),\n'
                  '    "street_story_publish_edge_route": Draft202012Validator(STREET_STORY_PUBLISH_EDGE_ROUTE_INPUT_SCHEMA),\n'
                  '    "street_story_service": Draft202012Validator(STREET_STORY_SERVICE_INPUT_SCHEMA),\n'
                  '    "street_story_deployment_receipt": Draft202012Validator(STREET_STORY_DEPLOYMENT_RECEIPT_INPUT_SCHEMA),\n'
)
    source = replace_once(source, validator_anchor, validators + validator_anchor,
                          "validator catalog")
    old_default = '''    if model is None:
        selected_model, selected_effort = await _resolve_model_selection(
            history, None, reasoning_effort,
        )
        return {"backend": "codex", "provider": "codex", "model": selected_model,
                "reasoningEffort": selected_effort, "reason": "default_codex"}
'''
    new_default = '''    if model is None:
        if reasoning_effort is not None:
            raise _invalid_params("reasoning_effort",
                "reasoning_effort requires provider=codex")
        selected = await OPENCODE_BACKEND.resolve_model(
            provider="groq", model="groq/openai/gpt-oss-120b", directory=project.path,
        )
        _assert_model_not_excluded(selected["selection"])
        return {"backend": "opencode", "provider": selected["provider"],
                "model": selected["nativeModelId"], "selection": selected["selection"],
                "reasoningEffort": None, "reason": "default_opencode"}
'''
    source = replace_once(source, old_default, new_default, "providerless routing")
    project_anchor = '''def _project_selection(raw_hint: str) -> tuple[ProjectEntry | None, dict[str, Any]]:
    tier, matches = _match_projects(raw_hint)
'''
    project_with_handoff = '''def _project_selection(raw_hint: str) -> tuple[ProjectEntry | None, dict[str, Any]]:
    if raw_hint.startswith("handoff:"):
        handoff_id = raw_hint.removeprefix("handoff:")
        try:
            workspace = _resolve_source_handoff(handoff_id)
        except SourceHandoffError:
            return None, {"status": "not_found", "matchType": "handoff",
                "message": f"Source handoff {handoff_id!r} is invalid or expired.",
                "candidates": []}
        entry = ProjectEntry(
            canonical=raw_hint,
            directory_name=workspace.name,
            path=str(workspace),
        )
        return entry, {"status": "resolved", "matchType": "handoff",
            "project": raw_hint, "cwd": str(workspace), "candidates": []}
    tier, matches = _match_projects(raw_hint)
'''
    source = replace_once(source, project_anchor, project_with_handoff,
                          "source-handoff project resolver")
    handler_anchor = '        if name == "council_run":\n'
    handlers = '''        if name == "project_probe":
            assert project is not None
            try:
                return _tool_result(await _run_project_probe(
                    project.path, project.canonical, arguments["checks"],
                ))
            except ProbeError as exc:
                payload = {"status": exc.status, "errorCategory": exc.category,
                           "message": str(exc)}
                if exc.exit_code is not None:
                    payload["exitCode"] = exc.exit_code
                return _tool_result(payload, error=True)

        if name == "host_probe":
            assert project is not None
            try:
                return _tool_result(await _run_host_probe(
                    project.path, project.canonical, arguments["checks"],
                ))
            except ProbeError as exc:
                payload = {"status": exc.status, "errorCategory": exc.category,
                           "message": str(exc)}
                if exc.exit_code is not None:
                    payload["exitCode"] = exc.exit_code
                return _tool_result(payload, error=True)

        if name == "project_action":
            assert project is not None
            try:
                result = await _run_project_action(
                    project.path, project.canonical, arguments["action"],
                )
                return _tool_result(result, error=result.get("status") not in {"ok", "scheduled"})
            except ProbeError as exc:
                payload = {"status": exc.status, "errorCategory": exc.category,
                           "message": str(exc)}
                if exc.exit_code is not None:
                    payload["exitCode"] = exc.exit_code
                return _tool_result(payload, error=True)

        if name == "host_action":
            assert project is not None
            try:
                result = await _run_host_action(
                    project.path, project.canonical, arguments["action"],
                )
                return _tool_result(result, error=result.get("status") not in {"ok", "scheduled"})
            except ProbeError as exc:
                payload = {"status": exc.status, "errorCategory": exc.category,
                           "message": str(exc)}
                if exc.exit_code is not None:
                    payload["exitCode"] = exc.exit_code
                return _tool_result(payload, error=True)

        if name == "stage_source_handoff":
            assert project is not None
            try:
                return _tool_result(_stage_source_handoff(
                    project_root=project.path,
                    project_id=project.canonical,
                    base_sha=arguments["base_sha"],
                    patch=arguments["patch"],
                    patch_sha256=arguments["patch_sha256"],
                ))
            except SourceHandoffError as exc:
                return _tool_result({"status": "failed",
                    "errorCategory": "source_handoff", "content": str(exc)}, error=True)

        if name == "cleanup_source_handoff":
            try:
                return _tool_result(_cleanup_source_handoff(arguments["handoff_id"]))
            except SourceHandoffError as exc:
                return _tool_result({"status": "failed",
                    "errorCategory": "source_handoff", "content": str(exc)}, error=True)

        if name == "street_story_materialize_release":
            try:
                return _tool_result(_street_story_operator().materialize_release(
                    repository=arguments["repository"], exact_sha=arguments["exact_sha"],
                    idempotency_key=arguments["idempotency_key"]))
            except StreetStoryOperatorError as exc:
                return _tool_result({"status":"failed","errorCategory":"street_story_operator","content":str(exc)}, error=True)

        if name == "street_story_deploy_user_service":
            try:
                return _tool_result(_street_story_operator().deploy_user_service(
                    release_sha=arguments["release_sha"],
                    idempotency_key=arguments["idempotency_key"]))
            except StreetStoryOperatorError as exc:
                return _tool_result({"status":"failed","errorCategory":"street_story_operator","content":str(exc)}, error=True)

        if name == "street_story_publish_edge_route":
            try:
                return _tool_result(_street_story_operator().publish_edge_route(
                    hostname=arguments["hostname"], loopback_port=arguments["loopback_port"],
                    idempotency_key=arguments["idempotency_key"]))
            except StreetStoryOperatorError as exc:
                return _tool_result({"status":"failed","errorCategory":"street_story_operator","content":str(exc)}, error=True)

        if name == "street_story_service":
            try:
                return _tool_result(_street_story_operator().service(
                    action=arguments["action"], service=arguments.get("service")))
            except StreetStoryOperatorError as exc:
                return _tool_result({"status":"failed","errorCategory":"street_story_operator","content":str(exc)}, error=True)

        if name == "street_story_deployment_receipt":
            try:
                return _tool_result(_street_story_operator().receipt(idempotency_key=arguments["idempotency_key"]))
            except StreetStoryOperatorError as exc:
                return _tool_result({"status":"failed","errorCategory":"street_story_operator","content":str(exc)}, error=True)

'''
    source = replace_once(source, handler_anchor, handlers + handler_anchor, "probe dispatch")
    spec = importlib.util.spec_from_file_location("restore_contract", ROOT / "scripts" / "restore-integration-contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = module.transform(source, replace_once)

    policy_spec = importlib.util.spec_from_file_location(
        "default_opencode_policy", ROOT / "scripts" / "install-default-opencode-policy.py"
    )
    policy = importlib.util.module_from_spec(policy_spec)
    policy_spec.loader.exec_module(policy)
    source = replace_once(
        source, policy.OLD_START_TOOL, policy.NEW_START_TOOL, "start_task operating policy"
    )

    reminder_spec = importlib.util.spec_from_file_location(
        "direct_tool_reminder", ROOT / "scripts" / "install-direct-tool-reminder.py"
    )
    reminder = importlib.util.module_from_spec(reminder_spec)
    reminder_spec.loader.exec_module(reminder)
    source = replace_once(
        source,
        reminder.TOOL_RESULT_ANCHOR,
        reminder.HELPER_BLOCK + reminder.TOOL_RESULT_ANCHOR,
        "direct/browser reminder helper",
    )
    source = replace_once(
        source,
        reminder.START_RETURN_OLD,
        reminder.START_RETURN_NEW,
        "direct/browser reminder response",
    )
    return source

def transform_backend(source: str) -> str:
    source = replace_once(source, '''        self.base_url = base_url
''', '''        self.base_url = base_url
        self._server_version: str | None = None
''', "backend version cache")

    source = replace_once(source, '''        request = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": _read_auth(),
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
        })
''', '''        headers = {
            "Authorization": _read_auth(),
            "Accept": "application/json",
            # Match the supported OpenCode SDK/CLI request contract. The
            # version is learned from /global/health, so upgrades do not need
            # a DevCoveer source change.
            "User-Agent": "opencode/" + (self._server_version or "unknown"),
            **({"Content-Type": "application/json"} if body is not None else {}),
        }
        if directory:
            headers["x-opencode-directory"] = urllib.parse.quote(directory, safe="")
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
''', "official request headers")

    source = replace_once(source, '''                if not raw: return None
                return json.loads(raw)
''', '''                if not raw: return None
                value = json.loads(raw)
                if path == "/global/health" and isinstance(value, dict):
                    version = value.get("version")
                    if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", version):
                        self._server_version = version
                return value
''', "server version discovery")

    source = replace_once(source, '''    @staticmethod
    def permissions(access: str) -> list[dict[str, str]]:
        rules = [{"permission": "*", "pattern": "*", "action": "deny"}]
        for permission in ("read", "glob", "grep", "list", "lsp", "websearch", "webfetch"):
            rules.append({"permission": permission, "pattern": "*", "action": "allow"})
        # OpenCode's own defaults protect dotenv, but make it explicit in the
        # session-level policy. Last match wins.
        rules.extend([
            {"permission": "read", "pattern": "*.env", "action": "deny"},
            {"permission": "read", "pattern": "*.env.*", "action": "deny"},
            {"permission": "read", "pattern": ".env", "action": "deny"},
        ])
        if access == "write":
            rules.append({"permission": "edit", "pattern": "*", "action": "allow"})
        return rules
''', '''    @staticmethod
    def permissions() -> list[dict[str, str]]:
        # Exact session policy emitted by the official OpenCode CLI. Tool
        # safety belongs to its built-in agent; replacing this policy or the
        # tools payload makes Zen reject the caller as a non-OpenCode client.
        return [
            {"permission": "question", "pattern": "*", "action": "deny"},
            {"permission": "plan_enter", "pattern": "*", "action": "deny"},
            {"permission": "plan_exit", "pattern": "*", "action": "deny"},
        ]
''', "official session permissions")

    source = replace_once(source, '''        session = await self.request("POST", "/session", directory=directory, payload={
            "title": title[:200],
            "model": {"id": model_id, "providerID": provider},
            "metadata": {"devcoveer": True, "project": project, "access": access},
            "permission": self.permissions(access),
        })
''', '''        # Prime version and directory configuration exactly as the official
        # CLI/SDK does before creating a session.
        await self.health()
        await self.request("GET", "/config", directory=directory, timeout=15)
        session = await self.request("POST", "/session", directory=directory, payload={
            "permission": self.permissions(),
        })
''', "official session creation")

    source = replace_once(source, '''        tools = {"bash": False, "task": False, "webfetch": True, "websearch": True}
        if access == "read":
            tools.update({
                "edit": False,
                "write": False,
                "patch": False,
                "apply_patch": False,
                "todowrite": False,
                "todoread": False,
            })
        await self.request("POST", f"/session/{session_id}/prompt_async", directory=directory,
                           payload={
                               "model": {"providerID": provider, "modelID": model_id},
                               "agent": "build",
                               "tools": tools,
                               "parts": [{"type": "text", "text": boundary + prompt}],
                           }, timeout=30)
''', '''        await self.request("POST", f"/session/{session_id}/prompt_async", directory=directory,
                           payload={
                               "model": {"providerID": provider, "modelID": model_id},
                               # plan denies edits; build is the official agent
                               # for explicitly writable operational tasks.
                               "agent": "plan" if access == "read" else "build",
                               "parts": [{"type": "text", "text": boundary + prompt}],
                           }, timeout=30)
''', "official prompt payload")
    return source

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    for name, expected in BASE_SHA.items():
        observed = sha(BASE / name)
        if observed != expected:
            raise SystemExit(f"baseline SHA mismatch for {name}: {observed}")
    shutil.copytree(BASE, out, dirs_exist_ok=True)
    bridge = out / "codex-chatgpt-bridge.py"
    bridge.write_text(transform_bridge(bridge.read_text(encoding="utf-8")), encoding="utf-8")
    bridge.chmod(0o755)
    backend = out / "opencode_backend.py"
    backend.write_text(transform_backend(backend.read_text(encoding="utf-8")), encoding="utf-8")
    for name in DIRECT_FILES:
        shutil.copy2(ROOT / "runtime" / name, out / name)
    shutil.copy2(ROOT / "runtime" / "direct_probe_config.json",
                 out / "direct_probe_config.json")
    shutil.copy2(ROOT / "runtime" / "source_handoff.py",
                 out / "source_handoff.py")
    shutil.copy2(ROOT / "runtime" / "street_story_operator.py",
                 out / "street_story_operator.py")
    for name in ("session_evidence.py", "opencode_routing.py", "model_health.py"):
        shutil.copy2(ROOT / "runtime" / name, out / name)
    for path in out.glob("*.py"):
        py_compile.compile(str(path), doraise=True)
    print(f"bridge_sha256={sha(bridge)}")
    print(f"backend_sha256={sha(out / 'opencode_backend.py')}")
    print("clean_host_build=PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
