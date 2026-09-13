from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-direct-tool-reminder.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("install_direct_tool_reminder", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_bridge(mod) -> str:
    return (
        'TOOLS = [\n'
        '    types.Tool(name="start_task", title="Start DevCoveer task",\n'
        '        description=("Start a task asynchronously. "\n'
        + mod.DESCRIPTION_OLD
        + '        input_schema=START_TASK_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA),\n'
        ']\n\n'
        + mod.TOOL_RESULT_ANCHOR
        + '    return types.CallToolResult()\n\n'
        'async def dispatch(name, arguments):\n'
        '        if name == "start_task":\n'
        '            access = arguments.get("access", "read")\n'
        '            task_record = {"taskId": "dvt_test"}\n'
        '            route = {"reason": "x", "provider": "groq", "selection": "groq/model"}\n'
        '            project = type("P", (), {"canonical": "p", "path": "/tmp/p"})()\n'
        + mod.START_RETURN_OLD
    )


def test_transform_adds_pre_call_and_post_call_reminders():
    mod = load_installer()
    transformed = mod.transform_bridge(synthetic_bridge(mod))
    assert "deterministic Git/host/runtime/model/task-state" in transformed
    assert "_direct_tool_reminder" in transformed
    assert '"directToolsHint"' in transformed
    assert 'payload["content"] += "\\n" + direct_tools_hint' in transformed
    assert "project_probe for Git/read/search/diff/remote/CI facts" in transformed
    assert "host_probe for services/logs/listeners/resources/runtime" in transformed


def test_transform_is_idempotent_after_marker_is_present():
    mod = load_installer()
    once = mod.transform_bridge(synthetic_bridge(mod))
    assert mod.transform_bridge(once) == once


def test_helper_policy_has_strong_and_soft_signals():
    mod = load_installer()
    helper = mod.HELPER_BLOCK
    assert '"git status"' in helper
    assert '"systemctl status"' in helper
    assert '"project_probe"' in helper and '"host_probe"' in helper
    assert '"bash"' in helper and '"shell"' in helper
    assert "soft_hits >= 2" in helper
    assert 'access == "read" and soft_hits >= 1' in helper
