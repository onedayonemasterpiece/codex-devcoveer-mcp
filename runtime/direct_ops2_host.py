from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import ssl
import stat as statmod
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from direct_probe_core import (
    ProbeError,
    _git,
    _redact_text,
    _run_process,
    _sha256_path,
    _validate_alias,
    _validate_rel_path,
)
from direct_probe_host_services import _host_section, _systemctl_show

MAX_HOST_TEXT_BYTES = 2 * 1024 * 1024
MAX_HTTP_BODY = 16 * 1024


def _registered_root(binding: dict[str, Any], alias: str) -> Path:
    alias = _validate_alias(alias)
    raw = _host_section(binding, "roots").get(alias)
    if isinstance(raw, dict):
        raw = raw.get("path")
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise ProbeError("target_denied", "host root is not registered", status="denied")
    path = Path(raw)
    st = path.lstat()
    if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISDIR(st.st_mode):
        raise ProbeError("file_type", "registered host root must be a real directory", status="denied")
    return path.resolve(strict=True)


def _root_target(binding: dict[str, Any], root_alias: str, raw_path: str) -> Path:
    root = _registered_root(binding, root_alias)
    rel = _validate_rel_path(raw_path)
    current = root
    for part in rel.parts:
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError as exc:
            raise ProbeError("not_found", "registered-root path does not exist", status="not_found") from exc
        if statmod.S_ISLNK(st.st_mode):
            raise ProbeError("symlink_escape", "registered-root symlinks are denied", status="denied")
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ProbeError("path_escape", "registered-root path escapes root", status="denied") from exc
    return resolved


def root_file_stat(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    path = _root_target(binding, check["root"], check["path"])
    st = path.lstat()
    kind = "file" if statmod.S_ISREG(st.st_mode) else "directory" if statmod.S_ISDIR(st.st_mode) else "special"
    if kind == "special":
        raise ProbeError("file_type", "special files are denied", status="denied")
    return {
        "root": check["root"], "path": check["path"], "type": kind,
        "size": st.st_size, "mode": oct(st.st_mode & 0o777), "mtime_ns": st.st_mtime_ns,
    }


def root_file_sha256(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    path = _root_target(binding, check["root"], check["path"])
    if not path.is_file():
        raise ProbeError("file_type", "hash target must be a regular file", status="denied")
    return {
        "root": check["root"], "path": check["path"],
        "sha256": _sha256_path(path, 128 * 1024 * 1024), "size": path.stat().st_size,
    }


def root_read_text(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    path = _root_target(binding, check["root"], check["path"])
    if not path.is_file():
        raise ProbeError("file_type", "text target must be a regular file", status="denied")
    st = path.stat()
    if st.st_size > MAX_HOST_TEXT_BYTES:
        raise ProbeError("file_too_large", "registered-root file exceeds text limit", status="denied")
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        raise ProbeError("binary_denied", "binary content is not returned", status="denied")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeError("binary_denied", "non-UTF8 content is not returned", status="denied") from exc
    lines = text.splitlines()
    start = int(check.get("start_line", 1))
    end = int(check.get("end_line", min(len(lines), start + 199)))
    if start < 1 or end < start:
        raise ProbeError("invalid_input", "invalid line range")
    end = min(end, len(lines), start + 199)
    selected = "\n".join(lines[start - 1:end])
    selected, redacted = _redact_text(selected)
    encoded = selected.encode("utf-8")
    truncated = len(encoded) > 64 * 1024
    if truncated:
        selected = encoded[:64 * 1024].decode("utf-8", "ignore")
    return {
        "root": check["root"], "path": check["path"], "start_line": start, "end_line": end,
        "total_lines": len(lines), "text": selected, "redacted": redacted,
        "complete": not truncated, "truncated": truncated,
    }


def _argv_shape(raw: bytes) -> list[str]:
    args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    shaped = []
    for index, arg in enumerate(args[:64]):
        if index == 0:
            value, _ = _redact_text(arg)
            shaped.append(value[:500])
        elif arg.startswith("-"):
            flag = arg.split("=", 1)[0]
            shaped.append(flag[:200] + ("=<arg>" if "=" in arg else ""))
        else:
            shaped.append("<arg>")
    return shaped


def _proc_info(pid: int, parent_pid: int | None) -> dict[str, Any] | None:
    base = Path(f"/proc/{pid}")
    try:
        exe = os.readlink(base / "exe")
        cwd = os.readlink(base / "cwd")
        cmdline = (base / "cmdline").read_bytes()
    except OSError:
        return None
    exe, exe_red = _redact_text(exe)
    cwd, cwd_red = _redact_text(cwd)
    return {
        "pid": pid, "parent_pid": parent_pid, "executable": exe[:1000], "cwd": cwd[:1000],
        "argv_shape": _argv_shape(cmdline), "redacted": exe_red or cwd_red,
    }


async def process_tree(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(check["service"])
    cfg = _host_section(binding, "services").get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "service is not registered", status="denied")
    state = await _systemctl_show(cfg)
    try:
        main_pid = int(state.get("MainPID", "0"))
    except (TypeError, ValueError):
        main_pid = 0
    if main_pid <= 0:
        return {"service": alias, "main_pid": main_pid, "processes": [], "complete": True}
    limit = min(int(check.get("limit", 64)), 128)
    queue = [(main_pid, None)]
    seen = set()
    rows = []
    while queue and len(rows) < limit:
        pid, parent = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        row = await asyncio.to_thread(_proc_info, pid, parent)
        if row is not None:
            rows.append(row)
        try:
            children = (Path(f"/proc/{pid}/task/{pid}/children").read_text().split())
        except OSError:
            children = []
        for child in children:
            try:
                queue.append((int(child), pid))
            except ValueError:
                continue
    return {
        "service": alias, "main_pid": main_pid, "processes": rows,
        "complete": not queue, "truncated": bool(queue),
        "environment_read": False,
    }


async def _resolve(host: str, port: int) -> list[str]:
    def do_resolve():
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    try:
        rows = await asyncio.wait_for(asyncio.to_thread(do_resolve), timeout=5)
    except (OSError, asyncio.TimeoutError) as exc:
        raise ProbeError("network", f"DNS resolution failed: {exc}") from exc
    out = []
    for row in rows:
        ip = row[4][0]
        if ip not in out:
            out.append(ip)
    return out[:16]


def _ip_class(value: str) -> dict[str, Any]:
    addr = ipaddress.ip_address(value)
    return {
        "address": value, "loopback": addr.is_loopback, "private": addr.is_private,
        "link_local": addr.is_link_local, "global": addr.is_global,
    }


def _connectable(value: str) -> bool:
    addr = ipaddress.ip_address(value)
    return addr.is_global or addr.is_loopback


async def _tcp(host: str, port: int) -> dict[str, Any]:
    addresses = await _resolve(host, port)
    allowed = [ip for ip in addresses if _connectable(ip)]
    if not allowed:
        raise ProbeError("network_denied", "resolved target is private/link-local/reserved", status="denied")
    ip = allowed[0]
    started = asyncio.get_running_loop().time()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=5)
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError) as exc:
        raise ProbeError("network", f"TCP connection failed: {exc}") from exc
    return {"host": host, "port": port, "peer_ip": ip,
            "duration_ms": int((asyncio.get_running_loop().time() - started) * 1000)}


async def _tls(host: str, port: int) -> dict[str, Any]:
    addresses = await _resolve(host, port)
    allowed = [ip for ip in addresses if _connectable(ip)]
    if not allowed:
        raise ProbeError("network_denied", "resolved target is private/link-local/reserved", status="denied")
    ip = allowed[0]
    ctx = ssl.create_default_context()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port, ssl=ctx, server_hostname=host), timeout=7
        )
        ssl_obj = writer.get_extra_info("ssl_object")
        cert = ssl_obj.getpeercert() if ssl_obj is not None else {}
        writer.close()
        await writer.wait_closed()
    except (OSError, ssl.SSLError, asyncio.TimeoutError) as exc:
        raise ProbeError("network", f"TLS connection failed: {exc}") from exc
    sans = [value for kind, value in cert.get("subjectAltName", []) if kind in {"DNS", "IP Address"}]
    return {
        "host": host, "port": port, "peer_ip": ip, "version": ssl_obj.version() if ssl_obj else None,
        "san": sans[:100], "not_after": cert.get("notAfter"),
        "subject": cert.get("subject"), "issuer": cert.get("issuer"),
    }


def _raw_http(ip: str, host: str, port: int, scheme: str, path: str, method: str) -> dict[str, Any]:
    sock = socket.create_connection((ip, port), timeout=5)
    try:
        if scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        host_header = host if (scheme == "http" and port == 80) or (scheme == "https" and port == 443) else f"{host}:{port}"
        request = (
            f"{method} {path} HTTP/1.1\r\nHost: {host_header}\r\n"
            "User-Agent: DevCoveer-Direct-Probe/2\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        f = sock.makefile("rb")
        status_line = f.readline(4096).decode("iso-8859-1", "replace").strip()
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise ProbeError("network", "HTTP response status line is invalid")
        status = int(parts[1]); reason = parts[2] if len(parts) > 2 else ""
        headers: dict[str, str] = {}
        total = 0
        for _ in range(100):
            line = f.readline(4096)
            total += len(line)
            if total > 32 * 1024:
                raise ProbeError("network", "HTTP headers exceed limit")
            if line in {b"\r\n", b"\n", b""}:
                break
            text = line.decode("iso-8859-1", "replace")
            key, sep, value = text.partition(":")
            if sep and key.lower() in {"content-type", "content-length", "location", "server"}:
                safe, _ = _redact_text(value.strip())
                headers[key.lower()] = safe[:1000]
        body = b"" if method == "HEAD" else f.read(MAX_HTTP_BODY + 1)
        truncated = len(body) > MAX_HTTP_BODY
        if truncated:
            body = body[:MAX_HTTP_BODY]
        body_text, redacted = _redact_text(body.decode("utf-8", "replace"))
        return {"status": status, "reason": reason, "headers": headers,
                "body": body_text, "truncated": truncated, "redacted": redacted}
    finally:
        try:
            sock.close()
        except OSError:
            pass


async def network_probe(check: dict[str, Any]) -> dict[str, Any]:
    mode = check["mode"]
    if mode == "http":
        raw_url = check.get("url")
        if not isinstance(raw_url, str):
            raise ProbeError("invalid_input", "http network probe requires url")
        parsed = urlsplit(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ProbeError("invalid_input", "network URL must be plain public/loopback http(s) without credentials or fragment")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = await _resolve(host, port)
        allowed = [ip for ip in addresses if _connectable(ip)]
        if not allowed:
            raise ProbeError("network_denied", "resolved target is private/link-local/reserved", status="denied")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        method = check.get("method", "GET")
        result = await asyncio.wait_for(asyncio.to_thread(_raw_http, allowed[0], host, port, parsed.scheme, path, method), timeout=10)
        return {"mode": mode, "host": host, "port": port, "peer_ip": allowed[0], "followed_redirect": False, **result}
    host = check.get("host")
    if not isinstance(host, str) or not host:
        raise ProbeError("invalid_input", f"{mode} network probe requires host")
    port = int(check.get("port", 0))
    if mode == "dns":
        addresses = await _resolve(host, port or 443)
        return {"mode": mode, "host": host, "addresses": [_ip_class(ip) for ip in addresses]}
    if port < 1 or port > 65535:
        raise ProbeError("invalid_input", "network port must be 1..65535")
    if mode == "tcp":
        return {"mode": mode, **await _tcp(host, port)}
    if mode == "tls":
        return {"mode": mode, **await _tls(host, port)}
    raise ProbeError("invalid_input", "unknown network probe mode")




_GITHUB_REPO_URL_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)


async def root_git_state(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    root = _registered_root(binding, check["root"])
    inside = await _git(root, "rev-parse", "--is-inside-work-tree", timeout=10, output_limit=128)
    if inside.exit_code != 0 or inside.stdout.decode("utf-8", "replace").strip() != "true":
        raise ProbeError("git_state", "registered root is not a Git worktree", status="denied")
    head_result = await _git(root, "rev-parse", "HEAD", timeout=10, output_limit=128)
    head = head_result.stdout.decode("utf-8", "replace").strip() if head_result.exit_code == 0 else None
    branch_result = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=10, output_limit=512, ok_codes=(0, 1))
    branch = branch_result.stdout.decode("utf-8", "replace").strip() if branch_result.exit_code == 0 else None
    tracked = await _git(root, "status", "--porcelain=v1", "--untracked-files=no", timeout=15, output_limit=64 * 1024)
    if tracked.exit_code != 0:
        raise ProbeError("git_state", "could not inspect registered-root tracked state", exit_code=tracked.exit_code)
    untracked = await _git(root, "ls-files", "--others", "--exclude-standard", "-z", timeout=15, output_limit=256 * 1024)
    if untracked.exit_code != 0:
        raise ProbeError("git_state", "could not inspect registered-root untracked state", exit_code=untracked.exit_code)
    untracked_count = len([item for item in untracked.stdout.split(b"\0") if item])
    tracked_lines = [line for line in tracked.stdout.decode("utf-8", "replace").splitlines() if line]
    return {
        "root": check["root"], "head": head, "branch": branch,
        "tracked_clean": not tracked_lines,
        "tracked_changes": tracked_lines[:200],
        "untracked_count": untracked_count,
        "complete": not tracked.truncated and not untracked.truncated,
    }


def _registered_repo_from_url(url: str) -> str | None:
    match = _GITHUB_REPO_URL_RE.fullmatch(url.strip())
    return match.group(1) if match else None


async def root_git_sync_exact(binding: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    root = _registered_root(binding, action["root"])
    remote = _validate_alias(action.get("remote", "origin"))
    branch = str(action["branch"])
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch) is None or ".." in branch:
        raise ProbeError("invalid_ref", "invalid root Git branch")
    expected = str(action["expected_sha"]).lower()
    if re.fullmatch(r"[0-9a-f]{40}", expected) is None:
        raise ProbeError("invalid_input", "expected_sha must be exact 40-hex")

    state = await root_git_state(binding, {"root": action["root"]})
    if not state["tracked_clean"]:
        raise ProbeError(
            "dirty_tracked",
            "registered runtime root has tracked/staged edits; refusing exact sync without resetting them",
            status="denied",
        )
    expected_head = action.get("expected_head")
    if expected_head is not None and state.get("head") != expected_head:
        raise ProbeError(
            "workspace_drift",
            f"registered runtime HEAD {state.get('head') or '<unknown>'} differs from expected {expected_head}",
            status="denied",
        )

    remote_url = await _git(root, "remote", "get-url", remote, timeout=10, output_limit=4096)
    if remote_url.exit_code != 0:
        raise ProbeError("remote_denied", "registered runtime remote is unavailable", status="denied")
    configured_repo = binding.get("github_repo")
    observed_repo = _registered_repo_from_url(remote_url.stdout.decode("utf-8", "replace"))
    if isinstance(configured_repo, str) and configured_repo and observed_repo != configured_repo:
        raise ProbeError("remote_mismatch", "registered runtime Git remote does not match project repository", status="denied")

    fetched = await _git(root, "fetch", "--no-tags", "--no-recurse-submodules", remote,
                         f"refs/heads/{branch}", timeout=120, output_limit=64 * 1024)
    if fetched.exit_code != 0:
        err, _ = _redact_text(fetched.stderr.decode("utf-8", "replace"))
        raise ProbeError("fetch_failed", err[-1600:] or "registered runtime fetch failed",
                         exit_code=fetched.exit_code)
    observed = await _git(root, "rev-parse", "FETCH_HEAD", timeout=10, output_limit=128)
    fetched_sha = observed.stdout.decode("utf-8", "replace").strip() if observed.exit_code == 0 else ""
    if fetched_sha != expected:
        raise ProbeError("remote_moved", f"fetched SHA {fetched_sha or '<unknown>'} differs from expected {expected}",
                         status="denied")
    checkout = await _git(root, "checkout", "--detach", expected, timeout=60, output_limit=64 * 1024)
    if checkout.exit_code != 0:
        err, _ = _redact_text(checkout.stderr.decode("utf-8", "replace"))
        raise ProbeError("checkout_failed", err[-1600:] or "registered runtime checkout failed",
                         exit_code=checkout.exit_code)
    after = await root_git_state(binding, {"root": action["root"]})
    if after.get("head") != expected or not after.get("tracked_clean"):
        raise ProbeError("readback_failed", "registered runtime exact sync failed readback")
    return {
        "status": "ok", "action": "root_git_sync_exact", "root": action["root"],
        "remote": remote, "branch": branch, "old_head": state.get("head"),
        "new_head": expected, "untracked_preserved_count": after.get("untracked_count", 0),
    }

def mcp_manifest(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    from direct_ops_gateway import describe_plane
    alias = _validate_alias(check.get("file", "bridge"))
    raw = _host_section(binding, "runtime_files").get(alias)
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise ProbeError("target_denied", "MCP manifest source is not registered", status="denied")
    path = Path(raw)
    st = path.lstat()
    if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISREG(st.st_mode):
        raise ProbeError("file_type", "MCP manifest source must be a regular file", status="denied")
    if st.st_size > 4 * 1024 * 1024:
        raise ProbeError("file_too_large", "MCP manifest source exceeds limit", status="denied")
    text = path.read_text(encoding="utf-8", errors="replace")
    version_match = re.search(r'BRIDGE_VERSION\s*=\s*"([^"]+)"', text)
    pairs = re.findall(
        r'types\.Tool\(name="([^"]+)".*?input_schema=([A-Z][A-Z0-9_]*)',
        text, flags=re.S,
    )
    unique = []
    seen = set()
    for name, schema in pairs:
        if name not in seen:
            seen.add(name); unique.append((name, schema))
    digest = hashlib.sha256(json.dumps(unique, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    return {
        "file": alias, "bridge_sha256": _sha256_path(path, 8 * 1024 * 1024),
        "version": version_match.group(1) if version_match else None,
        "tool_count": len(unique), "tools": [{"name": n, "input_schema_symbol": s} for n, s in unique],
        "catalog_digest": digest,
        "direct_schema_digests": {plane: describe_plane(plane)["schema_digest"] for plane in
                                  ("project_probe", "project_action", "host_probe", "host_action")},
        "direct_schema_source": "loaded-operation-schemas",
    }


def _systemd_env() -> dict[str, str]:
    uid = os.getuid()
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/home/dev"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }


async def daemon_reload(binding: dict[str, Any]) -> dict[str, Any]:
    services = _host_section(binding, "services")
    if not any(isinstance(cfg, dict) and cfg.get("scope", "user") == "user" for cfg in services.values()):
        raise ProbeError("target_denied", "no registered user service authorizes daemon-reload", status="denied")
    result = await _run_process(
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        env=_systemd_env(), timeout=30, output_limit=16 * 1024,
    )
    if result.exit_code != 0:
        err, _ = _redact_text(result.stderr.decode("utf-8", "replace"))
        raise ProbeError("daemon_reload", err[-1200:] or "daemon-reload failed", exit_code=result.exit_code)
    return {"status": "ok", "action": "daemon_reload"}


def delete_registered_file(binding: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(action["file"])
    cfg = _host_section(binding, "host_files").get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "host file is not registered", status="denied")
    raw = cfg.get("path")
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise ProbeError("config", "registered host file path is invalid")
    path = Path(raw)
    if not path.exists():
        raise ProbeError("not_found", "registered host file is absent", status="not_found")
    st = path.lstat()
    if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISREG(st.st_mode):
        raise ProbeError("file_type", "registered host file must be regular", status="denied")
    old_sha = _sha256_path(path, 16 * 1024 * 1024)
    expected = action.get("expected_sha256")
    if expected is not None and expected != old_sha:
        raise ProbeError("precondition_failed", "host file SHA-256 does not match expected value", status="denied")
    path.unlink()
    return {"status": "ok", "action": "delete_registered_file", "file": alias, "old_sha256": old_sha}
