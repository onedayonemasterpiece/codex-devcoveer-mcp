from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))
import direct_ops2 as ops
import direct_actions as da


def git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME":"T","GIT_AUTHOR_EMAIL":"t@example.invalid",
                "GIT_COMMITTER_NAME":"T","GIT_COMMITTER_EMAIL":"t@example.invalid"})
    return subprocess.check_output(["/usr/bin/git","-C",str(repo),*args],env=env,text=True).strip()


def init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.check_call(["/usr/bin/git","init","-q","-b","main",str(path)])
    git(path,"config","user.name","T"); git(path,"config","user.email","t@example.invalid")
    (path/"a.txt").write_text("one\n",encoding="utf-8")
    git(path,"add","a.txt"); git(path,"commit","-q","-m","base")
    return path


def run(coro): return asyncio.run(coro)


def test_generic_github_remote_does_not_require_registry_entry(tmp_path: Path):
    repo=init_repo(tmp_path/"r"); git(repo,"remote","add","origin","https://github.com/o/r.git")
    actual,url=run(ops.resolve_github_remote(repo,{},"origin"))
    assert actual=="o/r" and url.endswith("/o/r.git")
    with pytest.raises(ops.ProbeError):
        run(ops.resolve_github_remote(repo,{"remotes":{"origin":"other/r"}},"origin"))


def test_workspace_compare_classifies_target_local_and_untracked(tmp_path: Path):
    repo=init_repo(tmp_path/"r"); base=git(repo,"rev-parse","HEAD")
    (repo/"a.txt").write_text("changed\n",encoding="utf-8")
    (repo/"new.txt").write_text("new\n",encoding="utf-8")
    rows={row["path"]:row for row in run(ops.workspace_compare(repo,base))["entries"]}
    assert rows["a.txt"]["classification"]=="modified_local"
    assert rows["new.txt"]["classification"]=="untracked"


def test_direct_file_mutations_are_repo_scoped_and_guarded(tmp_path: Path):
    repo=init_repo(tmp_path/"r"); old=hashlib.sha256((repo/"a.txt").read_bytes()).hexdigest()
    ops.write_text(repo,{"path":"a.txt","content":"two\n","expected_sha256":old})
    with pytest.raises(ops.ProbeError):
        ops.write_text(repo,{"path":"a.txt","content":"bad","expected_sha256":old})
    ops.mkdir(repo,{"path":"dir/sub","parents":True})
    ops.write_text(repo,{"path":"dir/sub/x.txt","content":"x","must_not_exist":True})
    xsha=hashlib.sha256(b"x").hexdigest()
    ops.chmod_executable(repo,{"path":"dir/sub/x.txt","executable":True,"expected_sha256":xsha})
    ops.move_path(repo,{"source":"dir/sub/x.txt","target":"dir/sub/y.txt","expected_sha256":xsha})
    ops.delete_file(repo,{"path":"dir/sub/y.txt","expected_sha256":xsha})
    with pytest.raises(ops.ProbeError):
        ops.write_text(repo,{"path":".git/config","content":"no"})


def test_capture_handoff_collects_tracked_and_untracked_without_mutating_index(tmp_path: Path,monkeypatch):
    repo=init_repo(tmp_path/"r"); base=git(repo,"rev-parse","HEAD")
    (repo/"a.txt").write_text("changed\n",encoding="utf-8")
    (repo/"new.txt").write_text("new\n",encoding="utf-8")
    before=git(repo,"status","--porcelain=v1"); captured={}
    def fake_stage(project_root,project_id,base_sha,patch,patch_sha256):
        captured["patch"]=patch
        return {"handoffId":"src_"+"a"*24,"project":"handoff:src_"+"a"*24,
                "baseSha":base_sha,"patchSha256":patch_sha256,"changedFiles":["a.txt","new.txt"],"reused":False}
    monkeypatch.setattr(ops,"stage_source_handoff",fake_stage)
    out=run(ops.capture_handoff(repo,"p",{"base_sha":base,"include_untracked":True}))
    assert "a.txt" in captured["patch"] and "new.txt" in captured["patch"]
    assert out["capturedUntracked"]==["new.txt"]
    assert git(repo,"status","--porcelain=v1")==before


def test_runner_argv_uses_repo_wrappers_and_named_tasks(tmp_path: Path):
    repo=init_repo(tmp_path/"r")
    (repo/"gradlew").write_text("#!/bin/sh\n",encoding="utf-8")
    assert ops.runner_argv(repo,{"tool":"gradle_task","script":"test","args":[]},repo)[:2]==["/usr/bin/bash",str(repo/"gradlew")]
    (repo/"Makefile").write_text("test:\n\t@true\n",encoding="utf-8")
    assert ops.runner_argv(repo,{"tool":"make_target","script":"test","args":[]},repo)==["/usr/bin/make","test"]
    assert ops.runner_argv(repo,{"tool":"python_module","script":"pytest","args":["-q"]},repo)==["/usr/bin/python3","-m","pytest","-q"]


def test_service_action_is_limited_to_registered_user_service(monkeypatch):
    seen={}
    async def fake_run(argv,**kwargs):
        seen["argv"]=argv
        return SimpleNamespace(stdout=b"",stderr=b"",exit_code=0,truncated=False,duration_ms=1)
    monkeypatch.setattr(ops,"_run_process",fake_run)
    binding={"services":{"app":{"unit":"app.service","scope":"user"}}}
    assert run(ops.service_action(binding,"app","start"))["operation"]=="start"
    assert seen["argv"][-2:]==["start","app.service"]
    with pytest.raises(ops.ProbeError):
        run(ops.service_action({"services":{"app":{"unit":"app.service","scope":"system"}}},"app","start"))


def test_registered_host_file_write_requires_alias_and_sha_guard(tmp_path: Path):
    target=tmp_path/"unit.conf"; target.write_text("old\n",encoding="utf-8")
    old=hashlib.sha256(target.read_bytes()).hexdigest()
    binding={"host_files":{"unit":{"path":str(target),"mode":0o600}}}
    out=ops.write_registered_file(binding,{"file":"unit","content":"new\n","expected_sha256":old})
    assert out["old_sha256"]==old and target.read_text()=="new\n"
    assert (target.stat().st_mode&0o777)==0o600
    with pytest.raises(ops.ProbeError):
        ops.write_registered_file(binding,{"file":"unit","content":"again\n","expected_sha256":old})

def test_direct_ops2_schema_variants_are_unique():
    import direct_actions as da
    import direct_probe_core as core
    project_kinds=[row["properties"]["kind"]["const"] for row in da._PROJECT_ACTION_SCHEMA["oneOf"]]
    host_kinds=[row["properties"]["kind"]["const"] for row in da.HOST_ACTION_INPUT_SCHEMA["properties"]["action"]["oneOf"]]
    probe_kinds=[row["properties"]["kind"]["const"] for row in core.PROJECT_CHECK_SCHEMAS]
    assert len(project_kinds)==len(set(project_kinds))
    assert len(host_kinds)==len(set(host_kinds))
    assert len(probe_kinds)==len(set(probe_kinds))

def test_batch_schema_is_non_recursive_and_bounded():
    import direct_actions as da
    variants=da._PROJECT_ACTION_SCHEMA["oneOf"]
    batch=next(row for row in variants if row["properties"]["kind"].get("const")=="batch")
    step_variants=batch["properties"]["steps"]["items"]["oneOf"]
    step_kinds=[row["properties"]["kind"]["const"] for row in step_variants]
    assert "batch" not in step_kinds
    assert batch["properties"]["steps"]["maxItems"]==20

def test_batch_executes_in_order_and_stops_on_failure(tmp_path: Path):
    repo=init_repo(tmp_path/"batchrepo")
    cfg=tmp_path/"config.json"
    cfg.write_text(json.dumps({"version":1,"projects":{"p":{}}}),encoding="utf-8")
    ok=run(da.run_project_action(str(repo),"p",{
        "kind":"batch",
        "steps":[
            {"kind":"mkdir","path":"out"},
            {"kind":"write_text","path":"out/value.txt","content":"ok\n","must_not_exist":True},
        ],
    },config_path=cfg))
    assert ok["status"]=="ok"
    assert (repo/"out/value.txt").read_text()=="ok\n"

    before=(repo/"a.txt").read_text()
    failed=run(da.run_project_action(str(repo),"p",{
        "kind":"batch",
        "steps":[
            {"kind":"write_text","path":"a.txt","content":"no","expected_sha256":"0"*64},
            {"kind":"write_text","path":"out/should-not-exist.txt","content":"bad","must_not_exist":True},
        ],
    },config_path=cfg))
    assert failed["status"]=="failed" and failed["failed_index"]==0
    assert (repo/"a.txt").read_text()==before
    assert not (repo/"out/should-not-exist.txt").exists()
