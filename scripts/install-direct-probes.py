#!/usr/bin/env python3
"""Fail-closed installer for deterministic direct read probes in DevCoveer MCP.

Default mode only verifies lineage, transforms the installed bridge in memory,
and compiles a candidate. --apply atomically installs the candidate bridge and
the direct-probe runtime modules/config. It never restarts services.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import py_compile
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

INSTALL_DIR = Path("/home/dev/.local/libexec/openai-codex-mcp")
BRIDGE = INSTALL_DIR / "codex-chatgpt-bridge.py"
BACKEND = INSTALL_DIR / "opencode_backend.py"
HEALTH = INSTALL_DIR / "model_health.py"
SOURCE_HANDOFF = INSTALL_DIR / "source_handoff.py"
DIRECT_RUNTIME_NAMES = (
    "direct_probes.py", "direct_probe_core.py", "direct_probe_project.py",
    "direct_probe_project_git.py", "direct_probe_project_files.py", "direct_probe_project_github.py",
    "direct_probe_host.py", "direct_probe_host_services.py", "direct_probe_host_runtime.py",
)
DIRECT_RUNTIME_TARGETS = {name: INSTALL_DIR / name for name in DIRECT_RUNTIME_NAMES}
DIRECT_CONFIG = INSTALL_DIR / "direct_probe_config.json"
TASK_DIR = Path("/home/dev/.local/share/openai-codex-mcp/tasks")
TASK_ID_RE = re.compile(r"^dvt_[0-9a-f]{32}$")
SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]+$")
MAINTENANCE_PROJECT = "codex-devcoveer-mcp"
MAINTENANCE_TITLE_MARKER = "DIRECT PROBES DEPLOY"
LIVE_SESSION_TYPES = frozenset({"busy", "retry"})
EXPECTED_BRIDGE_SHA256 = "a3ce51d6fc8d5fa892f6f54f34f672f878e25039b6a44a7399ccb1c55a45872d"
EXPECTED_BACKEND_SHA256 = "479f6707669011f99a008a9432de83b82d0bda70e6b0cb23473662cae6892f03"
EXPECTED_HEALTH_SHA256 = "24c0505153998be0b65424fdab209a96cc4ad8c23a724d913f0ba4ace41c6a17"
EXPECTED_SOURCE_HANDOFF_SHA256 = "265af62223aca453644a12945e1395ee66b533ae9af6da51003c613f98f8a585"
SOURCE_ROOT = Path(__file__).resolve().parents[1]
DIRECT_RUNTIME_SOURCES = {name: SOURCE_ROOT / "runtime" / name for name in DIRECT_RUNTIME_NAMES}
DIRECT_CONFIG_SOURCE = SOURCE_ROOT / "runtime" / "direct_probe_config.json"

def sha256(data: bytes) -> str: return hashlib.sha256(data).hexdigest()

def safe_bytes(path: Path, *, max_size: int = 3_000_000) -> bytes:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > max_size:
        raise SystemExit(f"unsafe or missing file: {path}")
    return path.read_bytes()

def safe_text(path: Path, *, max_size: int = 3_000_000) -> str:
    return safe_bytes(path, max_size=max_size).decode("utf-8")

def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1: raise SystemExit(f"bridge anchor {label!r} expected once, found {count}")
    return source.replace(old, new, 1)

def transform_bridge(source: str) -> str:
    if "PROJECT_PROBE_INPUT_SCHEMA" in source or 'name="project_probe"' in source:
        raise SystemExit("direct probes already appear to be installed; refusing ambiguous transform")
    import_anchor = """from source_handoff import (\n    SourceHandoffError,\n    cleanup_source_handoff as _cleanup_source_handoff,\n    resolve_source_handoff as _resolve_source_handoff,\n    stage_source_handoff as _stage_source_handoff,\n)\n"""
    import_block = import_anchor + """from direct_probes import (\n    HOST_ACTION_INPUT_SCHEMA,\n    HOST_PROBE_INPUT_SCHEMA,\n    PROJECT_PROBE_INPUT_SCHEMA,\n    ProbeError,\n    run_host_action as _run_host_action,\n    run_host_probe as _run_host_probe,\n    run_project_probe as _run_project_probe,\n)\n"""
    source = replace_once(source, import_anchor, import_block, "source_handoff import")
    tool_anchor = '    types.Tool(name="stage_source_handoff", title="Stage source handoff",\n'
    tool_block = '''    types.Tool(name="project_probe", title="Read deterministic project facts",\n        description=(\n            "Read bounded deterministic facts from an already resolved DevCoveer project without "\n            "starting OpenCode/Codex or creating a task. Use this first when the need is Git state, "\n            "remote HEAD, local history/tree/diff, exact source ranges/search/file metadata/hashes, "\n            "or narrowly scoped GitHub CI status. Use the existing agent path for research, reasoning "\n            "or execution rather than treating this as a shell. Checks are independent and partial "\n            "results are preserved."\n        ),\n        input_schema=PROJECT_PROBE_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,\n        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,\n            idempotent_hint=True, open_world_hint=True)),\n    types.Tool(name="host_probe", title="Read deterministic host/runtime facts",\n        description=(\n            "Read bounded facts only from server-registered services, listeners, logs, disk roots, "\n            "health endpoints, runtimes, installed operational files and config-presence keys. No "\n            "model is invoked and no task is created. Use this first for deterministic runtime facts; "\n            "use start_task/continue_task when investigation or execution is actually required. "\n            "Targets come only from trusted server configuration, never project source."\n        ),\n        input_schema=HOST_PROBE_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,\n        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,\n            idempotent_hint=True, open_world_hint=False)),\n    types.Tool(name="host_action", title="Run allowlisted host service actions",\n        description=(\n            "Run only server-registered low-risk user-service actions that are explicitly allowlisted " \n            "for the selected DevCoveer project. Supported operations are start, restart and enable_start; " \n            "there is no arbitrary shell, argv, path, environment, stop, disable or system-service surface. " \n            "Use this directly instead of creating an OpenCode task for routine service activation/restart " \n            "when the registered action is sufficient."\n        ),\n        input_schema=HOST_ACTION_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,\n        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,\n            idempotent_hint=False, open_world_hint=False)),\n'''
    source = replace_once(source, tool_anchor, tool_block + tool_anchor, "tool list")
    validator_anchor = '    "stage_source_handoff": Draft202012Validator(STAGE_SOURCE_HANDOFF_INPUT_SCHEMA),\n'
    source = replace_once(source, validator_anchor,
        '    "project_probe": Draft202012Validator(PROJECT_PROBE_INPUT_SCHEMA),\n    "host_probe": Draft202012Validator(HOST_PROBE_INPUT_SCHEMA),\n    "host_action": Draft202012Validator(HOST_ACTION_INPUT_SCHEMA),\n' + validator_anchor,
        "validator table")
    handler_anchor = '        if name == "stage_source_handoff":\n'
    handler_block = '''        if name == "project_probe":\n            assert project is not None\n            try:\n                return _tool_result(await _run_project_probe(\n                    project.path, project.canonical, arguments["checks"],\n                ))\n            except ProbeError as exc:\n                payload = {\n                    "status": exc.status,\n                    "errorCategory": exc.category,\n                    "message": str(exc),\n                }\n                if exc.exit_code is not None:\n                    payload["exitCode"] = exc.exit_code\n                return _tool_result(payload, error=True)\n\n        if name == "host_probe":\n            assert project is not None\n            try:\n                return _tool_result(await _run_host_probe(\n                    project.path, project.canonical, arguments["checks"],\n                ))\n            except ProbeError as exc:\n                payload = {\n                    "status": exc.status,\n                    "errorCategory": exc.category,\n                    "message": str(exc),\n                }\n                if exc.exit_code is not None:\n                    payload["exitCode"] = exc.exit_code\n                return _tool_result(payload, error=True)\n\n        if name == "host_action":\n            assert project is not None\n            try:\n                return _tool_result(await _run_host_action(\n                    project.path, project.canonical, arguments["actions"],\n                ))\n            except ProbeError as exc:\n                payload = {\n                    "status": exc.status,\n                    "errorCategory": exc.category,\n                    "message": str(exc),\n                }\n                if exc.exit_code is not None:\n                    payload["exitCode"] = exc.exit_code\n                return _tool_result(payload, error=True)\n\n'''
    return replace_once(source, handler_anchor, handler_block + handler_anchor, "handler dispatch")

def registry_active_records(root: Path = TASK_DIR) -> dict[str, dict]:
    records: dict[str, dict] = {}
    if not root.is_dir(): return records
    for path in root.glob("dvt_*.json"):
        if path.is_symlink() or path.stat().st_size > 2_000_000: continue
        try: record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError): continue
        task_id = str(record.get("taskId") or path.stem) if isinstance(record, dict) else ""
        if isinstance(record, dict) and record.get("status") in {"running", "cancelling"} and TASK_ID_RE.fullmatch(task_id):
            records[task_id] = record
    return records

def live_opencode_session_types(directories: list[str] | set[str]) -> dict[str, str]:
    bridge_python = Path("/home/dev/.local/share/openai-codex-mcp/bridge-venv/bin/python")
    if not bridge_python.is_file() or not BACKEND.is_file(): raise RuntimeError("OpenCode liveness probe unavailable")
    directories = sorted(directories)
    if not directories: return {}
    helper = """
import importlib.util, json, sys
from pathlib import Path
path=Path(sys.argv[1]); directories=json.loads(sys.argv[2]); sys.path.insert(0,str(path.parent))
spec=importlib.util.spec_from_file_location('probe_backend',path)
if spec is None or spec.loader is None: raise SystemExit(2)
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
backend=mod.OpenCodeBackend(); out={}
for directory in directories:
    payload=backend._request_sync('GET','/session/status',directory=directory,timeout=15.0)
    if not isinstance(payload,dict): raise SystemExit(3)
    for sid,state in payload.items():
        if isinstance(sid,str) and isinstance(state,dict) and state.get('type') in {'busy','retry','idle'}: out[sid]=state['type']
print(json.dumps(out,sort_keys=True,separators=(',',':')))
"""
    completed = subprocess.run(
        [str(bridge_python), "-c", helper, str(BACKEND), json.dumps(directories, separators=(",", ":"))],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        timeout=20 + 16 * len(directories), text=True,
    )
    if completed.returncode != 0: raise RuntimeError("OpenCode liveness probe failed")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict): raise RuntimeError("OpenCode liveness probe returned invalid state")
    return {str(k): str(v) for k, v in payload.items() if v in LIVE_SESSION_TYPES | {"idle"}}

def maintenance_gate(maintenance_task_id: str | None) -> dict[str, object]:
    if maintenance_task_id is not None and TASK_ID_RE.fullmatch(maintenance_task_id) is None: raise SystemExit("invalid --maintenance-task-id")
    records=registry_active_records()
    if not records:
        if maintenance_task_id is not None: raise SystemExit("maintenance task is not present in active registry")
        return {"active":[],"stale":[],"unverified":[],"excluded":None,"livenessError":None}
    directories=sorted({
        cwd for record in records.values()
        if record.get("backend") == "opencode"
        and isinstance((cwd := record.get("cwd")), str)
        and cwd.startswith("/")
    })
    try: states=live_opencode_session_types(directories)
    except (RuntimeError, ValueError, json.JSONDecodeError):
        return {"active":sorted(records),"stale":[],"unverified":sorted(records),"excluded":None,"livenessError":"session_status_unavailable"}
    active=[]; stale=[]; unverified=[]; excluded=None
    for task_id, record in sorted(records.items()):
        if record.get("backend") != "opencode": active.append(task_id); unverified.append(task_id); continue
        cwd=record.get("cwd")
        if not isinstance(cwd,str) or not cwd.startswith("/"): active.append(task_id); unverified.append(task_id); continue
        session_id=record.get("backendSessionId") or record.get("session")
        if not isinstance(session_id,str) or SESSION_ID_RE.fullmatch(session_id) is None: active.append(task_id); unverified.append(task_id); continue
        state=states.get(session_id)
        if state not in LIVE_SESSION_TYPES: stale.append(task_id); continue
        if task_id == maintenance_task_id:
            title=record.get("title")
            if record.get("project") != MAINTENANCE_PROJECT or not isinstance(title,str) or MAINTENANCE_TITLE_MARKER not in title:
                raise SystemExit("maintenance task failed self-exclusion validation")
            excluded=task_id; continue
        active.append(task_id)
    if maintenance_task_id is not None and excluded != maintenance_task_id: raise SystemExit("maintenance task is not a verified live deployment session")
    return {"active":active,"stale":stale,"unverified":unverified,"excluded":excluded,"livenessError":None}

def atomic_write_bytes(path: Path, data: bytes, mode: int) -> None:
    fd,tmp=tempfile.mkstemp(prefix=".direct-probes-",dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,"wb") as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass

def validate_config(data: bytes) -> None:
    try: payload=json.loads(data)
    except (ValueError,UnicodeError) as exc: raise SystemExit("direct probe config is invalid JSON") from exc
    if not isinstance(payload,dict) or payload.get("version") != 1 or not isinstance(payload.get("projects"),dict):
        raise SystemExit("direct probe config has unsupported shape")
    for project,binding in payload["projects"].items():
        if not isinstance(project,str) or not isinstance(binding,dict): raise SystemExit("invalid direct probe project binding")
        for section in ("services","logs","roots","health","runtimes","runtime_files","config_presence"):
            if section in binding and not isinstance(binding[section],dict): raise SystemExit(f"invalid config section {section!r} for {project}")

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--apply",action="store_true"); ap.add_argument("--maintenance-task-id"); args=ap.parse_args()
    runtime_modules={name:safe_bytes(path) for name,path in DIRECT_RUNTIME_SOURCES.items()}
    config_data=safe_bytes(DIRECT_CONFIG_SOURCE,max_size=500_000); validate_config(config_data)
    lineage={"bridge":(BRIDGE,EXPECTED_BRIDGE_SHA256),"backend":(BACKEND,EXPECTED_BACKEND_SHA256),"health":(HEALTH,EXPECTED_HEALTH_SHA256),"source_handoff":(SOURCE_HANDOFF,EXPECTED_SOURCE_HANDOFF_SHA256)}
    observed={}
    for name,(path,expected) in lineage.items():
        digest=sha256(safe_bytes(path)); observed[name]=digest
        if digest != expected: raise SystemExit(f"installed {name} SHA-256 {digest} does not match expected lineage {expected}")
    current_bridge=safe_text(BRIDGE); candidate=transform_bridge(current_bridge); candidate_bytes=candidate.encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="devcoveer-direct-probes-") as tmp:
        td=Path(tmp); (td/"codex-chatgpt-bridge.py").write_bytes(candidate_bytes)
        for name,data in runtime_modules.items(): (td/name).write_bytes(data)
        (td/"direct_probe_config.json").write_bytes(config_data)
        for existing in (BACKEND,HEALTH,SOURCE_HANDOFF,INSTALL_DIR/"session_evidence.py",INSTALL_DIR/"dataset_loop_operator.py"):
            if existing.is_file(): shutil.copy2(existing,td/existing.name)
        for py in td.glob("*.py"): py_compile.compile(str(py),doraise=True)
    gate=maintenance_gate(args.maintenance_task_id)
    report={"targetIntegration":"direct-probes-v1","applyRequested":args.apply,"candidateCompiles":True,"bridgeOldSha256":observed["bridge"],"bridgeNewSha256":sha256(candidate_bytes),"backendSha256":observed["backend"],"healthSha256":observed["health"],"sourceHandoffSha256":observed["source_handoff"],"directRuntimeSha256":{name:sha256(data) for name,data in runtime_modules.items()},"directConfigSha256":sha256(config_data),"tools":["project_probe","host_probe","host_action"],"maintenanceGate":gate}
    print(json.dumps(report,sort_keys=True))
    if not args.apply: return 0
    if gate["active"]: raise SystemExit("live or unverified DevCoveer tasks exist; deployment/restart is blocked")
    if args.maintenance_task_id is not None and gate["excluded"] != args.maintenance_task_id: raise SystemExit("maintenance task self-exclusion was not verified")
    stamp=time.strftime("%Y%m%dT%H%M%SZ",time.gmtime()); backups={}; touched=(BRIDGE,*DIRECT_RUNTIME_TARGETS.values(),DIRECT_CONFIG)
    for path in touched:
        if path.exists():
            backup=path.with_name(path.name+f".bak.direct-probes-{stamp}"); shutil.copy2(path,backup); backups[path]=backup
        else: backups[path]=None
    try:
        atomic_write_bytes(BRIDGE,candidate_bytes,0o755 if os.access(BRIDGE,os.X_OK) else 0o600)
        for name,data in runtime_modules.items(): atomic_write_bytes(DIRECT_RUNTIME_TARGETS[name],data,0o600)
        atomic_write_bytes(DIRECT_CONFIG,config_data,0o600); py_compile.compile(str(BRIDGE),doraise=True)
        for target in DIRECT_RUNTIME_TARGETS.values(): py_compile.compile(str(target),doraise=True)
    except BaseException:
        for path,backup in backups.items():
            if backup is None:
                try: path.unlink()
                except FileNotFoundError: pass
            else: shutil.copy2(backup,path)
        raise
    print(json.dumps({"status":"installed","restartRequired":True,"bridgeSha256":sha256(BRIDGE.read_bytes()),"directRuntimeSha256":{name:sha256(path.read_bytes()) for name,path in DIRECT_RUNTIME_TARGETS.items()},"directConfigSha256":sha256(DIRECT_CONFIG.read_bytes()),"backups":{str(path):(backup.name if backup else None) for path,backup in backups.items()}},sort_keys=True))
    return 0

if __name__ == "__main__": raise SystemExit(main())
