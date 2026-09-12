from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-direct-probes.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("install_direct_probes", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_bridge() -> str:
    return (
        "from source_handoff import (\n"
        "    SourceHandoffError,\n"
        "    cleanup_source_handoff as _cleanup_source_handoff,\n"
        "    resolve_source_handoff as _resolve_source_handoff,\n"
        "    stage_source_handoff as _stage_source_handoff,\n"
        ")\n\n"
        "TOOLS = [\n"
        "    types.Tool(name=\"list_models\", title=\"List models\"),\n"
        "    types.Tool(name=\"stage_source_handoff\", title=\"Stage source handoff\",\n"
        "]\n"
        "VALIDATORS = {\n"
        "    \"start_task\": Draft202012Validator(START_TASK_INPUT_SCHEMA),\n"
        "    \"stage_source_handoff\": Draft202012Validator(STAGE_SOURCE_HANDOFF_INPUT_SCHEMA),\n"
        "}\n\n"
        "async def impl(name, arguments):\n"
        "        project = None\n"
        "        if \"project\" in arguments:\n"
        "            project, project_payload = _project_selection(arguments[\"project\"])\n"
        "            if project is None:\n"
        "                return _tool_result(project_payload, error=True)\n"
        "        if name == \"stage_source_handoff\":\n"
        "            return old_stage()\n"
        "        if name == \"start_task\":\n"
        "            return old_start()\n"
    )


def test_transform_adds_only_direct_probe_surface_and_preserves_existing_paths():
    mod = load_installer()
    out = mod.transform_bridge(synthetic_bridge())
    assert 'name="project_probe"' in out and 'name="host_probe"' in out
    assert 'Draft202012Validator(PROJECT_PROBE_INPUT_SCHEMA)' in out
    assert 'Draft202012Validator(HOST_PROBE_INPUT_SCHEMA)' in out
    assert 'await _run_project_probe(' in out and 'await _run_host_probe(' in out
    assert 'project, project_payload = _project_selection(arguments["project"])' in out
    assert 'if name == "stage_source_handoff":\n            return old_stage()' in out
    assert 'if name == "start_task":\n            return old_start()' in out
    assert out.count('name="project_probe"') == 1 and out.count('name="host_probe"') == 1


def test_transform_fails_closed_on_duplicate_or_missing_anchor():
    mod = load_installer()
    once = mod.transform_bridge(synthetic_bridge())
    try:
        mod.transform_bridge(once)
    except SystemExit as exc:
        assert "already" in str(exc)
    else:
        raise AssertionError("second transform must fail closed")
    try:
        mod.transform_bridge("no anchors here")
    except SystemExit as exc:
        assert "anchor" in str(exc)
    else:
        raise AssertionError("missing anchors must fail closed")


def test_installer_has_exact_lineage_and_never_restarts_services():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'EXPECTED_BRIDGE_SHA256 = "a3ce51d6fc8d5fa892f6f54f34f672f878e25039b6a44a7399ccb1c55a45872d"' in text
    assert 'EXPECTED_SOURCE_HANDOFF_SHA256 = "265af62223aca453644a12945e1395ee66b533ae9af6da51003c613f98f8a585"' in text
    assert "systemctl" not in text and "restart" in text.lower()
    assert "--maintenance-task-id" in text
    assert "maintenance_gate" in text and "live_opencode_session_types" in text


def test_installer_maintenance_gate_excludes_only_verified_self(monkeypatch):
    mod = load_installer()
    self_id = "dvt_" + "1" * 32
    other_id = "dvt_" + "2" * 32
    monkeypatch.setattr(mod, "registry_active_records", lambda: {
        self_id: {"taskId": self_id, "status": "running", "backend": "opencode", "session": "ses_self",
                  "project": "codex-devcoveer-mcp", "title": "DIRECT PROBES DEPLOY maintenance"},
        other_id: {"taskId": other_id, "status": "running", "backend": "opencode", "session": "ses_other",
                   "project": "elsewhere", "title": "other"},
    })
    monkeypatch.setattr(mod, "live_opencode_session_types", lambda: {"ses_self": "busy", "ses_other": "busy"})
    gate = mod.maintenance_gate(self_id)
    assert gate["excluded"] == self_id
    assert gate["active"] == [other_id]


def test_installer_maintenance_gate_fails_closed_when_liveness_unavailable(monkeypatch):
    mod = load_installer()
    task_id = "dvt_" + "3" * 32
    monkeypatch.setattr(mod, "registry_active_records", lambda: {
        task_id: {"taskId": task_id, "status": "running", "backend": "opencode", "session": "ses_x",
                  "project": "codex-devcoveer-mcp", "title": "DIRECT PROBES DEPLOY maintenance"},
    })
    def boom():
        raise RuntimeError("down")
    monkeypatch.setattr(mod, "live_opencode_session_types", boom)
    gate = mod.maintenance_gate(task_id)
    assert gate["active"] == [task_id]
    assert gate["livenessError"] == "session_status_unavailable"
