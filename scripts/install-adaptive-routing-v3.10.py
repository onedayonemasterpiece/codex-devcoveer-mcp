#!/usr/bin/env python3
"""Fail-closed installer for DevCoveer adaptive OpenCode routing v3.10.

This script intentionally targets one known v3.9 production lineage. It never reads
provider credentials or project .env files. Default mode is verification-only; pass
--apply to install after all preconditions succeed. Service restart is deliberately
left to the external deployment/acceptance step.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import py_compile
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

INSTALL_DIR = Path("/home/dev/.local/libexec/openai-codex-mcp")
BRIDGE = INSTALL_DIR / "codex-chatgpt-bridge.py"
BACKEND = INSTALL_DIR / "opencode_backend.py"
HEALTH = INSTALL_DIR / "model_health.py"
EXPECTED_BACKEND_SHA256 = "c4a152e5c6b44e0e6ca1899aec4ad221b1ec38b5f8578827d67d55f4dc007864"
EXPECTED_NEW_BACKEND_SHA256 = "2243d37d89605817db9cd8c19179ac8769851fdf688e2d201daca5359c10c63d"
EXPECTED_MODEL_DISCOVERY_SHA256 = "14903373ede193e18ca9fbcef92bb77ab23c5b99060d81a8e1ebd6551bcbee1a"
EXPECTED_READ_TASK_SHA256 = "26e8ce7d2232e3a4298d57397f162124268f5d9afbe4e24f655bf398c1a2395c"
OLD_VERSION = 'BRIDGE_VERSION = "3.9.0"'
NEW_VERSION = 'BRIDGE_VERSION = "3.10.0"'

CURRENT_READ_TOOL = '''    types.Tool(name="read_task", title="Read Codex task",
        description=("Check a background task and read its saved native Codex result without "
            "resuming or changing it. The task may be latest, a full/partial ID, or a name "
            "fragment. Default summary returns runtime status, errors, and final agent result; "
            "use detail=full only when the complete transcript is explicitly requested. "
            "For terminal OpenCode tasks, the response includes executionStatus, "
            "acceptanceStatus, and sessionEvidenceAvailable. Do not tell the user that an "
            "OpenCode task is successfully completed until terminal session evidence has been "
            "reviewed and an acceptance verdict has been recorded via record_task_review."),
        input_schema=READ_TASK_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False)),'''

ADAPTIVE_READ_TOOL = '''    types.Tool(name="read_task", title="Read Codex task",
        description=("Check a background task and read its saved result without resuming or changing it. "
            "For running OpenCode tasks the summary includes safe runtime/progress diagnostics; "
            "detail=full adds a bounded redacted live session snapshot for stall diagnosis. "
            "Before cancelling a slow OpenCode task, inspect this live snapshot and progress timing; "
            "do not infer a stall from status=running alone. For terminal OpenCode tasks, the response "
            "includes executionStatus, acceptanceStatus, and sessionEvidenceAvailable. Do not tell the "
            "user that an OpenCode task is successfully completed until terminal session evidence has "
            "been reviewed and an acceptance verdict has been recorded via record_task_review."),
        input_schema=READ_TASK_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False)),'''

CURRENT_CANCEL_TOOL_TEXT = '''        description=("Interrupt the active turn of one uniquely resolved Codex task without "
            "deleting its thread or history. After cancellation, use continue_task to resume "
            "the same task with corrected instructions. Calling this for a terminal task is "
            "an idempotent no-op."),'''
ADAPTIVE_CANCEL_TOOL_TEXT = '''        description=("Interrupt the active turn of one uniquely resolved task without deleting its "
            "thread or history. For a slow running OpenCode task, first use read_task(detail=full) "
            "and inspect real progress/provider state; never cancel solely because it has been running "
            "for a long time. After cancellation, use continue_task on the same task and, when useful, "
            "select a healthier model/provider. Calling this for a terminal task is an idempotent no-op."),'''

CURRENT_LIST_TOOL_TEXT = '''        description=("List provider-filtered models available to DevCoveer. Codex models come "
            "from the live native app-server catalog. OpenCode Zen models come from the live "
            "OpenCode catalog. verified_only=false (default) lists the current connected catalog; "
            "verified_only=true applies available verification receipts. This tool never performs "
            "inference or exposes credentials. NVIDIA entries are always marked limited/non-free, "
            "and the default free OpenCode model is explicitly identified."),'''
ADAPTIVE_LIST_TOOL_TEXT = '''        description=("List provider-filtered models available to DevCoveer with short-horizon runtime "
            "health, active usage and routing suggestions. Historical verification receipts are kept "
            "separate from current availability, so fresh quota/429/timeout/EOL observations override "
            "stale verified status. Default OpenCode resource priority is Zen, then NVIDIA, then metered "
            "reserve providers such as GigaChat; NVIDIA is non-scarce but dynamically loaded. Listing "
            "never performs inference or exposes credentials and never applies a global model lock."),'''


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(path: Path) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
        raise SystemExit(f"unsafe or missing file: {path}")
    return path.read_text(encoding="utf-8")


def function_block(source: str, name: str) -> tuple[int, int, str]:
    marker = f"async def {name}("
    start = source.find(marker)
    if start < 0:
        raise SystemExit(f"missing function: {name}")
    next_async = source.find("\n\nasync def ", start + len(marker))
    next_sync = source.find("\n\ndef ", start + len(marker))
    candidates = [x for x in (next_async, next_sync) if x >= 0]
    end = min(candidates) if candidates else len(source)
    block = source[start:end].rstrip() + "\n"
    return start, end, block


def atomic_write(path: Path, text: str, mode: int) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".adaptive-", dir=path.parent)
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


def active_tasks() -> list[str]:
    root = Path("/home/dev/.local/share/openai-codex-mcp/tasks")
    result: list[str] = []
    if not root.is_dir():
        return result
    for path in root.glob("dvt_*.json"):
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if record.get("status") in {"running", "cancelling"}:
            result.append(str(record.get("taskId") or path.stem))
    return sorted(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-active-tasks", action="store_true",
                        help="install files but still do not restart services; intended only for controlled maintenance")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    new_health = read_text(repo_root / "runtime" / "model_health.py")
    new_discovery = read_text(repo_root / "runtime" / "bridge_fragments" / "model_discovery_adaptive.py")
    new_read_task = read_text(repo_root / "runtime" / "bridge_fragments" / "read_opencode_task_adaptive.py")
    backend_patch = (
        read_text(repo_root / "patches" / "opencode_backend_adaptive.patch.1")
        + read_text(repo_root / "patches" / "opencode_backend_adaptive.patch.2")
    )

    current_backend = BACKEND.read_bytes()
    if sha256_bytes(current_backend) != EXPECTED_BACKEND_SHA256:
        raise SystemExit("installed opencode_backend.py does not match approved v3.9 lineage")
    patch_binary = shutil.which("patch")
    if not patch_binary:
        raise SystemExit("system patch utility is unavailable")
    with tempfile.TemporaryDirectory(prefix="devcoveer-backend-patch-") as tmp:
        tmpdir = Path(tmp)
        candidate = tmpdir / "opencode_backend.py"
        candidate.write_bytes(current_backend)
        completed = subprocess.run(
            [patch_binary, "--batch", "--forward", "--silent", "-p0"],
            input=backend_patch.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmpdir, check=False, timeout=30,
        )
        if completed.returncode != 0:
            raise SystemExit("approved OpenCode backend patch did not apply cleanly")
        new_backend = candidate.read_text(encoding="utf-8")
    if sha256_bytes(new_backend.encode("utf-8")) != EXPECTED_NEW_BACKEND_SHA256:
        raise SystemExit("patched OpenCode backend does not match approved v3.10 candidate hash")
    bridge = read_text(BRIDGE)
    if bridge.count(OLD_VERSION) != 1:
        raise SystemExit("installed bridge is not the approved v3.9.0 target")

    d_start, d_end, old_discovery = function_block(bridge, "_model_discovery_payload")
    if sha256_bytes(old_discovery.encode()) != EXPECTED_MODEL_DISCOVERY_SHA256:
        raise SystemExit("model discovery function does not match approved v3.9 lineage")
    bridge = bridge[:d_start] + new_discovery.rstrip() + bridge[d_end:]

    r_start, r_end, old_read = function_block(bridge, "_read_opencode_task")
    if sha256_bytes(old_read.encode()) != EXPECTED_READ_TASK_SHA256:
        raise SystemExit("read_task function does not match approved v3.9 acceptance-gate lineage")
    bridge = bridge[:r_start] + new_read_task.rstrip() + bridge[r_end:]

    for old, new, label in (
        (CURRENT_READ_TOOL, ADAPTIVE_READ_TOOL, "read_task tool description"),
        (CURRENT_CANCEL_TOOL_TEXT, ADAPTIVE_CANCEL_TOOL_TEXT, "cancel_task tool description"),
        (CURRENT_LIST_TOOL_TEXT, ADAPTIVE_LIST_TOOL_TEXT, "list_models tool description"),
    ):
        if bridge.count(old) != 1:
            raise SystemExit(f"{label} does not match approved v3.9 schema")
        bridge = bridge.replace(old, new, 1)
    bridge = bridge.replace(OLD_VERSION, NEW_VERSION, 1)

    with tempfile.TemporaryDirectory(prefix="devcoveer-adaptive-") as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "model_health.py").write_text(new_health, encoding="utf-8")
        (tmpdir / "opencode_backend.py").write_text(new_backend, encoding="utf-8")
        (tmpdir / "codex-chatgpt-bridge.py").write_text(bridge, encoding="utf-8")
        for path in tmpdir.iterdir():
            py_compile.compile(str(path), doraise=True)

    active = active_tasks()
    report = {
        "targetBridgeVersion": "3.10.0",
        "backendOldSha256": EXPECTED_BACKEND_SHA256,
        "backendNewSha256": sha256_bytes(new_backend.encode()),
        "backendExpectedNewSha256": EXPECTED_NEW_BACKEND_SHA256,
        "bridgeNewSha256": sha256_bytes(bridge.encode()),
        "activeTasks": active,
        "applyRequested": args.apply,
    }
    print(json.dumps(report, sort_keys=True))
    if not args.apply:
        return 0
    if active and not args.allow_active_tasks:
        raise SystemExit("active DevCoveer tasks exist; inspect them before installation/restart")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backups = []
    for path in (BACKEND, BRIDGE):
        backup = path.with_name(path.name + f".bak.adaptive-{stamp}")
        shutil.copy2(path, backup)
        backups.append(backup)
    try:
        atomic_write(HEALTH, new_health, 0o600)
        atomic_write(BACKEND, new_backend, 0o755 if os.access(BACKEND, os.X_OK) else 0o600)
        atomic_write(BRIDGE, bridge, 0o755 if os.access(BRIDGE, os.X_OK) else 0o600)
        for path in (HEALTH, BACKEND, BRIDGE):
            py_compile.compile(str(path), doraise=True)
    except BaseException:
        shutil.copy2(backups[0], BACKEND)
        shutil.copy2(backups[1], BRIDGE)
        raise
    print(json.dumps({"status": "installed", "restartRequired": True,
                      "backups": [p.name for p in backups]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
