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
    async def fake(root,project,checks):
        seen.update(root=root,project=project,checks=checks)
        return {"execution":"direct","results":[{"id":"direct_ops_v2","status":"ok"}]}
    monkeypatch.setattr(gateway,"run_project_probe",fake)
    out=run(gateway.run_direct_ops_v2(
        str(tmp_path),"p","project_probe","capabilities",{}
    ))
    assert out["execution"]=="direct"
    assert seen["checks"]==[{"id":"direct_ops_v2","kind":"capabilities"}]


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
    schema = gateway.describe_plane("project_probe", "recent_direct_ops")["payload_schema"]
    assert schema["properties"]["limit"]["maximum"] == 100


def test_gateway_records_bounded_direct_receipt(monkeypatch, tmp_path):
    seen = []

    async def inner(project_root, project_id, plane, operation, payload):
        return {"status": "ok", "value": 1}

    async def head(_project_root):
        return "a" * 40

    monkeypatch.setattr(gateway, "_run_direct_ops_v2_inner", inner)
    monkeypatch.setattr(gateway, "_receipt_head", head)
    monkeypatch.setattr(gateway, "_append_direct_receipt", seen.append)

    out = run(gateway.run_direct_ops_v2(
        str(tmp_path), "p", "project_probe", "git_state",
        {"expected_head": "b" * 40, "expected_workspace_digest": "c" * 64},
    ))
    assert out["status"] == "ok"
    assert seen and seen[0]["project"] == "p"
    assert seen[0]["operation"] == "git_state"
    assert seen[0]["head"] == "a" * 40
    assert seen[0]["expected_head"] == "b" * 40
    assert seen[0]["expected_workspace_digest"] == "c" * 64
    assert seen[0]["duration_ms"] >= 0
