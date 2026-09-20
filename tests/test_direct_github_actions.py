from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/"runtime"
sys.path.insert(0,str(RUNTIME))
import direct_github_actions as gha


def run(coro): return asyncio.run(coro)


def proc(stdout=b"",stderr=b"",code=0):
    return SimpleNamespace(stdout=stdout,stderr=stderr,exit_code=code,truncated=False,duration_ms=1)


def test_pr_create_reuses_exact_open_chatgpt_pr(tmp_path:Path,monkeypatch):
    async def remote(root,binding,alias): return ("o/r","https://github.com/o/r.git")
    async def sha(root,remote,branch): return "a"*40
    async def fake_gh(binding,args,**kwargs):
        if args[0:2]==["pr","list"]:
            return json.dumps([{"number":7,"url":"https://github.com/o/r/pull/7","headRefOid":"a"*40}])
        raise AssertionError(args)
    monkeypatch.setattr(gha,"resolve_github_remote",remote)
    monkeypatch.setattr(gha,"_remote_sha",sha)
    monkeypatch.setattr(gha,"_gh",fake_gh)
    out=run(gha.pr_create(tmp_path,{},{
        "head":"chatgpt/x","base":"main","expected_head_sha":"a"*40,"title":"T","body":"B"
    }))
    assert out["reused"] is True and out["number"]==7


def test_pr_merge_requires_exact_chatgpt_head_and_base(tmp_path:Path,monkeypatch):
    async def remote(root,binding,alias): return ("o/r","u")
    calls=[]
    async def fake_view(binding,repo,pr):
        if calls:
            return {"state":"MERGED","headRefName":"chatgpt/x","headRefOid":"b"*40,
                    "baseRefName":"main","url":"u","mergedAt":"now","mergeCommit":{"oid":"c"*40}}
        return {"state":"OPEN","headRefName":"chatgpt/x","headRefOid":"b"*40,
                "baseRefName":"main","url":"u"}
    async def fake_gh(binding,args,**kwargs):
        calls.append(args); return ""
    monkeypatch.setattr(gha,"resolve_github_remote",remote)
    monkeypatch.setattr(gha,"_view_pr",fake_view)
    monkeypatch.setattr(gha,"_gh",fake_gh)
    out=run(gha.pr_merge(tmp_path,{},{
        "pr":8,"expected_head_sha":"b"*40,"expected_base":"main","method":"merge"
    }))
    assert out["merge_sha"]=="c"*40
    assert "--merge" in calls[0]


def test_pr_merge_rejects_non_chatgpt_branch(tmp_path:Path,monkeypatch):
    async def remote(root,binding,alias): return ("o/r","u")
    async def fake_view(binding,repo,pr):
        return {"state":"OPEN","headRefName":"feature/x","headRefOid":"d"*40,"baseRefName":"main"}
    monkeypatch.setattr(gha,"resolve_github_remote",remote)
    monkeypatch.setattr(gha,"_view_pr",fake_view)
    with pytest.raises(gha.ProbeError) as exc:
        run(gha.pr_merge(tmp_path,{},{"pr":1,"expected_head_sha":"d"*40}))
    assert exc.value.category=="pr_denied"


def test_delete_remote_branch_is_sha_guarded_and_chatgpt_only(tmp_path:Path,monkeypatch):
    async def remote(root,binding,alias): return ("o/r","u")
    async def sha(root,remote,branch): return "e"*40
    async def fake_git(root,*args,**kwargs): return proc()
    monkeypatch.setattr(gha,"resolve_github_remote",remote)
    monkeypatch.setattr(gha,"_remote_sha",sha)
    monkeypatch.setattr(gha,"_git",fake_git)
    out=run(gha.delete_remote_branch(tmp_path,{},{
        "branch":"chatgpt/x","expected_sha":"e"*40
    }))
    assert out["deleted_sha"]=="e"*40
    with pytest.raises(gha.ProbeError):
        run(gha.delete_remote_branch(tmp_path,{},{"branch":"main","expected_sha":"e"*40}))

def test_explicit_remote_registry_blocks_unregistered_alias(tmp_path:Path,monkeypatch):
    called=False
    async def remote(root,binding,alias):
        nonlocal called
        called=True
        return ("o/r","u")
    monkeypatch.setattr(gha,"resolve_github_remote",remote)
    with pytest.raises(gha.ProbeError) as exc:
        run(gha._repo(tmp_path,{"remotes":{"origin":"o/r"}},"other"))
    assert exc.value.category=="remote_denied"
    assert called is False
