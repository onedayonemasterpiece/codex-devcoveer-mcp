from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/"runtime"
sys.path.insert(0,str(RUNTIME))

import direct_ops_gateway as gateway
import direct_actions as actions
import direct_ops2
import incident_registry


def run(coro): return asyncio.run(coro)


def test_gateway_schema_is_future_operation_stable():
    validator=Draft202012Validator(gateway.DIRECT_OPS_V2_INPUT_SCHEMA)
    payload={
        "project":"p",
        "plane":"project_action",
        "operation":"future_server_operation",
        "payload":{"future":True},
    }
    assert list(validator.iter_errors(payload)) == []
    op_schema=gateway.DIRECT_OPS_V2_INPUT_SCHEMA["properties"]["operation"]
    assert "enum" not in op_schema


def test_gateway_project_probe_validates_and_dispatches(monkeypatch,tmp_path:Path):
    seen={}
    async def fake(root,project,checks,receipt_surface="typed"):
        seen.update(root=root,project=project,checks=checks,receipt_surface=receipt_surface)
        return {"execution":"direct","results":[{"id":"direct_ops_v2","status":"ok"}]}
    monkeypatch.setattr(gateway,"run_project_probe",fake)
    out=run(gateway.run_direct_ops_v2(
        str(tmp_path),"p","project_probe","capabilities",{}
    ))
    assert out["execution"]=="direct"
    assert seen["checks"]==[{"id":"direct_ops_v2","kind":"capabilities"}]
    assert seen["receipt_surface"]=="direct_ops_v2"


def test_gateway_project_action_rejects_arbitrary_command_before_dispatch(monkeypatch,tmp_path:Path):
    called=False
    async def fake(*_args,**_kwargs):
        nonlocal called
        called=True
        return {}
    monkeypatch.setattr(gateway,"run_project_action",fake)
    with pytest.raises(gateway.ProbeError) as exc:
        run(gateway.run_direct_ops_v2(
            str(tmp_path),"p","project_action","write_text",
            {"path":"x.txt","content":"x","command":"rm -rf /"},
        ))
    assert exc.value.category=="invalid_input"
    assert called is False


def test_gateway_rejects_reserved_envelope_override(tmp_path:Path):
    with pytest.raises(gateway.ProbeError):
        run(gateway.run_direct_ops_v2(
            str(tmp_path),"p","project_probe","git_state",{"kind":"other"}
        ))


def test_cached_run_tool_schema_can_carry_direct_ops_v2(monkeypatch,tmp_path:Path):
    cfg=tmp_path/"config.json"
    cfg.write_text(json.dumps({"version":1,"projects":{"p":{}}}),encoding="utf-8")
    action={
        "kind":"run_tool",
        "tool":"python_script",
        "path":"@direct_ops_v2",
        "args":["project_probe","capabilities","{}"],
    }
    validator=Draft202012Validator(actions.PROJECT_ACTION_INPUT_SCHEMA)
    assert list(validator.iter_errors({"project":"p","action":action})) == []

    seen={}
    async def fake(root,project,plane,operation,payload):
        seen.update(root=root,project=project,plane=plane,operation=operation,payload=payload)
        return {"execution":"direct","results":[]}
    monkeypatch.setattr(gateway,"run_direct_ops_v2",fake)
    out=run(actions.run_project_action(str(tmp_path),"p",action,config_path=cfg))
    assert out["status"]=="ok"
    assert out["action"]=="direct_ops_v2_compat"
    assert out["result"]=={"execution":"direct","results":[]}
    assert seen["plane"]=="project_probe"
    assert seen["operation"]=="capabilities"
    assert seen["payload"]=={}


def test_cached_run_tool_compat_rejects_bad_json(tmp_path:Path):
    cfg=tmp_path/"config.json"
    cfg.write_text(json.dumps({"version":1,"projects":{"p":{}}}),encoding="utf-8")
    action={
        "kind":"run_tool","tool":"python_script","path":"@direct_ops_v2",
        "args":["project_probe","capabilities","{"],
    }
    with pytest.raises(actions.ProbeError) as exc:
        run(actions.run_project_action(str(tmp_path),"p",action,config_path=cfg))
    assert exc.value.category=="invalid_input"

def test_cached_run_tool_compat_normalizes_successful_lifecycle_statuses(monkeypatch,tmp_path:Path):
    cfg=tmp_path/"config.json"
    cfg.write_text(json.dumps({"version":1,"projects":{"p":{}}}),encoding="utf-8")

    async def fake(root,project,plane,operation,payload):
        return {"status":payload["status"],"job_id":"job_"+"a"*24}

    monkeypatch.setattr(gateway,"run_direct_ops_v2",fake)
    for inner in ("running","succeeded","cancelled","terminal","scheduled"):
        out=run(actions.run_project_action(str(tmp_path),"p",{
            "kind":"run_tool","tool":"python_script","path":"@direct_ops_v2",
            "args":["project_action","job_status",json.dumps({"status":inner})],
        },config_path=cfg))
        assert out["status"]=="ok"
        assert out["result"]["status"]==inner

    out=run(actions.run_project_action(str(tmp_path),"p",{
        "kind":"run_tool","tool":"python_script","path":"@direct_ops_v2",
        "args":["project_action","job_status",json.dumps({"status":"failed"})],
    },config_path=cfg))
    assert out["status"]=="failed"
    assert out["result"]["status"]=="failed"

def test_cached_run_tool_compat_preserves_handoff_project_id(monkeypatch,tmp_path:Path):
    cfg=tmp_path/"config.json"
    cfg.write_text(json.dumps({"version":1,"projects":{"p":{}}}),encoding="utf-8")
    handoff_id="src_"+"a"*24
    project_id="handoff:"+handoff_id
    seen={}

    def fake_binding(root,raw_project_id):
        assert raw_project_id==project_id
        return "p",True,{"id":handoff_id}

    async def fake_compat(root,raw_project_id,action):
        seen["project_id"]=raw_project_id
        return {"status":"ok","action":"direct_ops_v2_compat","result":{"status":"ok"}}

    monkeypatch.setattr(actions,"_handoff_binding",fake_binding)
    monkeypatch.setattr(actions,"_run_direct_ops_compat",fake_compat)
    out=run(actions.run_project_action(str(tmp_path),project_id,{
        "kind":"run_tool","tool":"python_script","path":"@direct_ops_v2",
        "args":["project_action","write_text","{}"],
    },config_path=cfg))
    assert out["status"]=="ok"
    assert seen["project_id"]==project_id


def test_recent_direct_ops_is_discoverable():
    described = gateway.describe_plane("project_probe", "recent_direct_ops")
    schema = described["payload_schema"]
    assert schema["properties"]["limit"]["maximum"] == 100
    assert "expected_workspace_digest" in described["operator_guidance"]["workspace"]


def test_capabilities_workspace_guidance_mentions_workspace_digest(monkeypatch, tmp_path):
    async def remotes(_root):
        return {}
    monkeypatch.setattr(direct_ops2, "discover_github_remotes", remotes)
    caps = run(direct_ops2.project_capabilities(tmp_path, {}))
    assert "expected_head" in caps["operator_help"]["workspace"]
    assert "expected_workspace_digest" in caps["operator_help"]["workspace"]


def test_liberal_direct_ops_add_capabilities_without_removing_escape_hatch():
    described = gateway.describe_plane("project_action")
    operations = set(described["operations"])
    assert {"git_switch_ff", "apply_source_patch", "artifact_fetch", "requirements_update"} <= operations
    assert {"run_tool", "run_profile", "write_text", "job_start", "git_fetch"} <= operations
    probe_operations = set(gateway.describe_plane("project_probe")["operations"])
    assert "requirements_context" in probe_operations
    run_tool = gateway.describe_plane("project_action", "run_tool")["payload_schema"]
    assert "bash_script" in run_tool["properties"]["tool"]["enum"]
    guidance = described["operator_guidance"]
    assert "not a capability boundary" in guidance["escape_hatch"]
    assert "arbitrary HTTPS" in guidance["artifacts"]
    assert "inert source" in guidance["source"]
    assert "requirements_update" in guidance["requirements"]
    assert "semantic typed operation" in guidance["efficiency"]


def _allow_incident_lookup(monkeypatch, incident_id):
    monkeypatch.setattr(
        incident_registry, "incident_project_scope",
        lambda _project, _root: "test-scope",
    )
    monkeypatch.setattr(
        incident_registry, "get_incident",
        lambda _scope, requested: {"incident_id": requested}
        if requested == incident_id else None,
    )


def test_native_incident_note_preserves_intrinsic_incident_id(monkeypatch,tmp_path:Path):
    incident_id="inc_"+"a"*24
    _allow_incident_lookup(monkeypatch,incident_id)
    seen={}
    async def fake(root,project,action,receipt_surface="typed"):
        seen.update(root=root,project=project,action=action,receipt_surface=receipt_surface)
        return {"status":"ok","incident":{"incident_id":action["incident_id"]}}
    monkeypatch.setattr(gateway,"run_project_action",fake)
    out=run(gateway.run_direct_ops_v2(
        str(tmp_path),"p","project_action","incident_note",
        {"incident_id":incident_id,"note":"verified","note_kind":"verification"},
    ))
    assert out["status"]=="ok"
    assert seen["action"]["incident_id"]==incident_id
    assert seen["action"]["kind"]=="incident_note"
    assert seen["receipt_surface"]=="direct_ops_v2"


def test_native_incident_get_preserves_intrinsic_incident_id(monkeypatch,tmp_path:Path):
    incident_id="inc_"+"b"*24
    _allow_incident_lookup(monkeypatch,incident_id)
    seen={}
    async def fake(root,project,checks,receipt_surface="typed"):
        seen.update(root=root,project=project,checks=checks,receipt_surface=receipt_surface)
        return {"execution":"direct","results":[{"id":"direct_ops_v2","status":"ok"}]}
    monkeypatch.setattr(gateway,"run_project_probe",fake)
    out=run(gateway.run_direct_ops_v2(
        str(tmp_path),"p","project_probe","incident_get",
        {"incident_id":incident_id},
    ))
    assert out["execution"]=="direct"
    assert seen["checks"][0]["incident_id"]==incident_id
    assert seen["checks"][0]["kind"]=="incident_get"
    assert seen["receipt_surface"]=="direct_ops_v2"


def test_context_incident_id_is_stripped_from_nonincident_native_action(monkeypatch,tmp_path:Path):
    incident_id="inc_"+"c"*24
    _allow_incident_lookup(monkeypatch,incident_id)
    seen={}
    async def fake(root,project,action,receipt_surface="typed"):
        seen.update(action=action,receipt_surface=receipt_surface)
        return {"status":"ok"}
    monkeypatch.setattr(gateway,"run_project_action",fake)
    out=run(gateway.run_direct_ops_v2(
        str(tmp_path),"p","project_action","write_text",
        {"path":"x.txt","content":"x","incident_id":incident_id},
    ))
    assert out["status"]=="ok"
    assert "incident_id" not in seen["action"]
    assert seen["action"]=={"kind":"write_text","path":"x.txt","content":"x"}
