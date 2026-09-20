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
import direct_jobs as jobs


def run(coro): return asyncio.run(coro)


def proc(code=0,stdout=b"",stderr=b""):
    return SimpleNamespace(exit_code=code,stdout=stdout,stderr=stderr,truncated=False,duration_ms=1)


def test_job_start_writes_private_spec_and_fixed_systemd_runner(tmp_path: Path,monkeypatch):
    monkeypatch.setattr(jobs,"JOB_ROOT",tmp_path/"jobs")
    seen={}
    async def fake(argv,**kwargs):
        seen["argv"]=argv
        return proc()
    monkeypatch.setattr(jobs,"_run_process",fake)
    out=run(jobs.start_job(tmp_path,{"tool":"python_module","script":"pytest","args":["-q"],"timeout_seconds":30}))
    assert out["status"]=="running"
    spec=jobs._read_json(jobs._job_dir(out["job_id"])/"spec.json")
    assert spec["root"]==str(tmp_path.resolve())
    assert spec["action"]["tool"]=="python_module"
    assert seen["argv"][0:3]==["/usr/bin/systemd-run","--user","--quiet"]
    assert (jobs._job_dir(out["job_id"])/"spec.json").stat().st_mode&0o777==0o600


def test_job_status_prefers_durable_result(tmp_path: Path,monkeypatch):
    monkeypatch.setattr(jobs,"JOB_ROOT",tmp_path/"jobs")
    jid="job_"+"a"*24; d=jobs._job_dir(jid); d.mkdir(parents=True)
    jobs._atomic_json(d/"spec.json",{"jobId":jid,"root":str(tmp_path.resolve()),"unit":"u","action":{}})
    jobs._atomic_json(d/"result.json",{"status":"succeeded","result":{"exit_code":0}})
    out=run(jobs.job_status(tmp_path,jid))
    assert out["status"]=="succeeded" and out["result"]["exit_code"]==0


def test_job_cancel_and_cleanup_are_project_bound(tmp_path: Path,monkeypatch):
    monkeypatch.setattr(jobs,"JOB_ROOT",tmp_path/"jobs")
    jid="job_"+"b"*24; d=jobs._job_dir(jid); d.mkdir(parents=True)
    jobs._atomic_json(d/"spec.json",{"jobId":jid,"root":str(tmp_path.resolve()),"unit":"u","action":{}})
    async def fake(argv,**kwargs): return proc()
    monkeypatch.setattr(jobs,"_run_process",fake)
    assert run(jobs.cancel_job(tmp_path,jid))["status"]=="cancelled"
    assert jobs.cleanup_job(tmp_path,jid)["removed"] is True


def test_job_access_denied_for_other_project(tmp_path: Path,monkeypatch):
    monkeypatch.setattr(jobs,"JOB_ROOT",tmp_path/"jobs")
    jid="job_"+"c"*24; d=jobs._job_dir(jid); d.mkdir(parents=True)
    jobs._atomic_json(d/"spec.json",{"jobId":jid,"root":str((tmp_path/"other").resolve()),"unit":"u","action":{}})
    with pytest.raises(jobs.ProbeError):
        run(jobs.job_status(tmp_path,jid))
