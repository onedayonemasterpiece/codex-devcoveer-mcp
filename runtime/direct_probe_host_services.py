from __future__ import annotations

import asyncio
import os
import re
import stat as statmod
from pathlib import Path
from typing import Any

from direct_probe_core import MAX_LOG_LINES, MAX_TEXT_BYTES, ProbeError, _redact_text, _run_process, _validate_alias

def _systemd_env(scope: str) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/home/dev"),
        "LANG": "C.UTF-8",
    }
    if scope == "user":
        runtime_dir = f"/run/user/{os.getuid()}"
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"
    return env

def _host_section(binding: dict[str, Any], name: str) -> dict[str, Any]:
    section = binding.get(name) or {}
    if not isinstance(section, dict):
        raise ProbeError("config", f"trusted {name} section is invalid")
    return section

async def _service_action(binding: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(action["service"])
    operation = str(action["operation"])
    cfg = _host_section(binding, "services").get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "service is not registered", status="denied")

    unit = cfg.get("unit")
    scope = cfg.get("scope", "user")
    if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", unit):
        raise ProbeError("config", "service binding is invalid")
    if scope != "user":
        raise ProbeError(
            "target_denied",
            "direct host actions are limited to explicitly allowlisted user services",
            status="denied",
        )

    allowed = cfg.get("direct_actions", [])
    if not isinstance(allowed, list) or operation not in allowed:
        raise ProbeError("target_denied", "service action is not allowlisted", status="denied")

    if operation == "start":
        verb = ["start", unit]
    elif operation == "restart":
        verb = ["restart", unit]
    elif operation == "enable_start":
        verb = ["enable", "--now", unit]
    else:
        raise ProbeError("invalid_input", "unsupported direct service action")

    result = await _run_process(
        ["/usr/bin/systemctl", "--user", *verb],
        env=_systemd_env("user"),
        timeout=20.0,
        output_limit=16 * 1024,
    )
    if result.exit_code != 0:
        err = result.stderr.decode("utf-8", "replace")
        raise ProbeError(
            "service_action",
            err.strip() or f"systemctl exited {result.exit_code}",
            exit_code=result.exit_code,
        )

    status = await _systemctl_show(cfg)
    return {
        "service": alias,
        "operation": operation,
        "unit": unit,
        "scope": scope,
        "active_state": status.get("ActiveState"),
        "sub_state": status.get("SubState"),
        "main_pid": int(status.get("MainPID") or 0) or None,
        "complete": True,
    }


async def _systemctl_show(service: dict[str, Any]) -> dict[str, Any]:
    unit = service.get("unit"); scope = service.get("scope", "user")
    if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", unit) or scope not in {"user","system"}:
        raise ProbeError("config", "service binding is invalid")
    argv = ["/usr/bin/systemctl"] + (["--user"] if scope == "user" else []) + ["show", unit, "--no-pager", "--property=LoadState,ActiveState,SubState,MainPID,ExecMainStartTimestamp,NRestarts,FragmentPath,WorkingDirectory"]
    result = await _run_process(argv, env=_systemd_env(scope), timeout=4.0, output_limit=16*1024)
    if result.exit_code != 0:
        err = result.stderr.decode("utf-8","replace")
        raise ProbeError("service", err.strip() or f"systemctl exited {result.exit_code}", exit_code=result.exit_code)
    props: dict[str, Any] = {}
    for line in result.stdout.decode("utf-8","replace").splitlines():
        key,_,value = line.partition("="); props[key]=value
    props["unit"]=unit; props["scope"]=scope
    if service.get("executable"): props["executable"] = service["executable"]
    if service.get("working_directory"): props["configured_working_directory"] = service["working_directory"]
    return props

def _parse_proc_tcp(path: Path) -> list[tuple[str,int,str]]:
    rows=[]
    try: lines=path.read_text().splitlines()[1:]
    except OSError: return rows
    for line in lines:
        fields=line.split()
        if len(fields)<10 or fields[3] != "0A": continue
        local=fields[1]; host_hex,port_hex=local.rsplit(":",1)
        try: port=int(port_hex,16)
        except ValueError: continue
        rows.append((host_hex,port,fields[9]))
    return rows

async def _listeners(service_cfg: dict[str, Any]) -> dict[str, Any]:
    status=await _systemctl_show(service_cfg); pid=int(status.get("MainPID") or 0)
    registered_ports={int(p) for p in service_cfg.get("ports",[]) if isinstance(p,int) and 0<p<65536}
    if not registered_ports:
        return {"unit":status["unit"],"main_pid":pid or None,"registered_ports":[],"listeners":[],"complete":True}
    socket_inodes:set[str]=set()
    if pid>0:
        try:
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                try: target=os.readlink(fd)
                except OSError: continue
                m=re.fullmatch(r"socket:\[(\d+)\]",target)
                if m: socket_inodes.add(m.group(1))
        except OSError: pass
    listeners=[]
    for proc_file in (Path("/proc/net/tcp"),Path("/proc/net/tcp6")):
        for _host,port,inode in _parse_proc_tcp(proc_file):
            if port in registered_ports and (not socket_inodes or inode in socket_inodes):
                listeners.append({"port":port,"pid":pid or None,"protocol":"tcp6" if proc_file.name=="tcp6" else "tcp"})
    return {"unit":status["unit"],"main_pid":pid or None,"registered_ports":sorted(registered_ports),"listeners":listeners,"complete":True}

def _tail_file(path: Path, lines: int, max_bytes: int = MAX_TEXT_BYTES) -> tuple[str,bool]:
    st=path.lstat()
    if not statmod.S_ISREG(st.st_mode) or statmod.S_ISLNK(st.st_mode): raise ProbeError("file_type","operational log must be a regular file",status="denied")
    read_bytes=min(st.st_size,256*1024)
    with path.open("rb") as f:
        if st.st_size>read_bytes: f.seek(st.st_size-read_bytes)
        data=f.read(read_bytes)
    text=data.decode("utf-8","replace"); selected="\n".join(text.splitlines()[-lines:]); encoded=selected.encode("utf-8")
    truncated=st.st_size>read_bytes or len(encoded)>max_bytes
    if len(encoded)>max_bytes: selected=encoded[-max_bytes:].decode("utf-8","ignore")
    return selected,truncated

async def _logs(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    alias=_validate_alias(check["log"]); cfg=_host_section(binding,"logs").get(alias)
    if not isinstance(cfg,dict): raise ProbeError("target_denied","log target is not registered",status="denied")
    lines=min(int(check.get("lines",80)),MAX_LOG_LINES); since_seconds=int(check.get("since_seconds",3600))
    if cfg.get("type")=="journal":
        unit_alias=cfg.get("service"); service_cfg=_host_section(binding,"services").get(unit_alias)
        if not isinstance(service_cfg,dict): raise ProbeError("config","journal log refers to unknown service")
        unit=service_cfg["unit"]; scope=service_cfg.get("scope","user")
        argv=["/usr/bin/journalctl"]+(["--user"] if scope=="user" else [])+["--unit",unit,"--no-pager","--output=short-iso","-n",str(lines),"--since",f"-{since_seconds} seconds"]
        result=await _run_process(argv,env=_systemd_env(scope),timeout=10,output_limit=MAX_TEXT_BYTES)
        if result.exit_code!=0: raise ProbeError("logs",result.stderr.decode("utf-8","replace")[-1000:],exit_code=result.exit_code)
        text=result.stdout.decode("utf-8","replace"); truncated=result.truncated; source=f"journal:{unit}"
    elif cfg.get("type")=="file":
        path=Path(str(cfg.get("path",""))); allowed_roots=[Path(str(v)).resolve() for v in _host_section(binding,"roots").values()]
        if not path.is_absolute(): raise ProbeError("config","operational log path must be absolute")
        resolved=path.resolve(strict=True)
        if not any(resolved==root or root in resolved.parents for root in allowed_roots): raise ProbeError("target_denied","operational log is outside registered host roots",status="denied")
        text,truncated=await asyncio.to_thread(_tail_file,resolved,lines); source=f"file:{alias}"
    else: raise ProbeError("config","unknown log target type")
    text,red=_redact_text(text)
    return {"source":source,"text":text,"complete":not truncated,"truncated":truncated,"redacted":red}
