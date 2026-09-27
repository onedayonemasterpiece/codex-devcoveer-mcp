from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import os
import re
import stat as statmod
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from direct_probe_core import ProbeError, _redact_text, _run_process, _sha256_path, _validate_alias
from direct_probe_disk_forensics import resolve_resource_target, run_resource_profile
from direct_probe_host_services import _host_section

_PROCESS_RUNTIME_SNAPSHOT_NS = time.time_ns()
_RUNTIME_FINGERPRINT_MODULES = ("source_handoff", "direct_probes", __name__)


def _capture_loaded_module_fingerprints() -> dict[str, dict[str, str]]:
    """Capture hashes for the small allowlist of MCP modules already loaded.

    The bridge imports source_handoff before direct_probes, and direct_probes
    imports this host runtime. Capturing these three known modules at startup is
    bounded and lets later probes distinguish an on-disk replacement from the
    source that this process actually imported.
    """
    result: dict[str, dict[str, str]] = {}
    for name in _RUNTIME_FINGERPRINT_MODULES:
        module = sys.modules.get(name)
        raw = getattr(module, "__file__", None) if module is not None else None
        if not isinstance(raw, str) or not raw:
            continue
        try:
            path = Path(raw).resolve(strict=True)
            st = path.lstat()
            if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISREG(st.st_mode):
                continue
            result[str(path)] = {
                "module": name,
                "sha256": _sha256_path(path, 128 * 1024 * 1024),
            }
        except (OSError, ValueError):
            continue
    return result


_LOADED_MODULE_FINGERPRINTS = _capture_loaded_module_fingerprints()


def _loaded_module_fingerprint(path: Path) -> dict[str, str] | None:
    return _LOADED_MODULE_FINGERPRINTS.get(str(path))


def _disk_usage(path: Path) -> dict[str, int]:
    v = os.statvfs(path)
    return {"total_bytes": v.f_blocks * v.f_frsize, "free_bytes": v.f_bavail * v.f_frsize,
            "total_inodes": v.f_files, "free_inodes": v.f_favail}

def _memory_load() -> dict[str, Any]:
    mem: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                parts = rest.strip().split()
                mem[key + "Bytes"] = int(parts[0]) * 1024
    except (OSError, ValueError):
        pass
    try:
        load = list(os.getloadavg())
    except OSError:
        load = []
    return {"memory": mem, "load": load}

def _system_info() -> dict[str, Any]:
    release: dict[str, str] = {}
    try:
        for raw in Path("/etc/os-release").read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = raw.partition("=")
            if sep and key in {"ID", "NAME", "VERSION", "VERSION_ID", "PRETTY_NAME"}:
                release[key.lower()] = value.strip().strip('"')[:256]
    except OSError:
        pass
    uname = os.uname()
    try:
        uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime_seconds = None
    return {"os_release": release,
            "kernel": {"sysname": uname.sysname, "release": uname.release,
                       "machine": uname.machine},
            "hostname": uname.nodename, "uptime_seconds": uptime_seconds,
            **_memory_load()}

async def _resources(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    roots = _host_section(binding, "roots")
    alias = check.get("root")
    result: dict[str, Any] = _memory_load()
    if alias:
        alias = _validate_alias(alias)
        raw = roots.get(alias)
        if raw is None:
            raise ProbeError("target_denied", "resource root is not registered", status="denied")
        path, profile = resolve_resource_target(raw)
        result["root"] = {"alias": alias, **_disk_usage(path)}
        largest = min(int(check.get("largest", 10)), 30)
        if largest:
            du = await _run_process(["/usr/bin/du", "-x", "-B1", "--max-depth=1", str(path)],
                                    env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
                                    timeout=30, output_limit=24 * 1024)
            items = []
            if du.exit_code == 0:
                for line in du.stdout.decode("utf-8", "replace").splitlines():
                    size_s, _, p = line.partition("\t")
                    try: size = int(size_s)
                    except ValueError: continue
                    child = Path(p)
                    if child == path: continue
                    items.append({"name": child.name, "bytes": size})
                items.sort(key=lambda x: x["bytes"], reverse=True)
                result["largest_immediate"] = items[:largest]
                result["largest_complete"] = not du.truncated
        if profile is not None:
            result["profile_data"] = await run_resource_profile(profile, path)
    else:
        resolved = {}
        for name, raw in roots.items():
            path, _profile = resolve_resource_target(raw)
            resolved[name] = _disk_usage(path)
        result["roots"] = resolved
    return result

def _health_request(url: str, method: str, timeout: float, body_limit: int) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ProbeError("config", "health endpoint must be a fixed plain loopback HTTP URL without query/userinfo")
    try:
        addr = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if parsed.hostname not in {"localhost"}:
            raise ProbeError("config", "health host is not loopback")
    else:
        if not addr.is_loopback:
            raise ProbeError("config", "health host is not loopback")
    port = parsed.port or 80
    path = parsed.path or "/"
    started = time.monotonic()
    conn = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
    try:
        conn.request(method, path, headers={"User-Agent": "DevCoveer-Direct-Probe/1"})
        response = conn.getresponse()
        body = b"" if method == "HEAD" else response.read(body_limit + 1)
    finally:
        conn.close()
    truncated = len(body) > body_limit
    if truncated: body = body[:body_limit]
    text = body.decode("utf-8", "replace")
    text, red = _redact_text(text)
    return {"status": response.status, "reason": response.reason, "duration_ms": int((time.monotonic() - started) * 1000),
            "body": text, "truncated": truncated, "redacted": red, "redirect": response.status in {301,302,303,307,308}}

async def _health(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(check["endpoint"])
    cfg = _host_section(binding, "health").get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "health endpoint is not registered", status="denied")
    method = check.get("method", "GET")
    if method not in cfg.get("methods", ["GET", "HEAD"]):
        raise ProbeError("target_denied", "health method is not allowed", status="denied")
    data = await asyncio.wait_for(asyncio.to_thread(_health_request, str(cfg["url"]), method, float(cfg.get("timeout_seconds",2.0)), int(cfg.get("body_bytes",4096))), timeout=4.0)
    data["endpoint"] = alias; data["method"] = method; data["followed_redirect"] = False
    return data

async def _runtime(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(check["runtime"])
    cfg = _host_section(binding, "runtimes").get(alias)
    if not isinstance(cfg, dict): raise ProbeError("target_denied", "runtime is not registered", status="denied")
    typ = cfg.get("type")
    if typ == "cli":
        argv = cfg.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x,str) for x in argv) or not argv[0].startswith("/"):
            raise ProbeError("config", "runtime CLI argv is invalid")
        result = await _run_process(argv, env={"PATH":"/usr/local/bin:/usr/bin:/bin","HOME":os.environ.get("HOME","/home/dev"),"LANG":"C.UTF-8"}, timeout=4, output_limit=4096)
        if result.exit_code != 0: raise ProbeError("runtime", result.stderr.decode("utf-8","replace")[-1000:], exit_code=result.exit_code)
        text, red = _redact_text(result.stdout.decode("utf-8","replace").strip())
        return {"runtime":alias,"type":"cli","executable":argv[0],"version":text,"redacted":red}
    if typ == "package":
        interpreter = str(cfg.get("python", "")); distribution = str(cfg.get("distribution", ""))
        if not interpreter.startswith("/") or not re.fullmatch(r"[A-Za-z0-9_.-]+", distribution):
            raise ProbeError("config", "package runtime binding is invalid")
        helper = "import importlib.metadata as m, json, sys; n=sys.argv[1]; print(json.dumps({'name':n,'version':m.version(n)}))"
        result = await _run_process([interpreter,"-P","-c",helper,distribution], env={"PATH":"/usr/local/bin:/usr/bin:/bin","HOME":os.environ.get("HOME","/home/dev"),"LANG":"C.UTF-8"}, timeout=4, output_limit=4096)
        if result.exit_code != 0: raise ProbeError("runtime", result.stderr.decode("utf-8","replace")[-1000:], exit_code=result.exit_code)
        payload = json.loads(result.stdout)
        return {"runtime":alias,"type":"package","python":interpreter,"distribution":payload["name"],"version":payload["version"],"app_imported":False}
    raise ProbeError("config", "unknown runtime type")

async def _runtime_file(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(check["file"])
    raw = _host_section(binding, "runtime_files").get(alias)
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise ProbeError("target_denied", "runtime file is not registered", status="denied")
    path = Path(raw).resolve(strict=True)
    st = path.lstat()
    if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISREG(st.st_mode):
        raise ProbeError("file_type", "runtime file must be regular and non-symlink", status="denied")
    digest = await asyncio.to_thread(_sha256_path, path, 128 * 1024 * 1024)

    loaded = _loaded_module_fingerprint(path)
    loaded_sha = loaded.get("sha256") if isinstance(loaded, dict) else None
    loaded_module = loaded.get("module") if isinstance(loaded, dict) else None
    matches = digest == loaded_sha if isinstance(loaded_sha, str) else None
    state = (
        "matches_disk" if matches is True
        else "disk_changed_after_process_import" if matches is False
        else "not_observable"
    )
    return {
        "file": alias,
        "path": str(path),
        "sha256": digest,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "loaded_process_version": loaded_sha or "not_observable",
        "loaded_process_sha256": loaded_sha,
        "loaded_module": loaded_module,
        "process_import_snapshot_ns": _PROCESS_RUNTIME_SNAPSHOT_NS,
        "disk_matches_loaded_process": matches,
        "loaded_process_state": state,
        "note": (
            "Startup source fingerprint matches the current installed file."
            if matches is True else
            "The installed file changed after this MCP process imported the module; restart is required."
            if matches is False else
            "This registered file was not present in the process module table when the runtime fingerprint snapshot was captured."
        ),
    }

def _config_presence_file(path: Path, allowed_keys: set[str], requested: list[str]) -> dict[str, Any]:
    st = path.lstat()
    if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISREG(st.st_mode): raise ProbeError("file_type","config source must be a regular non-symlink file",status="denied")
    requested_set = set(requested)
    if not requested_set <= allowed_keys: raise ProbeError("target_denied","one or more config keys are not allowlisted",status="denied")
    found = {key:False for key in requested}
    with path.open("r",encoding="utf-8",errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            key,value = line.split("=",1); key=key.strip()
            if key in requested_set:
                value=value.strip(); found[key]=bool(value and value not in {"''",'""'})
    return {"keys":[{"key":key,"present_nonempty":found[key]} for key in requested]}



def _config_values_file(path: Path, allowed_keys: set[str], requested: list[str]) -> dict[str, str | None]:
    st = path.lstat()
    if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISREG(st.st_mode):
        raise ProbeError("file_type", "config source must be a regular non-symlink file", status="denied")
    requested_set = set(requested)
    if not requested_set <= allowed_keys:
        raise ProbeError("target_denied", "one or more config keys are not allowlisted", status="denied")
    found: dict[str, str | None] = {key: None for key in requested}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in requested_set:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            found[key] = value
    return found


def _safe_config_metadata(key: str, value: str | None) -> dict[str, Any]:
    if value is None or value == '':
        return {'key': key, 'present_nonempty': False, 'kind': 'missing'}
    row: dict[str, Any] = {'key': key, 'present_nonempty': True, 'kind': 'opaque'}
    try:
        parsed = urlsplit(value)
        scheme = (parsed.scheme or '').lower()
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password_present = parsed.password is not None
    except ValueError:
        return row
    if scheme in {'http', 'https'} and hostname:
        row.update({
            'kind': 'http_url', 'scheme': scheme, 'hostname': hostname, 'port': port,
            'path_present': bool(parsed.path and parsed.path != '/'),
            'query_present': bool(parsed.query),
        })
        if hostname.endswith('.supabase.co'):
            row['project_ref'] = hostname.split('.', 1)[0]
        return row
    if scheme in {'postgres', 'postgresql'} and hostname:
        row.update({
            'kind': 'postgres_dsn', 'scheme': scheme, 'hostname': hostname, 'port': port,
            'database': parsed.path.lstrip('/') or None, 'username': username,
            'password_present': password_present, 'query_present': bool(parsed.query),
        })
    return row


async def _config_metadata(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(check["source"])
    cfg = _host_section(binding, "config_presence").get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "config metadata source is not registered", status="denied")
    path = Path(str(cfg.get("path", "")))
    if not path.is_absolute():
        raise ProbeError("config", "config metadata path must be absolute")
    allowed = {str(item) for item in cfg.get("keys", [])}
    requested = list(check["keys"])
    values = await asyncio.to_thread(_config_values_file, path, allowed, requested)
    return {
        "source": alias,
        "keys": [_safe_config_metadata(key, values.get(key)) for key in requested],
        "values_returned": False,
        "hashes_returned": False,
    }


async def _config_compare(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(check["source"])
    cfg = _host_section(binding, "config_presence").get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "config compare source is not registered", status="denied")
    path = Path(str(cfg.get("path", "")))
    if not path.is_absolute():
        raise ProbeError("config", "config compare path must be absolute")
    allowed = {str(item) for item in cfg.get("keys", [])}
    requested = list(check["keys"])
    values = await asyncio.to_thread(_config_values_file, path, allowed, requested)
    missing = [key for key in requested if not values.get(key)]
    groups: list[list[str]] = []
    used: set[str] = set()
    for key in requested:
        value = values.get(key)
        if not value or key in used:
            continue
        group = [other for other in requested if values.get(other) == value]
        used.update(group)
        groups.append(group)
    nonempty_count = len(requested) - len(missing)
    return {
        "source": alias,
        "groups": groups,
        "missing": missing,
        "nonempty_count": nonempty_count,
        "all_present_equal": bool(nonempty_count == len(requested) and len(groups) == 1),
        "values_returned": False,
        "hashes_returned": False,
    }

async def _config_presence(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(check["source"]); cfg = _host_section(binding,"config_presence").get(alias)
    if not isinstance(cfg,dict): raise ProbeError("target_denied","config presence source is not registered",status="denied")
    path = Path(str(cfg.get("path","")))
    if not path.is_absolute(): raise ProbeError("config","config presence path must be absolute")
    allowed = {str(x) for x in cfg.get("keys",[])}
    data = await asyncio.to_thread(_config_presence_file,path,allowed,list(check["keys"])); data["source"]=alias; data["values_returned"]=False
    return data

def _inventory(binding: dict[str, Any]) -> dict[str, Any]:
    return {"services":sorted(_host_section(binding,"services")),"logs":sorted(_host_section(binding,"logs")),"roots":sorted(_host_section(binding,"roots")),"health":sorted(_host_section(binding,"health")),"runtimes":sorted(_host_section(binding,"runtimes")),"runtime_files":sorted(_host_section(binding,"runtime_files")),"host_files":sorted(_host_section(binding,"host_files")),"config_presence":sorted(_host_section(binding,"config_presence"))}
