from __future__ import annotations

import asyncio
import os
import re
import stat as statmod
from pathlib import Path
from typing import Any

from direct_probe_core import MAX_LOG_LINES, MAX_TEXT_BYTES, ProbeError, _redact_text, _run_process, _validate_alias

UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
_BASE_SYSTEMD_SHOW_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ExecMainStartTimestamp",
    "NRestarts",
    "FragmentPath",
    "WorkingDirectory",
)
_OPTIONAL_SYSTEMD_SHOW_PROPERTIES = frozenset({
    "ExecStart",
    "ProtectHome",
    "ReadWritePaths",
    "ReadOnlyPaths",
    "InaccessiblePaths",
})

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

async def _systemctl_show(service: dict[str, Any]) -> dict[str, Any]:
    unit = service.get("unit"); scope = service.get("scope", "user")
    if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", unit) or scope not in {"user","system"}:
        raise ProbeError("config", "service binding is invalid")
    extra = service.get("show_properties", [])
    if (not isinstance(extra, list)
            or not all(isinstance(item, str) and item in _OPTIONAL_SYSTEMD_SHOW_PROPERTIES for item in extra)):
        raise ProbeError("config", "service show_properties contains a non-allowlisted property")
    properties = list(_BASE_SYSTEMD_SHOW_PROPERTIES)
    for item in extra:
        if item not in properties:
            properties.append(item)
    argv = ["/usr/bin/systemctl"] + (["--user"] if scope == "user" else []) + [
        "show", unit, "--no-pager", f"--property={','.join(properties)}"
    ]
    result = await _run_process(argv, env=_systemd_env(scope), timeout=4.0, output_limit=16*1024)
    if result.exit_code != 0:
        err = result.stderr.decode("utf-8","replace")
        raise ProbeError("service", err.strip() or f"systemctl exited {result.exit_code}", exit_code=result.exit_code)
    output, redacted = _redact_text(result.stdout.decode("utf-8","replace"))
    props: dict[str, Any] = {}
    for line in output.splitlines():
        key,_,value = line.partition("="); props[key]=value
    props["unit"]=unit; props["scope"]=scope
    if redacted: props["redacted"] = True
    if service.get("executable"): props["executable"] = service["executable"]
    if service.get("working_directory"): props["configured_working_directory"] = service["working_directory"]
    return props

async def _service_list(scope: str, pattern: str | None, limit: int) -> dict[str, Any]:
    scopes = ("user", "system") if scope == "all" else (scope,)
    services: list[dict[str, str]] = []
    complete = True
    for selected in scopes:
        argv = ["/usr/bin/systemctl"] + (["--user"] if selected == "user" else []) + [
            "list-units", "--type=service", "--all", "--no-legend", "--no-pager", "--plain",
        ]
        result = await _run_process(argv, env=_systemd_env(selected), timeout=10.0,
                                    output_limit=128 * 1024)
        if result.exit_code != 0:
            complete = False
            continue
        complete = complete and not result.truncated
        for raw in result.stdout.decode("utf-8", "replace").splitlines():
            parts = raw.strip().split(None, 4)
            if len(parts) < 4 or not UNIT_RE.fullmatch(parts[0]):
                continue
            description = parts[4] if len(parts) > 4 else ""
            if pattern and pattern.casefold() not in (parts[0] + " " + description).casefold():
                continue
            services.append({"unit": parts[0], "scope": selected, "load": parts[1],
                             "active": parts[2], "sub": parts[3], "description": description})
    services.sort(key=lambda row: (row["scope"], row["unit"]))
    truncated = len(services) > limit
    return {"services": services[:limit], "count": min(len(services), limit),
            "complete": complete and not truncated, "truncated": truncated}

async def _journal_unit(unit: str, scope: str, lines: int, since_seconds: int) -> dict[str, Any]:
    if not UNIT_RE.fullmatch(unit):
        raise ProbeError("invalid_input", "invalid systemd service unit")
    argv = ["/usr/bin/journalctl"] + (["--user"] if scope == "user" else []) + [
        "--unit", unit, "--no-pager", "--output=short-iso", "-n", str(lines),
        "--since", f"-{since_seconds} seconds",
    ]
    result = await _run_process(argv, env=_systemd_env(scope), timeout=15,
                                output_limit=MAX_TEXT_BYTES)
    if result.exit_code != 0:
        raise ProbeError("logs", result.stderr.decode("utf-8", "replace")[-1000:],
                         exit_code=result.exit_code)
    text, redacted = _redact_text(result.stdout.decode("utf-8", "replace"))
    return {"unit": unit, "scope": scope, "text": text, "complete": not result.truncated,
            "truncated": result.truncated, "redacted": redacted}

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

def _network_listeners() -> dict[str, Any]:
    listeners: list[dict[str, Any]] = []
    for proc_file in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        for host_hex, port, inode in _parse_proc_tcp(proc_file):
            listeners.append({"family": "tcp6" if proc_file.name == "tcp6" else "tcp",
                              "address_hex": host_hex, "port": port, "inode": inode})
    listeners.sort(key=lambda row: (row["port"], row["family"], row["inode"]))
    return {"listeners": listeners, "count": len(listeners), "complete": True}

async def _listeners(service_cfg: dict[str, Any]) -> dict[str, Any]:
    status = await _systemctl_show(service_cfg)
    pid = int(status.get("MainPID") or 0)
    registered_ports = {
        int(p) for p in service_cfg.get("ports", [])
        if isinstance(p, int) and 0 < p < 65536
    }
    transport = service_cfg.get("transport")
    endpoints = [
        str(value) for value in service_cfg.get("endpoints", [])
        if isinstance(value, str) and value
    ]
    base = {
        "unit": status["unit"],
        "main_pid": pid or None,
        "registered_ports": sorted(registered_ports),
        "registered_endpoints": endpoints,
        "transport": transport if isinstance(transport, str) and transport else (
            "tcp" if registered_ports else "not_declared"
        ),
    }
    if not registered_ports:
        return {
            **base,
            "listeners": [],
            "complete": False,
            "observability": "not_observable",
            "note": (
                "No listener ports are registered for this service; an empty result "
                "must not be interpreted as proof that the service has no endpoint."
            ),
        }

    socket_inodes: set[str] = set()
    fd_scan_complete = False
    if pid > 0:
        try:
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                match = re.fullmatch(r"socket:\[(\d+)\]", target)
                if match:
                    socket_inodes.add(match.group(1))
            fd_scan_complete = True
        except OSError:
            fd_scan_complete = False

    listeners = []
    observed_ports: set[int] = set()
    if fd_scan_complete:
        for proc_file in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            for _host, port, inode in _parse_proc_tcp(proc_file):
                if port in registered_ports and inode in socket_inodes:
                    observed_ports.add(port)
                    listeners.append({
                        "port": port,
                        "pid": pid,
                        "protocol": "tcp6" if proc_file.name == "tcp6" else "tcp",
                        "ownership_verified": True,
                    })

    missing_ports = sorted(registered_ports - observed_ports)
    complete = fd_scan_complete and not missing_ports
    return {
        **base,
        "listeners": listeners,
        "complete": complete,
        "observability": "observed" if complete else "partial",
        "missing_registered_ports": missing_ports,
        "fd_scan_complete": fd_scan_complete,
        "note": (
            None if complete else
            "One or more registered listener ports could not be attributed to the "
            "service MainPID; the result is intentionally not marked complete."
        ),
    }

def _tail_file(path: Path, lines: int, max_bytes: int = MAX_TEXT_BYTES, read_limit: int = 256*1024) -> tuple[str,bool]:
    st=path.lstat()
    if not statmod.S_ISREG(st.st_mode) or statmod.S_ISLNK(st.st_mode): raise ProbeError("file_type","operational log must be a regular file",status="denied")
    read_bytes=min(st.st_size,read_limit)
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


def _search_log_text(text: str, *, query: str, use_regex: bool, case_sensitive: bool,
                     limit: int, context_lines: int) -> dict[str, Any]:
    flags = 0 if case_sensitive else re.IGNORECASE
    if use_regex:
        try:
            pattern = re.compile(query, flags)
        except re.error as exc:
            raise ProbeError("invalid_input", f"invalid log search regex: {exc}") from exc
        matches_line = lambda line: pattern.search(line) is not None
    else:
        needle = query if case_sensitive else query.casefold()
        matches_line = lambda line: needle in (line if case_sensitive else line.casefold())
    lines = text.splitlines()
    hits: list[dict[str, Any]] = []
    seen_ranges: set[tuple[int, int]] = set()
    total_matches = 0
    for index, line in enumerate(lines):
        if not matches_line(line):
            continue
        total_matches += 1
        if len(hits) >= limit:
            continue
        start = max(0, index - context_lines)
        end = min(len(lines), index + context_lines + 1)
        key = (start, end)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        excerpt = "\n".join(lines[start:end])
        excerpt, redacted = _redact_text(excerpt)
        hits.append({
            "line": index + 1,
            "text": excerpt,
            "redacted": redacted,
        })
    return {
        "matches": hits,
        "match_count": total_matches,
        "returned": len(hits),
        "truncated_matches": total_matches > len(hits),
    }


async def _log_search(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(check["log"])
    cfg = _host_section(binding, "logs").get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "log target is not registered", status="denied")
    query = str(check["query"])
    limit = max(1, min(int(check.get("limit", 50)), 200))
    context_lines = max(0, min(int(check.get("context_lines", 0)), 5))
    since_seconds = int(check.get("since_seconds", 3600))
    # Scan substantially more than we return, while keeping memory/output bounded.
    scan_lines = min(5000, max(1000, limit * 25))
    source = ""
    source_truncated = False
    if cfg.get("type") == "journal":
        unit_alias = cfg.get("service")
        service_cfg = _host_section(binding, "services").get(unit_alias)
        if not isinstance(service_cfg, dict):
            raise ProbeError("config", "journal log refers to unknown service")
        unit = service_cfg["unit"]
        scope = service_cfg.get("scope", "user")
        argv = ["/usr/bin/journalctl"] + (["--user"] if scope == "user" else []) + [
            "--unit", unit, "--no-pager", "--output=short-iso",
            "-n", str(scan_lines), "--since", f"-{since_seconds} seconds",
        ]
        result = await _run_process(
            argv, env=_systemd_env(scope), timeout=15, output_limit=2 * 1024 * 1024
        )
        if result.exit_code != 0:
            raise ProbeError("logs", result.stderr.decode("utf-8", "replace")[-1000:],
                             exit_code=result.exit_code)
        text = result.stdout.decode("utf-8", "replace")
        source_truncated = result.truncated or len(text.splitlines()) >= scan_lines
        source = f"journal:{unit}"
    elif cfg.get("type") == "file":
        path = Path(str(cfg.get("path", "")))
        allowed_roots = [
            Path(str(v.get("path") if isinstance(v, dict) else v)).resolve()
            for v in _host_section(binding, "roots").values()
        ]
        if not path.is_absolute():
            raise ProbeError("config", "operational log path must be absolute")
        resolved = path.resolve(strict=True)
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise ProbeError("target_denied", "operational log is outside registered host roots", status="denied")
        text, source_truncated = await asyncio.to_thread(
            _tail_file, resolved, scan_lines, 2 * 1024 * 1024, 2 * 1024 * 1024
        )
        source = f"file:{alias}"
    else:
        raise ProbeError("config", "unknown log target type")
    filtered = _search_log_text(
        text,
        query=query,
        use_regex=bool(check.get("regex", False)),
        case_sensitive=bool(check.get("case_sensitive", False)),
        limit=limit,
        context_lines=context_lines,
    )
    return {
        "source": source,
        "query": query,
        "regex": bool(check.get("regex", False)),
        "case_sensitive": bool(check.get("case_sensitive", False)),
        "since_seconds": since_seconds,
        **filtered,
        "complete": not source_truncated and not filtered["truncated_matches"],
        "source_truncated": source_truncated,
        "redacted": any(bool(item.get("redacted")) for item in filtered["matches"]),
    }
