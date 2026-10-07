from __future__ import annotations
import asyncio, hashlib, json, sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"runtime"))
import direct_actions as actions, direct_github_actions as gha, direct_jobs as jobs, direct_ops2 as ops
import direct_ops_gateway as gateway, direct_probe_core as core, process_recovery as recovery, process_support as support
def run(coro): return asyncio.run(coro)
def test_text_edit_is_atomic_and_exact_count_guarded(tmp_path):
    p=tmp_path/"a.txt"; p.write_text("alpha beta alpha\n",encoding="utf-8"); sha=hashlib.sha256(p.read_bytes()).hexdigest()
    r=ops.text_edit(tmp_path,{"path":"a.txt","expected_sha256":sha,"edits":[{"old":"alpha","new":"A","expected_count":2},{"old":"beta","new":"B","expected_count":1}]})
    assert r["changed"] and p.read_text()=="A B A\n"; before=p.read_bytes()
    with pytest.raises(core.ProbeError,match="expected 2 exact matches"): ops.text_edit(tmp_path,{"path":"a.txt","edits":[{"old":"B","new":"C","expected_count":2}]})
    assert p.read_bytes()==before
def test_new_operations_preserve_legacy_capabilities():
    v={x["properties"]["kind"]["const"]:x for x in actions.PROJECT_ACTION_INPUT_SCHEMA["properties"]["action"]["oneOf"]}
    assert {"text_edit","job_wait","github_run_wait"}<=set(v); assert {"run_tool","write_text","apply_source_patch","capture_handoff","batch"}<=set(v)
    assert "bash_script" in v["run_tool"]["properties"]["tool"]["enum"]
    d=gateway.describe_plane("project_action"); assert d["preferred_tool"]=="direct_ops_v2" and "cache_resistant" in d["operator_guidance"]
    assert {"safety_trace","recovery_snapshot"}<=set(gateway.describe_plane("project_probe")["operations"])
def test_job_wait_returns_on_progress(monkeypatch,tmp_path):
    seq=[{"status":"running","last_output_at":1.0},{"status":"running","last_output_at":2.0}]
    async def fake(*a,**k): return seq.pop(0)
    monkeypatch.setattr(jobs,"job_status",fake); r=run(jobs.job_wait(tmp_path,"job_"+"a"*24,timeout_seconds=2,poll_interval_ms=250))
    assert r["action"]=="job_wait" and r["progress_changed"] and not r["wait_timed_out"]
def test_github_run_wait_is_head_guarded(monkeypatch,tmp_path):
    async def repo(*a): return "origin","o/r"
    seq=[{"status":"queued","conclusion":None,"head_sha":"a"*40,"run_attempt":1},{"status":"in_progress","conclusion":None,"head_sha":"a"*40,"run_attempt":1}]
    async def gh(*a,**k): return json.dumps(seq.pop(0))
    monkeypatch.setattr(gha,"_repo",repo); monkeypatch.setattr(gha,"_gh",gh)
    r=run(gha.workflow_run_wait(tmp_path,{},{"run_id":7,"expected_head_sha":"a"*40,"timeout_seconds":2,"poll_interval_ms":250}))
    assert r["status"]=="in_progress" and r["progress_changed"]
def test_safety_trace_is_advisory(tmp_path,monkeypatch):
    path=tmp_path/"receipts.jsonl"; monkeypatch.setattr(core,"DIRECT_RECEIPT_LOG",path); cwd=str(tmp_path.resolve()); stamp="2026-10-07T15:00:00+00:00"
    for i in range(5): support.append_receipt(path,{"cwd":cwd,"plane":"project_action","operation":"job_status","status":"ok","subject":f"job_{i}","observed_at":stamp},1024*1024)
    for i in range(3): support.append_receipt(path,{"cwd":cwd,"plane":"project_action","operation":"run_tool","status":"denied" if i==0 else "failed","subject":f"scripts/.tmp_helper_{i}.py","observed_at":stamp},1024*1024)
    monkeypatch.setattr(recovery.time,"time",lambda:1791387002.0); t=recovery.process_read(cwd,"safety_trace",{"limit":50,"window_seconds":7200}); codes={x["code"] for x in t["findings"]}
    assert t["advisory"] and not t["blocking"] and {"polling_burst","helper_script_churn","failure_retry_pressure"}<=codes
def test_recovery_snapshot_never_reexecutes(tmp_path,monkeypatch):
    monkeypatch.setattr(recovery,"DB_PATH",tmp_path/"recovery.sqlite3"); monkeypatch.setattr(jobs,"JOB_ROOT",tmp_path/"jobs")
    w=recovery.work_action(str(tmp_path),"p","work_open",{"goal":"finish","request_key":"snapshot-work","next_step":"read"})["work"]; calls=[]
    async def fail(): calls.append(1); raise RuntimeError("lost")
    async def scenario():
        token=recovery.CONTEXT.set({"work_id":w["work_id"],"request_key":"snapshot-op"})
        try:
            with pytest.raises(RuntimeError): await recovery.recorded_call(str(tmp_path),"p","project_action",{"kind":"write_text"},fail)
        finally: recovery.CONTEXT.reset(token)
    run(scenario()); snap=recovery.process_read(str(tmp_path),"recovery_snapshot",{"work_id":w["work_id"],"limit":10})
    assert calls==[1] and not snap["safe_to_start_new_side_effect"] and snap["uncertain_operations"]
    assert any(x["operation"]=="operation_get" for x in snap["next_actions"])
def test_concise_validation_and_version():
    assert "expected_count" in json.dumps(gateway.describe_plane("project_action","text_edit")["payload_schema"])
    b=(ROOT/"scripts"/"build-clean-host-runtime.py").read_text(); assert "_validation_message(first)" in b and "3.38.0-safety-friction-v1" in b
