from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

from direct_probe_core import ProbeError, _redact_text, _run_process

JOB_ID_RE = re.compile(r"^job_[0-9a-f]{24}$")
JOB_ROOT = Path.home() / ".local" / "share" / "openai-codex-mcp" / "direct-jobs"


def _systemd_env() -> dict[str, str]:
    uid = os.getuid()
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/home/dev"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }


def _job_dir(job_id: str) -> Path:
    if JOB_ID_RE.fullmatch(job_id) is None:
        raise ProbeError("invalid_input", "invalid direct job id")
    return JOB_ROOT / job_id


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".job-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ProbeError("job_not_found", "direct job metadata is missing", status="not_found")
    try:
        data=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError,UnicodeError) as exc:
        raise ProbeError("job_invalid", "direct job metadata is invalid") from exc
    if not isinstance(data,dict):
        raise ProbeError("job_invalid", "direct job metadata is invalid")
    return data


def _owned(root: Path, job_id: str) -> tuple[Path,dict[str,Any]]:
    directory=_job_dir(job_id)
    spec=_read_json(directory/"spec.json")
    if spec.get("jobId")!=job_id or spec.get("root")!=str(root.resolve(strict=True)):
        raise ProbeError("job_denied", "direct job belongs to another project/worktree", status="denied")
    return directory,spec


async def start_job(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    job_id="job_"+secrets.token_hex(12)
    directory=_job_dir(job_id)
    directory.mkdir(parents=True,exist_ok=False,mode=0o700)
    timeout=int(action.get("timeout_seconds",900))
    run_action={
        "kind":"run_tool","tool":action["tool"],
        **({"path":action["path"]} if action.get("path") is not None else {}),
        **({"script":action["script"]} if action.get("script") is not None else {}),
        **({"cwd":action["cwd"]} if action.get("cwd") is not None else {}),
        **({"args":action["args"]} if action.get("args") is not None else {}),
        "timeout_seconds":timeout,
    }
    unit="devcoveer-job-"+job_id.removeprefix("job_")
    spec={"version":1,"jobId":job_id,"root":str(root.resolve(strict=True)),"unit":unit,"action":run_action}
    _atomic_json(directory/"spec.json",spec)
    runner=Path(__file__).with_name("direct_job_runner.py").resolve(strict=True)
    argv=[
        "/usr/bin/systemd-run","--user","--quiet","--collect",f"--unit={unit}",
        f"--property=RuntimeMaxSec={timeout+60}s",
        sys.executable,str(runner),str(directory/"spec.json"),
    ]
    result=await _run_process(argv,env=_systemd_env(),timeout=15,output_limit=16*1024)
    if result.exit_code!=0:
        err,_=_redact_text(result.stderr.decode("utf-8","replace"))
        import shutil; shutil.rmtree(directory,ignore_errors=True)
        raise ProbeError("job_start",err[-1200:] or "could not start direct job",exit_code=result.exit_code)
    return {"status":"running","action":"job_start","job_id":job_id,"unit":unit,"timeout_seconds":timeout}


async def job_status(root: Path, job_id: str) -> dict[str, Any]:
    directory,spec=_owned(root,job_id)
    result_path=directory/"result.json"
    if result_path.is_file():
        payload=_read_json(result_path)
        return {"action":"job_status","job_id":job_id,**payload}
    if (directory/"cancelled").is_file():
        return {"action":"job_status","job_id":job_id,"status":"cancelled"}
    result=await _run_process(
        ["/usr/bin/systemctl","--user","show",spec["unit"],
         "--property=ActiveState,SubState,Result,ExecMainStatus","--no-pager"],
        env=_systemd_env(),timeout=10,output_limit=8192,
    )
    props={}
    if result.exit_code==0:
        for line in result.stdout.decode("utf-8","replace").splitlines():
            key,sep,value=line.partition("=")
            if sep: props[key]=value
    active=props.get("ActiveState")
    if active in {"active","activating","reloading"}:
        status="running"
    elif active in {"failed"}:
        status="failed"
    else:
        status="pending" if result.exit_code==0 else "unknown"
    return {"action":"job_status","job_id":job_id,"status":status,"unit_state":props}


async def cancel_job(root: Path, job_id: str) -> dict[str, Any]:
    directory,spec=_owned(root,job_id)
    if (directory/"result.json").is_file():
        return {"status":"terminal","action":"job_cancel","job_id":job_id}
    result=await _run_process(
        ["/usr/bin/systemctl","--user","stop",spec["unit"]],
        env=_systemd_env(),timeout=15,output_limit=8192,
    )
    if result.exit_code!=0:
        err,_=_redact_text(result.stderr.decode("utf-8","replace"))
        raise ProbeError("job_cancel",err[-1200:] or "could not cancel direct job",exit_code=result.exit_code)
    (directory/"cancelled").write_text("cancelled\n",encoding="utf-8")
    return {"status":"cancelled","action":"job_cancel","job_id":job_id}


def cleanup_job(root: Path, job_id: str) -> dict[str, Any]:
    import shutil
    directory,_spec=_owned(root,job_id)
    if not (directory/"result.json").is_file() and not (directory/"cancelled").is_file():
        raise ProbeError("job_active","active direct job cannot be cleaned",status="denied")
    shutil.rmtree(directory)
    return {"status":"ok","action":"job_cleanup","job_id":job_id,"removed":True}
