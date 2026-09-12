#!/usr/bin/env python3
"""Fail-closed installer for Source Handoff integration into DevCoveer MCP bridge.

This installer adds stage_source_handoff and cleanup_source_handoff methods
to the existing v3.10 production bridge. It follows the same fail-closed
pattern as install-adaptive-routing-v3.10.py.

Default mode is read-only verification. --apply performs the atomic install.
The installer never restarts services, never reads provider credentials,
and checks active tasks before installation.
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
from typing import Any

INSTALL_DIR = Path("/home/dev/.local/libexec/openai-codex-mcp")
BRIDGE = INSTALL_DIR / "codex-chatgpt-bridge.py"
BACKEND = INSTALL_DIR / "opencode_backend.py"
HEALTH = INSTALL_DIR / "model_health.py"
SOURCE_HANDOFF = INSTALL_DIR / "source_handoff.py"
TASK_DIR = Path("/home/dev/.local/share/openai-codex-mcp/tasks")
TASK_ID_RE = re.compile(r"^dvt_[0-9a-f]{32}$")
SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]+$")
MAINTENANCE_PROJECT = "devcoveer-opencode-smoke-fixture"
MAINTENANCE_TITLE_MARKER = "SOURCE HANDOFF INSTALL"
LIVE_SESSION_TYPES = frozenset({"busy", "retry"})

# Exact SHA-256 of the current production bridge (v3.10.0 with adaptive routing)
EXPECTED_BRIDGE_SHA256 = "8bfbbb7003e916bffd6cf4b37b1705295ecf8fd428c0ada3523ed2a19d17c12c"
EXPECTED_BACKEND_SHA256 = "479f6707669011f99a008a9432de83b82d0bda70e6b0cb23473662cae6892f03"
EXPECTED_HEALTH_SHA256 = "24c0505153998be0b65424fdab209a96cc4ad8c23a724d913f0ba4ace41c6a17"

# Source handoff module from the source-controlled repo
SOURCE_HANDOFF_MODULE = Path(__file__).resolve().parents[1] / "runtime" / "source_handoff.py"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(path: Path) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
        raise SystemExit(f"unsafe or missing file: {path}")
    return path.read_text(encoding="utf-8")


def atomic_write(path: Path, text: str, mode: int) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".source-handoff-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def registry_active_records(root: Path = TASK_DIR) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return result
    for path in root.glob("dvt_*.json"):
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(record, dict) or record.get("status") not in {"running", "cancelling"}:
            continue
        task_id = str(record.get("taskId") or path.stem)
        if TASK_ID_RE.fullmatch(task_id):
            result[task_id] = record
    return result


def live_opencode_session_types() -> dict[str, str]:
    bridge_python = Path("/home/dev/.local/share/openai-codex-mcp/bridge-venv/bin/python")
    if not bridge_python.is_file() or not BACKEND.is_file():
        raise RuntimeError("localhost OpenCode liveness probe is unavailable")
    helper = r'''
import importlib.util
import json
import sys
from pathlib import Path

backend_path = Path(sys.argv[1])
sys.path.insert(0, str(backend_path.parent))
spec = importlib.util.spec_from_file_location("devcoveer_installed_backend", backend_path)
if spec is None or spec.loader is None:
    raise SystemExit(2)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
backend = module.OpenCodeBackend()
statuses = backend._request_sync("GET", "/session/status", timeout=15.0)
if not isinstance(statuses, dict):
    raise SystemExit(3)
out = {}
for session_id, state in statuses.items():
    if not isinstance(session_id, str) or not isinstance(state, dict):
        continue
    state_type = state.get("type")
    if state_type in {"busy", "retry", "idle"}:
        out[session_id] = state_type
print(json.dumps(out, sort_keys=True, separators=(",", ":")))
'''
    completed = subprocess.run(
        [str(bridge_python), "-c", helper, str(BACKEND)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=20,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("localhost OpenCode liveness probe failed")
    try:
        payload = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("localhost OpenCode liveness probe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("localhost OpenCode liveness probe returned invalid state")
    return {
        str(session_id): str(state_type)
        for session_id, state_type in payload.items()
        if isinstance(session_id, str) and state_type in {"busy", "retry", "idle"}
    }


def classify_active_records(
    records: dict[str, dict[str, Any]],
    session_types: dict[str, str],
    *,
    maintenance_task_id: str | None = None,
) -> tuple[list[str], list[str], list[str], str | None]:
    if maintenance_task_id is not None and TASK_ID_RE.fullmatch(maintenance_task_id) is None:
        raise SystemExit("invalid --maintenance-task-id")

    active: list[str] = []
    stale: list[str] = []
    unverified: list[str] = []
    maintenance_excluded: str | None = None

    for task_id, record in sorted(records.items()):
        if record.get("backend") != "opencode":
            active.append(task_id)
            unverified.append(task_id)
            continue
        session_id = record.get("session")
        if not isinstance(session_id, str) or SESSION_ID_RE.fullmatch(session_id) is None:
            active.append(task_id)
            unverified.append(task_id)
            continue
        state_type = session_types.get(session_id)
        if state_type not in LIVE_SESSION_TYPES:
            stale.append(task_id)
            continue

        if task_id == maintenance_task_id:
            title = record.get("title")
            if (
                record.get("status") != "running"
                or record.get("project") != MAINTENANCE_PROJECT
                or not isinstance(title, str)
                or MAINTENANCE_TITLE_MARKER not in title
            ):
                raise SystemExit("maintenance task failed self-exclusion validation")
            maintenance_excluded = task_id
            continue
        active.append(task_id)

    if maintenance_task_id is not None and maintenance_excluded != maintenance_task_id:
        raise SystemExit("maintenance task is not a verified live maintenance session")
    return active, stale, unverified, maintenance_excluded


def maintenance_gate(
    *, maintenance_task_id: str | None = None,
) -> tuple[list[str], list[str], list[str], str | None, str | None]:
    records = registry_active_records()
    if not records:
        if maintenance_task_id is not None:
            raise SystemExit("maintenance task is not present in the active registry")
        return [], [], [], None, None
    try:
        session_types = live_opencode_session_types()
    except RuntimeError:
        return sorted(records), [], sorted(records), None, "session_status_unavailable"
    active, stale, unverified, excluded = classify_active_records(
        records,
        session_types,
        maintenance_task_id=maintenance_task_id,
    )
    return active, stale, unverified, excluded, None


# --- Bridge Integration (single-pass) ---

def _apply_source_handoff_to_bridge(bridge_source: str) -> str:
    """Apply all source handoff changes to the bridge source in a single pass."""
    lines = bridge_source.splitlines(keepends=True)
    result: list[str] = []

    for i, line in enumerate(lines):
        # 1. Add import after session_evidence import (0-indexed 58 = line 59)
        if i == 58:
            result.append(line)
            result.append("\n")
            result.append("from source_handoff import (\n")
            result.append("    SourceHandoffError,\n")
            result.append("    cleanup_source_handoff as _cleanup_source_handoff,\n")
            result.append("    resolve_source_handoff as _resolve_source_handoff,\n")
            result.append("    stage_source_handoff as _stage_source_handoff,\n")
            result.append(")\n")
            continue

        # 2. Add schemas after DATASET_LOOP_EMPTY_INPUT_SCHEMA closing (0-indexed 377 = line 378)
        if i == 377:
            result.append(line)
            result.append("\n")
            result.append("STAGE_SOURCE_HANDOFF_INPUT_SCHEMA: dict[str, Any] = {\n")
            result.append('    "type": "object",\n')
            result.append('    "properties": {\n')
            result.append('        "project": {\n')
            result.append('            "type": "string", "minLength": 1, "maxLength": 128,\n')
            result.append('            "description": "Existing DevCoveer project identifier; never an arbitrary filesystem path.",\n')
            result.append('        },\n')
            result.append('        "base_sha": {\n')
            result.append('            "type": "string", "pattern": r"^[0-9a-f]{40}$",\n')
            result.append('            "description": "Exact 40-hex Git commit SHA used to produce the patch.",\n')
            result.append('        },\n')
            result.append('        "patch": {\n')
            result.append('            "type": "string", "minLength": 1,\n')
            result.append('            "description": (\n')
            result.append('                "Output equivalent to `git diff --cached --binary <base_sha>`. "\n')
            result.append('                "The patch is consumed through stdin and is never persisted."\n')
            result.append('            ),\n')
            result.append('        },\n')
            result.append('        "patch_sha256": {\n')
            result.append('            "type": "string", "pattern": r"^[0-9a-f]{64}$",\n')
            result.append('            "description": "Exact SHA-256 of the UTF-8 patch bytes.",\n')
            result.append('        },\n')
            result.append('    },\n')
            result.append('    "required": ["project", "base_sha", "patch", "patch_sha256"],\n')
            result.append('    "additionalProperties": False,\n')
            result.append("}\n")
            result.append("\n")
            result.append("CLEANUP_SOURCE_HANDOFF_INPUT_SCHEMA: dict[str, Any] = {\n")
            result.append('    "type": "object",\n')
            result.append('    "properties": {\n')
            result.append('        "handoff_id": {\n')
            result.append('            "type": "string", "pattern": r"^src_[0-9a-f]{24}$",\n')
            result.append('            "description": "The source handoff ID returned by stage_source_handoff.",\n')
            result.append('        },\n')
            result.append('    },\n')
            result.append('    "required": ["handoff_id"],\n')
            result.append('    "additionalProperties": False,\n')
            result.append("}\n")
            continue

        # 3. Add tool declarations after record_task_review tool (0-indexed 583 = line 584)
        if i == 583:
            result.append(line)
            result.append("\n")
            result.append('    types.Tool(name="stage_source_handoff", title="Stage source handoff",\n')
            result.append('        description=(\n')
            result.append('            "Materialize a Git patch in an isolated detached worktree. The patch must be "\n')
            result.append('            "equivalent to `git diff --cached --binary <base_sha>`. The project must be an "\n')
            result.append('            "existing DevCoveer project identifier. The patch is consumed through stdin and "\n')
            result.append('            "is never persisted to disk. Returns a handoff:<id> project identifier for use "\n')
            result.append('            "with start_task."\n')
            result.append('        ),\n')
            result.append('        input_schema=STAGE_SOURCE_HANDOFF_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,\n')
            result.append('        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,\n')
            result.append('            idempotent_hint=True, open_world_hint=False)),\n')
            result.append('    types.Tool(name="cleanup_source_handoff", title="Cleanup source handoff",\n')
            result.append('        description=(\n')
            result.append('            "Remove a managed source handoff worktree and its metadata. Call this after "\n')
            result.append('            "the resulting code has been pushed/accepted and no further continue_task is "\n')
            result.append('            "needed. After cleanup, the workspace and all transport artifacts are gone."\n')
            result.append('        ),\n')
            result.append('        input_schema=CLEANUP_SOURCE_HANDOFF_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,\n')
            result.append('        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=True,\n')
            result.append('            idempotent_hint=True, open_world_hint=False)),\n')
            continue

        # 4. Add validators after record_task_review entry (0-indexed 608 = line 609)
        if i == 608:
            result.append(line)
            result.append('    "stage_source_handoff": Draft202012Validator(STAGE_SOURCE_HANDOFF_INPUT_SCHEMA),\n')
            result.append('    "cleanup_source_handoff": Draft202012Validator(CLEANUP_SOURCE_HANDOFF_INPUT_SCHEMA),\n')
            continue

        # 5. Add handler before council_run (0-indexed 3554 = line 3555)
        if i == 3554:
            result.append("        if name == \"stage_source_handoff\":\n")
            result.append("            assert project is not None\n")
            result.append("            try:\n")
            result.append("                result = _stage_source_handoff(\n")
            result.append("                    project_root=project.path,\n")
            result.append("                    project_id=project.canonical,\n")
            result.append("                    base_sha=arguments[\"base_sha\"],\n")
            result.append("                    patch=arguments[\"patch\"],\n")
            result.append("                    patch_sha256=arguments[\"patch_sha256\"],\n")
            result.append("                )\n")
            result.append("            except SourceHandoffError as exc:\n")
            result.append("                return _tool_result({\"status\": \"failed\", \"errorCategory\": \"source_handoff\",\n")
            result.append("                    \"content\": str(exc)}, error=True)\n")
            result.append("            return _tool_result(result)\n")
            result.append("\n")
            result.append("        if name == \"cleanup_source_handoff\":\n")
            result.append("            try:\n")
            result.append("                result = _cleanup_source_handoff(arguments[\"handoff_id\"])\n")
            result.append("            except SourceHandoffError as exc:\n")
            result.append("                return _tool_result({\"status\": \"failed\", \"errorCategory\": \"source_handoff\",\n")
            result.append("                    \"content\": str(exc)}, error=True)\n")
            result.append("            return _tool_result(result)\n")
            result.append("\n")
            result.append(line)
            continue

        # 6. Extend _project_selection (0-indexed 2179 = line 2180)
        if i == 2179:
            result.append(line)
            result.append("    if raw_hint.startswith(\"handoff:\"):\n")
            result.append("        handoff_id = raw_hint.removeprefix(\"handoff:\")\n")
            result.append("        try:\n")
            result.append("            workspace = _resolve_source_handoff(handoff_id)\n")
            result.append("        except Exception:\n")
            result.append("            return None, {\"status\": \"not_found\", \"matchType\": \"handoff\",\n")
            result.append("                \"message\": f\"Source handoff {handoff_id!r} is invalid or expired.\",\n")
            result.append("                \"candidates\": []}\n")
            result.append("        entry = ProjectEntry(\n")
            result.append("            canonical=raw_hint,\n")
            result.append("            directory_name=workspace.name,\n")
            result.append("            path=str(workspace),\n")
            result.append("        )\n")
            result.append("        return entry, {\"status\": \"resolved\", \"matchType\": \"handoff\",\n")
            result.append("            \"project\": raw_hint, \"cwd\": str(workspace), \"candidates\": []}\n")
            result.append("\n")
            continue

        result.append(line)

    return "".join(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--maintenance-task-id",
        help="exact live DevCoveer maintenance dvt task to self-exclude",
    )
    args = parser.parse_args()

    # Verify source handoff module exists
    if not SOURCE_HANDOFF_MODULE.is_file():
        raise SystemExit(f"source handoff module not found: {SOURCE_HANDOFF_MODULE}")
    source_handoff_source = read_text(SOURCE_HANDOFF_MODULE)
    source_handoff_sha = sha256_bytes(source_handoff_source.encode("utf-8"))

    # Read current production bridge
    current_bridge = read_text(BRIDGE)
    current_bridge_sha = sha256_bytes(current_bridge.encode("utf-8"))
    if current_bridge_sha != EXPECTED_BRIDGE_SHA256:
        raise SystemExit(
            f"installed bridge SHA-256 ({current_bridge_sha}) does not match "
            f"expected ({EXPECTED_BRIDGE_SHA256}); bridge may have been modified"
        )

    # Read current production backend
    current_backend = BACKEND.read_bytes()
    backend_sha = sha256_bytes(current_backend)
    if backend_sha != EXPECTED_BACKEND_SHA256:
        raise SystemExit("installed backend does not match expected lineage")

    # Read current model_health
    current_health = read_text(HEALTH)
    health_sha = sha256_bytes(current_health.encode("utf-8"))
    if health_sha != EXPECTED_HEALTH_SHA256:
        raise SystemExit("installed model_health.py does not match expected lineage")

    # Generate candidate bridge with source handoff integration
    candidate_bridge = _apply_source_handoff_to_bridge(current_bridge)
    candidate_bridge_sha = sha256_bytes(candidate_bridge.encode("utf-8"))

    # Verify candidate compiles
    with tempfile.TemporaryDirectory(prefix="devcoveer-source-handoff-") as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "codex-chatgpt-bridge.py").write_text(candidate_bridge, encoding="utf-8")
        (tmpdir / "source_handoff.py").write_text(source_handoff_source, encoding="utf-8")
        (tmpdir / "opencode_backend.py").write_bytes(current_backend)
        (tmpdir / "model_health.py").write_text(current_health, encoding="utf-8")
        for module_name in ("session_evidence.py", "dataset_loop_operator.py"):
            src = INSTALL_DIR / module_name
            if src.is_file():
                shutil.copy2(src, tmpdir / module_name)
        for py_file in tmpdir.glob("*.py"):
            py_compile.compile(str(py_file), doraise=True)

    # Maintenance gate
    active, stale, unverified, maintenance_excluded, liveness_error = maintenance_gate(
        maintenance_task_id=args.maintenance_task_id,
    )

    report = {
        "targetIntegration": "source-handoff-v1",
        "bridgeOldSha256": current_bridge_sha,
        "bridgeNewSha256": candidate_bridge_sha,
        "backendSha256": backend_sha,
        "healthSha256": health_sha,
        "sourceHandoffModuleSha256": source_handoff_sha,
        "candidateCompiles": True,
        "activeTasks": active,
        "staleRegistryTasks": stale,
        "unverifiedTasks": unverified,
        "maintenanceTaskExcluded": maintenance_excluded,
        "livenessCheckError": liveness_error,
        "applyRequested": args.apply,
    }
    print(json.dumps(report, sort_keys=True))
    if not args.apply:
        return 0
    if active:
        raise SystemExit("live or unverified DevCoveer tasks exist; inspect them before installation/restart")
    if args.maintenance_task_id is not None and maintenance_excluded != args.maintenance_task_id:
        raise SystemExit("maintenance task self-exclusion was not verified")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backups = []
    for path in (BRIDGE,):
        backup = path.with_name(path.name + f".bak.source-handoff-{stamp}")
        shutil.copy2(path, backup)
        backups.append(backup)

    try:
        atomic_write(BRIDGE, candidate_bridge, 0o755 if os.access(BRIDGE, os.X_OK) else 0o600)
        atomic_write(SOURCE_HANDOFF, source_handoff_source, 0o600)
        py_compile.compile(str(BRIDGE), doraise=True)
        py_compile.compile(str(SOURCE_HANDOFF), doraise=True)
    except BaseException:
        if backups:
            shutil.copy2(backups[0], BRIDGE)
        raise

    print(json.dumps({
        "status": "installed",
        "restartRequired": True,
        "backups": [p.name for p in backups],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
