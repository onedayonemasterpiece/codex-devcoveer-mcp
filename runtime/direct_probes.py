from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from direct_probe_core import (
    BATCH_DEADLINE_SECONDS, CHECK_TIMEOUT_SECONDS, DEFAULT_CONFIG_PATH, HOST_PROBE_INPUT_SCHEMA,
    MAX_CHECKS, MAX_RESPONSE_BYTES, PROJECT_PROBE_INPUT_SCHEMA, ProcessResult, ProbeError,
    _binding, _load_config, _now_iso, _redact_text, _run_process, _safe_worktree_path,
    _validate_alias, _validate_ref,
)
from direct_probe_project import (
    _diff, _file_list, _file_sha, _file_stat, _git_blob, _git_log, _git_state, _github_status,
    _read_text, _remote_head, _search, _tree, _worktree_list,
)
from direct_ops2 import project_capabilities, workspace_compare
from direct_ops2_worktrees import worktree_compare
from direct_web_context import read_instructions, web_preflight
from direct_ops2_host import (
    mcp_manifest, network_probe, process_tree, root_file_sha256, root_file_stat, root_read_text,
)
from direct_probe_host import (
    _config_presence, _health, _host_section, _inventory, _journal_unit, _listeners, _logs,
    _network_listeners, _resources, _runtime, _runtime_file, _service_list, _system_info,
    _systemctl_show,
)

def _base_result(check: dict[str, Any], started: float) -> dict[str, Any]:
    return {"id": check["id"], "operation": check["kind"], "observed_at": _now_iso(),
            "duration_ms": int((time.monotonic() - started) * 1000), "complete": True,
            "truncated": False, "redacted": False}


def _encoded_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _set_response_bytes(payload: dict[str, Any]) -> dict[str, Any]:
    payload["response_bytes"] = 0
    for _ in range(6):
        size = _encoded_size(payload)
        if payload["response_bytes"] == size:
            break
        payload["response_bytes"] = size
    return payload


def _fit_response(payload: dict[str, Any]) -> dict[str, Any]:
    _set_response_bytes(payload)
    if _encoded_size(payload) <= MAX_RESPONSE_BYTES:
        return payload
    for result in reversed(payload.get("results", [])):
        data = result.get("data")
        if isinstance(data, dict):
            for key in ("text", "body"):
                if isinstance(data.get(key), str) and data[key]:
                    data[key] = data[key][:1024] + "\n<response-cap-truncated>"
                    result["truncated"] = True
                    result["complete"] = False
            if isinstance(data.get("failed_log"), dict) and isinstance(data["failed_log"].get("text"), str):
                data["failed_log"]["text"] = data["failed_log"]["text"][:1024] + "\n<response-cap-truncated>"
                result["truncated"] = True
                result["complete"] = False
        _set_response_bytes(payload)
        if _encoded_size(payload) <= MAX_RESPONSE_BYTES:
            return payload
    while len(payload.get("results", [])) > 1:
        payload["results"].pop()
        payload["response_truncated"] = True
        _set_response_bytes(payload)
        if _encoded_size(payload) <= MAX_RESPONSE_BYTES:
            return payload
    if payload.get("results"):
        result = payload["results"][0]
        result["data"] = {"omitted": "response cap exceeded"}
        result["truncated"] = True
        result["complete"] = False
        payload["response_truncated"] = True
    _set_response_bytes(payload)
    if _encoded_size(payload) > MAX_RESPONSE_BYTES:
        raise ProbeError("response_cap", "bounded probe metadata exceeded response budget")
    return payload


async def _one_project(root: Path, binding: dict[str, Any], check: dict[str, Any]) -> tuple[dict[str, Any], str]:
    kind = check["kind"]
    if kind == "instructions": return read_instructions(root, check), "exact-instruction-file"
    if kind == "web_preflight": return await web_preflight(root, check), "project-local-web-runtime"
    if kind == "git_state": return await _git_state(root), "git-local"
    if kind == "remote_head": return await _remote_head(root, binding, check["remote"], check["branch"]), "github-remote+git-tracking"
    if kind == "git_log": return await _git_log(root, check), "git-local-object"
    if kind == "tree": return await _tree(root, check), "git-local-object"
    if kind == "worktree_list": return await _worktree_list(root), "git-local"
    if kind == "worktree_compare": return await worktree_compare(root, check), "git-worktree/object/worktree"
    if kind == "capabilities": return await project_capabilities(root, binding), "git-local+trusted-server-config"
    if kind == "workspace_compare": return await workspace_compare(root, check["target_ref"], include_untracked=bool(check.get("include_untracked", True)), limit=int(check.get("limit", 500))), "git-local-object/worktree"
    if kind == "diff": return await _diff(root, check), "git-local-object/worktree"
    if kind == "read_text": return await _read_text(root, binding, check), check.get("source", "worktree")
    if kind == "search": return await _search(root, check), "worktree-files"
    if kind == "file_list": return await _file_list(root, check), "worktree-metadata"
    if kind == "file_stat": return await _file_stat(root, check["path"]), "worktree-metadata+git-index"
    if kind == "file_sha256": return await _file_sha(root, check["path"]), "worktree-file"
    if kind == "github_status": return await _github_status(binding, check), "github-read-api"
    raise ProbeError("invalid_input", f"unknown project probe kind {kind!r}")


async def _one_host(binding: dict[str, Any], check: dict[str, Any]) -> tuple[dict[str, Any], str]:
    kind = check["kind"]
    if kind == "inventory": return _inventory(binding), "trusted-server-config"
    if kind == "system_info": return _system_info(), "proc+os-release"
    if kind == "service_list":
        return await _service_list(check.get("scope", "all"), check.get("pattern"),
                                   int(check.get("limit", 200))), "systemd"
    if kind == "journal":
        return await _journal_unit(check["unit"], check.get("scope", "user"),
                                   int(check.get("lines", 100)),
                                   int(check.get("since_seconds", 3600))), "journal"
    if kind == "network_listeners": return _network_listeners(), "proc-sockets"
    if kind == "service":
        alias = _validate_alias(check["service"]); cfg = _host_section(binding, "services").get(alias)
        if not isinstance(cfg, dict): raise ProbeError("target_denied", "service is not registered", status="denied")
        data = await _systemctl_show(cfg); data["alias"] = alias; return data, "systemd"
    if kind == "listeners":
        alias = _validate_alias(check["service"]); cfg = _host_section(binding, "services").get(alias)
        if not isinstance(cfg, dict): raise ProbeError("target_denied", "service is not registered", status="denied")
        data = await _listeners(cfg); data["alias"] = alias; return data, "systemd+/proc-sockets"
    if kind == "logs": return await _logs(binding, check), "journal-or-operational-log"
    if kind == "resources": return await _resources(binding, check), "statvfs+/proc+du-metadata"
    if kind == "health": return await _health(binding, check), "registered-loopback-http"
    if kind == "runtime": return await _runtime(binding, check), "trusted-runtime"
    if kind == "runtime_file": return await _runtime_file(binding, check), "installed-file"
    if kind == "root_file_stat": return root_file_stat(binding, check), "registered-root"
    if kind == "root_file_sha256": return root_file_sha256(binding, check), "registered-root"
    if kind == "root_read_text": return root_read_text(binding, check), "registered-root"
    if kind == "process_tree": return await process_tree(binding, check), "systemd+/proc"
    if kind == "network": return await network_probe(check), "dns+socket+tls/http"
    if kind == "mcp_manifest": return mcp_manifest(binding, check), "installed-runtime-catalog"
    if kind == "config_presence": return await _config_presence(binding, check), "registered-config-presence"
    raise ProbeError("invalid_input", f"unknown host probe kind {kind!r}")


async def _execute_check(root: Path, binding: dict[str, Any], check: dict[str, Any], *, host: bool) -> dict[str, Any]:
    item_started = time.monotonic()
    base = _base_result(check, item_started)
    timeout = CHECK_TIMEOUT_SECONDS.get(check["kind"], 20.0)
    try:
        if host:
            data, fact_source = await asyncio.wait_for(_one_host(binding, check), timeout=timeout)
        else:
            data, fact_source = await asyncio.wait_for(_one_project(root, binding, check), timeout=timeout)
        base.update({"status": "ok", "source": fact_source, "data": data})
        if any(token in fact_source for token in ("git", "github", "systemd", "journal", "du-", "trusted-runtime")):
            base["exit_code"] = 0
        if isinstance(data, dict):
            base["complete"] = bool(data.get("complete", data.get("scan_complete", True)))
            base["truncated"] = bool(data.get("truncated", False))
            base["redacted"] = bool(data.get("redacted", False))
    except asyncio.TimeoutError:
        base.update({"status": "timeout", "complete": False,
                     "error": {"type": "timeout", "message": f"check exceeded {timeout:g}s bounded deadline"}})
    except ProbeError as exc:
        msg, red = _redact_text(str(exc))
        base.update({"status": exc.status, "complete": False, "redacted": red,
                     "error": {"type": exc.category, "message": msg}})
        if exc.exit_code is not None:
            base["exit_code"] = exc.exit_code
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg, red = _redact_text(f"{type(exc).__name__}: {exc}")
        base.update({"status": "error", "complete": False, "redacted": red,
                     "error": {"type": "internal", "message": msg[:1200]}})
    base["duration_ms"] = int((time.monotonic() - item_started) * 1000)
    base["observed_at"] = _now_iso()
    return base


async def _run_probe(project_root: str, project_id: str, checks: list[dict[str, Any]], *, host: bool,
                     config_path: Path | None = None) -> dict[str, Any]:
    request_started = time.monotonic()
    observed = _now_iso()
    root = Path(project_root).resolve(strict=True)
    config = _load_config(config_path or DEFAULT_CONFIG_PATH)
    binding = _binding(config, project_id)
    slots: list[dict[str, Any] | asyncio.Task[dict[str, Any]]] = []
    tasks: list[asyncio.Task[dict[str, Any]]] = []
    seen_ids: set[str] = set()
    for check in checks:
        if check["id"] in seen_ids:
            started = time.monotonic()
            base = _base_result(check, started)
            base.update({"status": "error", "complete": False,
                         "error": {"type": "duplicate_id", "message": "check ids must be unique"}})
            slots.append(base)
            continue
        seen_ids.add(check["id"])
        task = asyncio.create_task(_execute_check(root, binding, check, host=host))
        tasks.append(task)
        slots.append(task)

    if tasks:
        _done, pending = await asyncio.wait(tasks, timeout=BATCH_DEADLINE_SECONDS)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    results: list[dict[str, Any]] = []
    for check, slot in zip(checks, slots):
        if isinstance(slot, dict):
            results.append(slot)
        elif slot.done() and not slot.cancelled():
            try:
                results.append(slot.result())
            except asyncio.CancelledError:
                base = _base_result(check, request_started)
                base.update({"status": "timeout", "complete": False,
                             "error": {"type": "batch_deadline", "message": "batch deadline cancelled this check"}})
                results.append(base)
        else:
            base = _base_result(check, request_started)
            base.update({"status": "timeout", "complete": False,
                         "error": {"type": "batch_deadline",
                                   "message": f"probe batch exceeded {BATCH_DEADLINE_SECONDS:g}s deadline"}})
            results.append(base)

    payload = {
        "execution": "direct", "observed_at": observed,
        "target": {"project": project_id, "cwd": str(root), "host_binding": bool(project_id in config["projects"])},
        "duration_ms": int((time.monotonic() - request_started) * 1000),
        "batch_deadline_seconds": BATCH_DEADLINE_SECONDS,
        "atomic": False, "results": results, "response_truncated": False,
    }
    return _fit_response(payload)


async def run_project_probe(project_root: str, project_id: str, checks: list[dict[str, Any]], *, config_path: Path | None = None) -> dict[str, Any]:
    return await _run_probe(project_root, project_id, checks, host=False, config_path=config_path)


async def run_host_probe(project_root: str, project_id: str, checks: list[dict[str, Any]], *, config_path: Path | None = None) -> dict[str, Any]:
    return await _run_probe(project_root, project_id, checks, host=True, config_path=config_path)
