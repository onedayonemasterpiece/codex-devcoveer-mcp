"""Install native Codex deep-link support onto the generated MCP bridge.

The pinned historical 3.6.1 snapshot remains unchanged. All modifications to
the effective bridge are made during the canonical reproducible build.
"""
from __future__ import annotations


def transform(source: str, replace_once) -> str:
    source = replace_once(
        source,
        '\nBRIDGE_VERSION = "3.38.0-safety-friction-v1"',
        '\nBRIDGE_VERSION = "3.39.0-native-thread-links"',
        "native deep-link version",
    )
    source = replace_once(
        source,
        '\nBRIDGE_VERSION = "3.39.0-native-thread-links"',
        '\nfrom native_thread_links import (\n'
        '    NativeThreadError as _NativeThreadError,\n'
        '    is_thread_link as _is_native_thread_link,\n'
        '    normalize_thread_reference as _normalize_native_thread_reference,\n'
        '    read_native_thread as _read_native_thread,\n'
        ')\n\nBRIDGE_VERSION = "3.39.0-native-thread-links"',
        "native deep-link import",
    )
    source = replace_once(
        source, '\nTOOLS = [\n',
        '''
# Cursors are indexes of redacted, visible native events.  Each item is
# chunked into 10K-character parts: no invisible truncation of a large output.
READ_TASK_INPUT_SCHEMA["properties"]["cursor"] = {
    "type": "integer", "minimum": 0, "default": 0,
    "description": "Native codex:// thread pagination cursor (detail=full).",
}
READ_TASK_INPUT_SCHEMA["properties"]["limit"] = {
    "type": "integer", "minimum": 1, "maximum": 100, "default": 40,
}
READ_TASK_INPUT_SCHEMA["properties"]["include_tools"] = {
    "type": "boolean",
    "description": "Include native tool calls and outputs with detail=full.",
}
READ_NATIVE_THREAD_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thread": {"type": "string", "minLength": 1, "maxLength": 200,
            "description": "Exact Codex Native UUID or codex://threads/<UUID>."},
        "project": {"type": "string", "minLength": 1, "maxLength": 128},
        "detail": {"type": "string", "enum": ["summary", "full"], "default": "full"},
        "cursor": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 40},
        "include_tools": {"type": "boolean",
            "description": "Include tool calls and outputs; reasoning and private metadata never leave the server."},
    },
    "required": ["thread"], "additionalProperties": False,
}

TOOLS = [
''',
        "native deep-link tool schema",
    )
    source = replace_once(
        source,
        '    types.Tool(name="continue_task", title="Continue Codex task",',
        '''    types.Tool(name="read_native_thread", title="Read Codex Native thread",
        description=("Read a native Codex session by exact codex://threads/<UUID> or UUID. "
            "This is read-only, requires no registered DevCoveer task, and reads a verified "
            "local native rollout. detail=full returns secret-redacted user/assistant messages "
            "and optionally tool calls/outputs, in reconstructible chunks. Follow nextCursor "
            "until null. Hidden reasoning and system/developer messages are never exposed. "
            "An active session can grow; snapshotStable reports concurrent writes."),
        input_schema=READ_NATIVE_THREAD_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False)),
    types.Tool(name="continue_task", title="Continue Codex task",''',
        "native deep-link MCP tool",
    )
    source = replace_once(
        source,
        '\n\ndef _invalid_params(field_name: str, message: str = "Invalid tool parameters") -> MCPError:',
        '\nVALIDATORS["read_native_thread"] = Draft202012Validator(READ_NATIVE_THREAD_INPUT_SCHEMA)'
        '\n\ndef _invalid_params(field_name: str, message: str = "Invalid tool parameters") -> MCPError:',
        "native deep-link validator",
    )
    source = replace_once(
        source,
        '        if name == "council_run":\n',
        '''        if name == "read_native_thread" or (
            name == "read_task" and str(arguments.get("task") or "").strip().startswith("codex://")
        ):
            reference = arguments["thread"] if name == "read_native_thread" else arguments["task"]
            detail = arguments.get("detail", "full" if name == "read_native_thread" else "summary")
            try:
                native_result = await anyio.to_thread.run_sync(
                    lambda: _read_native_thread(
                        reference,
                        cursor=arguments.get("cursor", 0),
                        limit=arguments.get("limit", 40),
                        detail=detail,
                        include_tools=arguments.get("include_tools"),
                    )
                )
            except _NativeThreadError as exc:
                if not (name == "read_task" and exc.status == "not_found"
                        and _normalize_native_thread_reference(reference) is not None):
                    return _tool_result({
                        "status": exc.status, "message": str(exc),
                        "threadReference": reference,
                    }, error=True)
                # An MCP-managed thread may live only in the native app-server
                # state DB: preserve existing read_task behavior in that case.
                arguments = dict(arguments)
                arguments["task"] = _normalize_native_thread_reference(reference)
            else:
                if project is not None and native_result["task"].get("cwd") != project.path:
                    return _tool_result({
                        "status": "project_mismatch",
                        "message": "Native thread belongs to another project.",
                    }, error=True)
                return _tool_result(native_result)

        if name == "council_run":
''',
        "read native thread MCP dispatcher",
    )
    source = replace_once(
        source,
        '        registered = _registry_task(arguments["task"], project)\n',
        '''        if name in {"continue_task", "cancel_task"} and _is_native_thread_link(arguments["task"]):
            arguments = dict(arguments)
            arguments["task"] = _normalize_native_thread_reference(arguments["task"])
        registered = _registry_task(arguments["task"], project)
''',
        "normalize native reference for existing guarded write/interrupt operations",
    )
    return source
