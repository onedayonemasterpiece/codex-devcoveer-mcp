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

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/"runtime"
sys.path.insert(0,str(RUNTIME))
import direct_ops2_worktrees as wt


def git(repo: Path,*args: str,ok=True) -> str:
    env=os.environ.copy()
    env.update({"GIT_AUTHOR_NAME":"T","GIT_AUTHOR_EMAIL":"t@example.invalid",
                "GIT_COMMITTER_NAME":"T","GIT_COMMITTER_EMAIL":"t@example.invalid"})
    p=subprocess.run(["/usr/bin/git","-C",str(repo),*args],env=env,text=True,
                     stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if ok and p.returncode: raise RuntimeError(p.stderr)
    return p.stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.check_call(["/usr/bin/git","init","-q","-b","main",str(path)])
    git(path,"config","user.name","T"); git(path,"config","user.email","t@example.invalid")
    (path/"a.txt").write_text("base\n",encoding="utf-8")
    git(path,"add","a.txt"); git(path,"commit","-q","-m","base")
    return path


def run(coro): return asyncio.run(coro)


def detached_worktree(repo: Path,path: Path) -> Path:
    git(repo,"worktree","add","-q","--detach",str(path),"HEAD")
    return path


def test_resolve_repo_worktree_accepts_registered_external_only(tmp_path: Path):
    repo=init_repo(tmp_path/"repo"); runtime=detached_worktree(repo,tmp_path/"runtime")
    assert run(wt.resolve_repo_worktree(repo,str(runtime),external_only=True))==runtime.resolve()
    with pytest.raises(wt.ProbeError):
        run(wt.resolve_repo_worktree(repo,str(repo),external_only=True))
    other=tmp_path/"other"; other.mkdir()
    with pytest.raises(wt.ProbeError):
        run(wt.resolve_repo_worktree(repo,str(other)))


def test_worktree_compare_targets_external_runtime(tmp_path: Path):
    repo=init_repo(tmp_path/"repo"); base=git(repo,"rev-parse","HEAD")
    runtime=detached_worktree(repo,tmp_path/"runtime")
    (runtime/"a.txt").write_text("changed\n",encoding="utf-8")
    out=run(wt.worktree_compare(repo,{"worktree":str(runtime),"target_ref":base}))
    assert out["worktree"]==str(runtime.resolve())
    assert {x["path"]:x["classification"] for x in out["entries"]}["a.txt"]=="modified_local"


def test_capture_receipt_and_guarded_reconcile(tmp_path: Path,monkeypatch):
    repo=init_repo(tmp_path/"repo"); base=git(repo,"rev-parse","HEAD")
    runtime=detached_worktree(repo,tmp_path/"runtime")
    handoff=detached_worktree(repo,tmp_path/"handoff")
    (runtime/"a.txt").write_text("recovery\n",encoding="utf-8")
    (runtime/"new.txt").write_text("new\n",encoding="utf-8")
    (handoff/"a.txt").write_text("recovery\n",encoding="utf-8")
    (handoff/"new.txt").write_text("new\n",encoding="utf-8")
    git(handoff,"add","a.txt","new.txt")
    patch,captured=run(wt._patch_bytes(runtime,base,include_untracked=True,paths=None))
    digest=hashlib.sha256(patch).hexdigest()
    hid="src_"+"a"*24
    receipts=tmp_path/"receipts"; monkeypatch.setattr(wt,"CAPTURE_RECEIPT_ROOT",receipts)
    monkeypatch.setattr(wt,"resolve_source_handoff",lambda _hid: handoff)
    meta={"id":hid,"projectId":"p","projectRoot":str(repo.resolve()),"baseSha":base,
          "patchSha256":digest,"worktree":str(handoff.resolve())}
    (handoff.parent/"meta.json").write_text(json.dumps(meta),encoding="utf-8")
    receipt={"version":1,"handoffId":hid,"projectId":"p","canonicalRoot":str(repo.resolve()),
             "sourceWorktree":str(runtime.resolve()),"baseSha":base,"patchSha256":digest,
             "capturedHead":base,"includeUntracked":True,"paths":None,"fullCapture":True,
             "capturedUntracked":captured,"createdAt":1}
    wt._atomic_json(wt._receipt_path(hid),receipt)
    out=run(wt.reconcile_worktree(repo,"p",{"worktree":str(runtime),"handoff_id":hid}))
    assert out["canonical_checkout_untouched"] is True
    assert git(runtime,"status","--porcelain=v1")==""
    assert (runtime/"a.txt").read_text()=="recovery\n"
    assert (runtime/"new.txt").read_text()=="new\n"
    assert git(repo,"rev-parse","HEAD")==base


def test_guarded_reconcile_rejects_drift(tmp_path: Path,monkeypatch):
    repo=init_repo(tmp_path/"repo"); base=git(repo,"rev-parse","HEAD")
    runtime=detached_worktree(repo,tmp_path/"runtime")
    hid="src_"+"b"*24
    monkeypatch.setattr(wt,"CAPTURE_RECEIPT_ROOT",tmp_path/"receipts")
    receipt={"version":1,"handoffId":hid,"projectId":"p","canonicalRoot":str(repo.resolve()),
             "sourceWorktree":str(runtime.resolve()),"baseSha":base,"patchSha256":"0"*64,
             "capturedHead":base,"includeUntracked":True,"paths":None,"fullCapture":True}
    wt._atomic_json(wt._receipt_path(hid),receipt)
    (runtime/"a.txt").write_text("drift\n",encoding="utf-8")
    with pytest.raises(wt.ProbeError) as exc:
        run(wt.reconcile_worktree(repo,"p",{"worktree":str(runtime),"handoff_id":hid}))
    assert exc.value.category=="workspace_drift"


def test_owned_service_requires_working_directory_in_repo_worktrees(tmp_path: Path,monkeypatch):
    repo=init_repo(tmp_path/"repo"); runtime=detached_worktree(repo,tmp_path/"runtime")
    async def fake_run(argv,**kwargs):
        if "show" in argv:
            body=f"LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=123\nWorkingDirectory={runtime}\nExecMainStartTimestamp=x\n".encode()
            return SimpleNamespace(stdout=body,stderr=b"",exit_code=0,truncated=False,duration_ms=1)
        return SimpleNamespace(stdout=b"",stderr=b"",exit_code=0,truncated=False,duration_ms=1)
    monkeypatch.setattr(wt,"_run_process",fake_run)
    info=run(wt.owned_service_action(repo,{"unit":"wonderful-lections.service","operation":"status"}))
    assert info["ownership_verified"] is True and info["working_directory"]==str(runtime.resolve())
    restarted=run(wt.owned_service_action(repo,{"unit":"wonderful-lections.service","operation":"restart"}))
    assert restarted["scheduled_after_seconds"]==1
