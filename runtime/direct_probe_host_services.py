from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shlex
import stat as statmod
import time
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



_SAFE_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SECRETISH_ENV_NAME_RE = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|COOKIE|AUTH)", re.I)


async def _service_config(service: dict[str, Any]) -> dict[str, Any]:
    unit = service.get("unit")
    scope = service.get("scope", "user")
    if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit) or scope not in {"user", "system"}:
        raise ProbeError("config", "service binding is invalid")
    raw_safe = service.get("safe_environment_keys", [])
    if (
        not isinstance(raw_safe, list)
        or not all(isinstance(item, str) and _SAFE_ENV_NAME_RE.fullmatch(item) for item in raw_safe)
        or any(_SECRETISH_ENV_NAME_RE.search(item) for item in raw_safe)
    ):
        raise ProbeError("config", "safe_environment_keys contains an unsafe name")
    safe_keys = set(raw_safe)
    argv = ["/usr/bin/systemctl"] + (["--user"] if scope == "user" else []) + [
        "show", unit, "--no-pager",
        "--property=ExecStart,WorkingDirectory,EnvironmentFiles,Environment",
    ]
    result = await _run_process(
        argv, env=_systemd_env(scope), timeout=5, output_limit=64 * 1024
    )
    if result.exit_code != 0:
        err, _ = _redact_text(result.stderr.decode("utf-8", "replace"))
        raise ProbeError("service", err[-1000:] or "systemctl show failed", exit_code=result.exit_code)
    props: dict[str, str] = {}
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    safe_env: dict[str, str | None] = {key: None for key in safe_keys}
    raw_env = props.get("Environment", "")
    try:
        fields = shlex.split(raw_env)
    except ValueError:
        fields = []
    for field in fields:
        key, sep, value = field.partition("=")
        if sep and key in safe_keys:
            cleaned, _ = _redact_text(value)
            safe_env[key] = cleaned[:2000]
    env_files, env_files_redacted = _redact_text(props.get("EnvironmentFiles", ""))
    exec_start, exec_redacted = _redact_text(props.get("ExecStart", ""))
    working, working_redacted = _redact_text(props.get("WorkingDirectory", ""))
    return {
        "unit": unit,
        "scope": scope,
        "safe_environment": safe_env,
        "environment_files": env_files[:4000],
        "exec_start": exec_start[:4000],
        "working_directory": working[:2000],
        "redacted": env_files_redacted or exec_redacted or working_redacted,
        "complete": True,
        "note": "Only explicitly registered non-secret environment keys are returned.",
    }

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



_LOG_CURSOR_DOMAIN = b"devcoveer-log-search-v1\0"
_LOG_CURSOR_TTL_SECONDS = 24 * 60 * 60


def _log_search_fingerprint(alias: str, check: dict[str, Any]) -> str:
    stable = {
        "log": alias,
        "query": str(check["query"]),
        "regex": bool(check.get("regex", False)),
        "case_sensitive": bool(check.get("case_sensitive", False)),
        "since_seconds": int(check.get("since_seconds", 3600)),
        "limit": int(check.get("limit", 50)),
        "context_lines": int(check.get("context_lines", 0)),
        "page_lines": int(check.get("page_lines", 500)),
        "page_bytes": int(check.get("page_bytes", 65536)),
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_log_cursor(body: dict[str, Any]) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    envelope = {
        "body": body,
        "checksum": hashlib.sha256(_LOG_CURSOR_DOMAIN + encoded).hexdigest(),
    }
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_log_cursor(token: str, *, alias: str, fingerprint: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise ProbeError("invalid_cursor", "log search cursor is invalid", status="denied")
    try:
        padded = token + "=" * (-len(token) % 4)
        envelope = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        body = envelope["body"]
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(_LOG_CURSOR_DOMAIN + encoded).hexdigest()
        if not isinstance(body, dict) or envelope.get("checksum") != checksum:
            raise ValueError("checksum")
    except (ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ProbeError("invalid_cursor", "log search cursor failed integrity validation", status="denied") from None
    if body.get("v") != 1 or body.get("log") != alias or body.get("fingerprint") != fingerprint:
        raise ProbeError("cursor_mismatch", "log search cursor does not match this source/query/options", status="denied")
    created_at = body.get("created_at")
    if not isinstance(created_at, int) or int(time.time()) - created_at > _LOG_CURSOR_TTL_SECONDS:
        raise ProbeError("cursor_expired", "log search cursor has expired; start a new bounded observation", status="denied")
    return body


def _search_log_records(records: list[dict[str, Any]], *, query: str, use_regex: bool,
                        case_sensitive: bool, limit: int, context_lines: int) -> dict[str, Any]:
    messages = [str(row.get("MESSAGE", "")) for row in records]
    filtered = _search_log_text(
        "\n".join(messages), query=query, use_regex=use_regex,
        case_sensitive=case_sensitive, limit=limit, context_lines=context_lines,
    )
    by_line = {index + 1: row for index, row in enumerate(records)}
    for hit in filtered["matches"]:
        row = by_line.get(int(hit["line"]))
        if isinstance(row, dict):
            hit["source_cursor"] = row.get("__CURSOR")
            raw_ts = row.get("__REALTIME_TIMESTAMP")
            try:
                hit["realtime_us"] = int(raw_ts) if raw_ts is not None else None
            except (TypeError, ValueError):
                hit["realtime_us"] = None
    return filtered


def _read_file_log_page(path: Path, *, offset: int, upper_size: int, page_bytes: int,
                        expected_source_id: str | None) -> tuple[str, int, str, int]:
    st = path.lstat()
    if not statmod.S_ISREG(st.st_mode) or statmod.S_ISLNK(st.st_mode):
        raise ProbeError("file_type", "operational log must be a regular non-symlink file", status="denied")
    source_id = f"{st.st_dev}:{st.st_ino}"
    if expected_source_id is not None and source_id != expected_source_id:
        raise ProbeError("cursor_gap", "operational log rotated; prior cursor can no longer prove continuous coverage", status="partial")
    if st.st_size < upper_size:
        raise ProbeError("cursor_gap", "operational log was truncated; prior cursor can no longer prove continuous coverage", status="partial")
    if offset < 0 or offset > upper_size:
        raise ProbeError("invalid_cursor", "operational log cursor offset is outside the frozen source", status="denied")
    with path.open("rb") as handle:
        handle.seek(offset)
        remaining = max(0, upper_size - offset)
        data = handle.read(min(page_bytes, remaining))
        if offset + len(data) < upper_size and data and not data.endswith(b"\n"):
            extra = handle.readline(min(64 * 1024, upper_size - offset - len(data)))
            data += extra
            if offset + len(data) < upper_size and not data.endswith(b"\n"):
                raise ProbeError(
                    "coverage_gap", "operational log line exceeds bounded page continuation size",
                    status="partial",
                )
    next_offset = min(upper_size, offset + len(data))
    text = data.decode("utf-8", "replace")
    return text, next_offset, source_id, st.st_mtime_ns


async def _log_search(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(check["log"])
    cfg = _host_section(binding, "logs").get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "log target is not registered", status="denied")
    query = str(check["query"])
    limit = max(1, min(int(check.get("limit", 50)), 200))
    context_lines = max(0, min(int(check.get("context_lines", 0)), 5))
    since_seconds = int(check.get("since_seconds", 3600))
    page_lines = max(20, min(int(check.get("page_lines", 500)), 1000))
    page_bytes = max(4096, min(int(check.get("page_bytes", 65536)), 262144))
    fingerprint = _log_search_fingerprint(alias, check)
    cursor_body = (
        _decode_log_cursor(check["cursor"], alias=alias, fingerprint=fingerprint)
        if check.get("cursor") else None
    )
    created_at = int(time.time()) if cursor_body is None else int(cursor_body["created_at"])
    matches_seen = 0 if cursor_body is None else int(cursor_body.get("matches_seen", 0))
    source = ""
    source_truncated = False
    next_cursor: str | None = None
    requested_interval: dict[str, Any]
    covered_interval: dict[str, Any]

    if cfg.get("type") == "journal":
        unit_alias = cfg.get("service")
        service_cfg = _host_section(binding, "services").get(unit_alias)
        if not isinstance(service_cfg, dict):
            raise ProbeError("config", "journal log refers to unknown service")
        unit = service_cfg["unit"]
        scope = service_cfg.get("scope", "user")
        upper_us = int(time.time() * 1_000_000) if cursor_body is None else int(cursor_body["upper_us"])
        lower_us = max(0, upper_us - since_seconds * 1_000_000)
        journal_cursor = None if cursor_body is None else cursor_body.get("journal_cursor")
        if cursor_body is not None and cursor_body.get("source_type") != "journal":
            raise ProbeError("cursor_mismatch", "log search cursor source type changed", status="denied")
        argv = ["/usr/bin/journalctl"] + (["--user"] if scope == "user" else []) + [
            "--unit", unit, "--no-pager", "--output=json", "--reverse",
            "--lines", str(page_lines + (1 if journal_cursor else 0)),
            "--since", f"@{lower_us / 1_000_000:.6f}",
            "--until", f"@{upper_us / 1_000_000:.6f}",
        ]
        if journal_cursor:
            argv += ["--cursor", str(journal_cursor)]
        result = await _run_process(
            argv, env=_systemd_env(scope), timeout=15, output_limit=2 * 1024 * 1024
        )
        if result.exit_code != 0:
            stderr = result.stderr.decode("utf-8", "replace")[-1000:]
            if journal_cursor and "cursor" in stderr.casefold():
                raise ProbeError(
                    "cursor_gap",
                    "journal continuation cursor is no longer available; retention/rotation may have created a coverage gap",
                    status="partial", exit_code=result.exit_code,
                )
            raise ProbeError("logs", stderr, exit_code=result.exit_code)
        records: list[dict[str, Any]] = []
        for raw in result.stdout.decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                source_truncated = True
                continue
            if isinstance(row, dict):
                records.append(row)
        if journal_cursor and records and records[0].get("__CURSOR") == journal_cursor:
            records = records[1:]
        has_more = result.truncated or len(records) >= page_lines
        records = records[:page_lines]
        source_truncated = source_truncated or bool(result.truncated)
        filtered = _search_log_records(
            records, query=query, use_regex=bool(check.get("regex", False)),
            case_sensitive=bool(check.get("case_sensitive", False)),
            limit=limit, context_lines=context_lines,
        )
        matches_seen += int(filtered["match_count"])
        last_cursor = records[-1].get("__CURSOR") if records else None
        timestamps: list[int] = []
        for row in records:
            try:
                timestamps.append(int(row.get("__REALTIME_TIMESTAMP")))
            except (TypeError, ValueError):
                pass
        if has_more and isinstance(last_cursor, str) and last_cursor:
            next_cursor = _encode_log_cursor({
                "v": 1, "log": alias, "fingerprint": fingerprint, "created_at": created_at,
                "source_type": "journal", "upper_us": upper_us, "journal_cursor": last_cursor,
                "matches_seen": matches_seen,
            })
        elif has_more:
            source_truncated = True
        requested_interval = {"kind": "realtime_us", "start": lower_us, "end": upper_us}
        covered_interval = {
            "kind": "realtime_us",
            "start": lower_us if not has_more else (min(timestamps) if timestamps else None),
            "end": upper_us,
            "direction": "newest_to_oldest",
        }
        source = f"journal:{unit}"

    elif cfg.get("type") == "file":
        path = Path(str(cfg.get("path", "")))
        allowed_roots = [
            Path(str(value.get("path") if isinstance(value, dict) else value)).resolve()
            for value in _host_section(binding, "roots").values()
        ]
        if not path.is_absolute():
            raise ProbeError("config", "operational log path must be absolute")
        resolved = path.resolve(strict=True)
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise ProbeError("target_denied", "operational log is outside registered host roots", status="denied")
        st = resolved.lstat()
        if cursor_body is not None and cursor_body.get("source_type") != "file":
            raise ProbeError("cursor_mismatch", "log search cursor source type changed", status="denied")
        upper_size = st.st_size if cursor_body is None else int(cursor_body["upper_size"])
        offset = 0 if cursor_body is None else int(cursor_body["offset"])
        expected_source_id = None if cursor_body is None else str(cursor_body["source_id"])
        text_page, next_offset, source_id, mtime_ns = await asyncio.to_thread(
            _read_file_log_page, resolved, offset=offset, upper_size=upper_size,
            page_bytes=page_bytes, expected_source_id=expected_source_id,
        )
        filtered = _search_log_text(
            text_page, query=query, use_regex=bool(check.get("regex", False)),
            case_sensitive=bool(check.get("case_sensitive", False)),
            limit=limit, context_lines=context_lines,
        )
        matches_seen += int(filtered["match_count"])
        has_more = next_offset < upper_size
        if has_more:
            next_cursor = _encode_log_cursor({
                "v": 1, "log": alias, "fingerprint": fingerprint, "created_at": created_at,
                "source_type": "file", "upper_size": upper_size, "offset": next_offset,
                "source_id": source_id, "mtime_ns": mtime_ns, "matches_seen": matches_seen,
            })
        requested_interval = {"kind": "bytes", "start": 0, "end": upper_size}
        covered_interval = {"kind": "bytes", "start": offset, "end": next_offset}
        source = f"file:{alias}"
    else:
        raise ProbeError("config", "unknown log target type")

    page_complete = next_cursor is None and not source_truncated and not filtered["truncated_matches"]
    coverage_complete = page_complete
    return {
        "source": source,
        "query": query,
        "regex": bool(check.get("regex", False)),
        "case_sensitive": bool(check.get("case_sensitive", False)),
        "since_seconds": since_seconds,
        **filtered,
        "requested_interval": requested_interval,
        "covered_interval": covered_interval,
        "frozen_upper_bound": requested_interval["end"],
        "coverage_complete": coverage_complete,
        "complete": coverage_complete,
        "source_truncated": source_truncated,
        "next_cursor": next_cursor,
        "continuation_required": next_cursor is not None,
        "matches_seen": matches_seen,
        "confirmed_no_matches": coverage_complete and matches_seen == 0,
        "retention": {
            "status": "observed_interval_complete" if coverage_complete else "in_progress",
            "note": "A cursor_gap error on continuation is reported as a retention/rotation coverage gap.",
        },
        "redacted": any(bool(item.get("redacted")) for item in filtered["matches"]),
    }
