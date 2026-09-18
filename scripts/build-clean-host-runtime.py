#!/usr/bin/env python3
"""Build canonical runtime from the checked-in 3.6.1 baseline."""
from __future__ import annotations

import argparse
import hashlib
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
    "direct_probes.py", "direct_probe_core.py", "direct_probe_project.py",
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
                          'BRIDGE_VERSION = "3.11.1-liberal-probes-handoff.1"', "version")
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
from source_handoff import (
    SourceHandoffError,
    cleanup_source_handoff as _cleanup_source_handoff,
    resolve_source_handoff as _resolve_source_handoff,
    stage_source_handoff as _stage_source_handoff,
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
    types.Tool(name="stage_source_handoff", title="Stage source handoff",
        description=("Materialize an exact-base Git binary patch in an isolated detached "
            "worktree without modifying the canonical checkout. Returns handoff:<id> for "
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
'''
    source = replace_once(source, tool_anchor, tool_block + tool_anchor, "tool catalog")
    validator_anchor = '    "council_run": Draft202012Validator(COUNCIL_RUN_INPUT_SCHEMA),\n'
    validators = ('    "project_probe": Draft202012Validator(PROJECT_PROBE_INPUT_SCHEMA),\n'
                  '    "host_probe": Draft202012Validator(HOST_PROBE_INPUT_SCHEMA),\n'
                  '    "stage_source_handoff": Draft202012Validator(STAGE_SOURCE_HANDOFF_INPUT_SCHEMA),\n'
                  '    "cleanup_source_handoff": Draft202012Validator(CLEANUP_SOURCE_HANDOFF_INPUT_SCHEMA),\n')
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

'''
    return replace_once(source, handler_anchor, handlers + handler_anchor, "probe dispatch")

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
    for path in out.glob("*.py"):
        py_compile.compile(str(path), doraise=True)
    print(f"bridge_sha256={sha(bridge)}")
    print(f"backend_sha256={sha(out / 'opencode_backend.py')}")
    print("clean_host_build=PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
