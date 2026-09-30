from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/"runtime"
sys.path.insert(0,str(RUNTIME))

import direct_actions as actions
import direct_github_actions as gha
import direct_ops2_host as hostops
import direct_probe_host_runtime as hostrt
import direct_probe_host_services as hostservices
import direct_probes as probes
import incident_registry as incidents
import process_recovery as recovery


def run(coro):
    return asyncio.run(coro)


def git(repo: Path,*args: str) -> str:
    env=os.environ.copy()
    env.update({"GIT_AUTHOR_NAME":"T","GIT_AUTHOR_EMAIL":"t@example.invalid",
                "GIT_COMMITTER_NAME":"T","GIT_COMMITTER_EMAIL":"t@example.invalid"})
    return subprocess.check_output(["/usr/bin/git","-C",str(repo),*args],env=env,text=True).strip()


def init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.check_call(["/usr/bin/git","init","-q","-b","main",str(path)])
    git(path,"config","user.name","T")
    git(path,"config","user.email","t@example.invalid")
    (path/"a.txt").write_text("one\n",encoding="utf-8")
    git(path,"add","a.txt")
    git(path,"commit","-q","-m","init")
    return path


def incident_store(monkeypatch,tmp_path):
    monkeypatch.setattr(incidents,"INCIDENT_STATE_PATH",tmp_path/"state"/"incidents.json")
    monkeypatch.setattr(incidents,"INCIDENT_LOCK_PATH",tmp_path/"state"/"incidents.lock")


def test_live_incident_preflight_is_automatic_but_not_for_local_unit_failure(monkeypatch,tmp_path):
    incident_store(monkeypatch,tmp_path)
    repo=init_repo(tmp_path/"repo")
    git(repo,"remote","add","origin","https://github.com/o/r.git")
    assert incidents.looks_like_live_incident(
        "Continue investigation and fix the production live service regression after deployment failure."
    )
    assert not incidents.looks_like_live_incident(
        "Fix a local unit test failure in a new helper; no production impact."
    )
    first=incidents.preflight_incident(
        "worktree-x",str(repo),"Production live service regression",
        "Continue investigation and fix the production live service regression after deployment failure.",
    )
    assert first is not None and first["reused"] is False
    second=incidents.preflight_incident(
        "worktree-x",str(repo),"Production live service regression",
        "Continue investigation and fix the production live service regression after deployment failure.",
    )
    assert second is not None and second["reused"] is True
    assert second["incident"]["incident_id"] == first["incident"]["incident_id"]
    assert second["incident"]["project"] == "o/r"
    assert "not an approval gate" in second["agent_context"]


def test_config_metadata_and_compare_return_no_values_or_hashes(tmp_path):
    env=tmp_path/"service.env"
    env.write_text(
        "URL=https://project-ref.supabase.co/rest/v1?mode=read\n"
        "DSN=postgresql://dbuser@db.example.invalid:6543/postgres?sslmode=require\n"
        "ALIAS_A=opaque-alpha\n"
        "ALIAS_B=opaque-alpha\n"
        "ALIAS_C=opaque-beta\n",
        encoding="utf-8",
    )
    binding={"config_presence":{"cfg":{"path":str(env),"keys":["URL","DSN","ALIAS_A","ALIAS_B","ALIAS_C"]}}}
    meta=run(hostrt._config_metadata(binding,{"source":"cfg","keys":["URL","DSN","ALIAS_A"]}))
    rows={row["key"]:row for row in meta["keys"]}
    assert rows["URL"]["kind"]=="http_url"
    assert rows["URL"]["project_ref"]=="project-ref"
    assert rows["DSN"]["kind"]=="postgres_dsn"
    assert rows["DSN"]["hostname"]=="db.example.invalid"
    assert rows["DSN"]["username"]=="dbuser"
    rendered=json.dumps(meta)
    assert "opaque-alpha" not in rendered and "opaque-beta" not in rendered
    assert meta["values_returned"] is False and meta["hashes_returned"] is False

    compared=run(hostrt._config_compare(binding,{"source":"cfg","keys":["ALIAS_A","ALIAS_B","ALIAS_C"]}))
    assert ["ALIAS_A","ALIAS_B"] in compared["groups"]
    assert compared["all_present_equal"] is False
    assert compared["values_returned"] is False and compared["hashes_returned"] is False
    assert "opaque-alpha" not in json.dumps(compared)


def test_service_config_returns_only_registered_safe_environment(monkeypatch):
    output=(
        "ExecStart={ path=/usr/local/bin/node ; argv[]=/usr/local/bin/node app.mjs ; }\n"
        "WorkingDirectory=/srv/app\n"
        "EnvironmentFiles=/srv/app/runtime.env (ignore_errors=no)\n"
        "Environment=WL_GEMINI_PYTHON=/venv/bin/python WL_GEMINI_ENV_FILE=/srv/app/gemini.env GOOGLE_API_KEY=<redacted>
    )
    async def fake_run(argv,**kwargs):
        return SimpleNamespace(stdout=output.encode(),stderr=b"",exit_code=0,truncated=False,duration_ms=1)
    monkeypatch.setattr(hostservices,"_run_process",fake_run)
    result=run(hostservices._service_config({
        "unit":"fixture.service","scope":"user",
        "safe_environment_keys":["WL_GEMINI_PYTHON","WL_GEMINI_ENV_FILE"],
    }))
    assert result["safe_environment"] == {
        "WL_GEMINI_PYTHON":"/venv/bin/python",
        "WL_GEMINI_ENV_FILE":"/srv/app/gemini.env",
    }
    assert "GOOGLE_API_KEY" not in json.dumps(result)
    with pytest.raises(Exception):
        run(hostservices._service_config({
            "unit":"fixture.service","scope":"user",
            "safe_environment_keys":["GOOGLE_API_KEY"],
        }))


def test_registered_root_git_sync_exact_preserves_untracked(monkeypatch,tmp_path):
    seed=init_repo(tmp_path/"seed")
    bare=tmp_path/"remote.git"
    subprocess.check_call(["/usr/bin/git","init","-q","--bare",str(bare)])
    git(seed,"remote","add","origin",str(bare))
    git(seed,"push","-q","-u","origin","main")
    git(seed,"checkout","-q","-b","release")
    (seed/"a.txt").write_text("release-one\n",encoding="utf-8")
    git(seed,"add","a.txt"); git(seed,"commit","-q","-m","r1")
    git(seed,"push","-q","-u","origin","release")
    git(seed,"checkout","-q","main")

    runtime=tmp_path/"runtime"
    subprocess.check_call(["/usr/bin/git","clone","-q","--branch","main",str(bare),str(runtime)])
    old=git(runtime,"rev-parse","HEAD")
    (runtime/"keep.local").write_text("keep\n",encoding="utf-8")

    git(seed,"checkout","-q","release")
    (seed/"a.txt").write_text("release-two\n",encoding="utf-8")
    git(seed,"add","a.txt"); git(seed,"commit","-q","-m","r2")
    expected=git(seed,"rev-parse","HEAD")
    git(seed,"push","-q","origin","release")

    binding={"github_repo":"o/r","roots":{"runtime":{"path":str(runtime)}}}
    monkeypatch.setattr(hostops,"_registered_repo_from_url",lambda _url:"o/r")
    before=run(hostops.root_git_state(binding,{"root":"runtime"}))
    assert before["head"]==old and before["tracked_clean"] is True
    result=run(hostops.root_git_sync_exact(binding,{
        "root":"runtime","remote":"origin","branch":"release",
        "expected_sha":expected,"expected_head":old,
    }))
    assert result["new_head"]==expected
    assert git(runtime,"rev-parse","HEAD")==expected
    assert (runtime/"keep.local").read_text()=="keep\n"


def test_workflow_dispatch_request_key_never_posts_twice(monkeypatch,tmp_path):
    monkeypatch.setattr(gha,"DISPATCH_STATE_PATH",tmp_path/"state"/"dispatch.json")
    monkeypatch.setattr(gha,"DISPATCH_LOCK_PATH",tmp_path/"state"/"dispatch.lock")
    post_count={"value":0}
    async def fake_repo(root,binding,remote):
        return "origin","o/r"
    async def fake_rows(binding,repo,workflow,ref):
        base=[{"id":10,"status":"completed","conclusion":"success","head_sha":"a"*40,
               "head_branch":ref,"created_at":"2026-09-27T00:00:00Z","html_url":"https://x/10"}]
        if post_count["value"]:
            base.append({"id":11,"status":"queued","conclusion":None,"head_sha":"b"*40,
                         "head_branch":ref,"created_at":"2026-09-27T00:01:00Z","html_url":"https://x/11"})
        return base
    async def fake_gh(binding,args,**kwargs):
        assert "--method" in args and "POST" in args
        post_count["value"]+=1
        return ""
    monkeypatch.setattr(gha,"_repo",fake_repo)
    monkeypatch.setattr(gha,"_workflow_run_rows",fake_rows)
    monkeypatch.setattr(gha,"_gh",fake_gh)

    action={"workflow":"live.yml","ref":"main","inputs":{"mode":"smoke"},
            "request_key":"acceptance:20260927:one"}
    first=run(gha.workflow_dispatch(tmp_path,{},action))
    second=run(gha.workflow_dispatch(tmp_path,{},action))
    assert first["run_id"]==11 and first["reused"] is False
    assert second["run_id"]==11 and second["reused"] is True
    assert post_count["value"]==1
    changed=dict(action); changed["ref"]="other"
    with pytest.raises(Exception):