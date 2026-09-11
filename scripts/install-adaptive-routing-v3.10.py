#!/usr/bin/env python3
"""Fail-closed installer for DevCoveer adaptive OpenCode routing v3.10.

The installer targets the exact production 3.9 runtime observed on 2026-09-11.
Default mode is read-only verification. During candidate re-baselining, --apply is
refused until every generated candidate hash is explicitly pinned in this file.
The installer never restarts services and never reads provider credentials or
project .env files.
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

# Exact live lineage reconciled from the host on 2026-09-11.
EXPECTED_BACKEND_SHA256 = "0b769ac4aae681345d6857b0322c8dddb76a499fbb86986bddc39b7ace68e902"
EXPECTED_BRIDGE_SOURCE_SHA256 = "2006a60b5bdbbed9b6892ce9c0308512a5fc31dceb9fa197726d74157aac57ea"
EXPECTED_MODEL_DISCOVERY_SHA256 = "14903373ede193e18ca9fbcef92bb77ab23c5b99060d81a8e1ebd6551bcbee1a"
EXPECTED_READ_TASK_SHA256 = "ccc473d2c50c519ce979c1cee7a3235f4a7ca59890cb6e67abdfa69566f00275"
EXPECTED_PATCH1_SHA256 = "e325dfa1e8575cd42f25a84d7b535332c2207da349b404db9bfffe3eb20e2829"
EXPECTED_CANCEL_PATCH_SHA256 = "4b4f3ed187d084860613a0232afb1e33c6a01c60f706e46be73c7d31c4be07c5"

# Bootstrap gate: dry-run computes these from the exact live lineage. They MUST
# be replaced by exact SHA-256 values and re-verified before --apply is allowed.
EXPECTED_PATCH2_SHA256: str | None = "69c782babbc234caa6aff16fcd8982e5e195c2b2d03a51c94d4fe7a717eb3918"
EXPECTED_ROUTING_BACKEND_SHA256: str | None = "7313917544890dac03934fb0da53047012d300faafbfd5a8445b3122bfe112a2"
EXPECTED_NEW_BACKEND_SHA256: str | None = "479f6707669011f99a008a9432de83b82d0bda70e6b0cb23473662cae6892f03"
EXPECTED_NEW_BRIDGE_SHA256: str | None = "8bfbbb7003e916bffd6cf4b37b1705295ecf8fd428c0ada3523ed2a19d17c12c"

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


def _apply_patch(patch_binary: str, tmpdir: Path, patch_text: str, label: str) -> None:
    completed = subprocess.run(
        [patch_binary, "--batch", "--forward", "--silent", "-p0"],
        input=patch_text.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmpdir,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:500]
        suffix = f": {detail}" if detail else ""
        raise SystemExit(f"approved {label} did not apply cleanly{suffix}")


def _check_optional_pin(actual: str, expected: str | None, label: str) -> None:
    if expected is not None and actual != expected:
        raise SystemExit(f"{label} does not match approved source")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    new_health = read_text(repo_root / "runtime" / "model_health.py")
    new_discovery = read_text(repo_root / "runtime" / "bridge_fragments" / "model_discovery_adaptive.py")
    new_read_task = read_text(repo_root / "runtime" / "bridge_fragments" / "read_opencode_task_adaptive.py")
    patch1 = read_text(repo_root / "patches" / "opencode_backend_adaptive.patch.1")
    patch2 = read_text(repo_root / "patches" / "opencode_backend_adaptive.patch.2")
    cancel_patch = read_text(repo_root / "patches" / "opencode_backend_cancel_neutral.patch")

    patch1_sha = sha256_bytes(patch1.encode("utf-8"))
    patch2_sha = sha256_bytes(patch2.encode("utf-8"))
    cancel_patch_sha = sha256_bytes(cancel_patch.encode("utf-8"))
    if patch1_sha != EXPECTED_PATCH1_SHA256:
        raise SystemExit("adaptive backend patch part 1 hash does not match approved source")
    _check_optional_pin(patch2_sha, EXPECTED_PATCH2_SHA256, "adaptive backend patch part 2 hash")
    if cancel_patch_sha != EXPECTED_CANCEL_PATCH_SHA256:
        raise SystemExit("cancel-neutral backend patch hash does not match approved source")

    current_backend = BACKEND.read_bytes()
    backend_old_sha = sha256_bytes(current_backend)
    if backend_old_sha != EXPECTED_BACKEND_SHA256:
        raise SystemExit("installed opencode_backend.py does not match approved live lineage")

    bridge_source = read_text(BRIDGE)
    bridge_old_sha = sha256_bytes(bridge_source.encode("utf-8"))
    if bridge_old_sha != EXPECTED_BRIDGE_SOURCE_SHA256:
        raise SystemExit("installed bridge does not match approved live source")
    if bridge_source.count(OLD_VERSION) != 1:
        raise SystemExit("installed bridge is not the approved v3.9.0 target")

    patch_binary = shutil.which("patch")
    if not patch_binary:
        raise SystemExit("system patch utility is unavailable")
    with tempfile.TemporaryDirectory(prefix="devcoveer-backend-patch-") as tmp:
        tmpdir = Path(tmp)
        candidate = tmpdir / "opencode_backend.py"
        candidate.write_bytes(current_backend)
        _apply_patch(patch_binary, tmpdir, patch1 + patch2, "adaptive routing backend patch")
        routing_backend = candidate.read_bytes()
        routing_sha = sha256_bytes(routing_backend)
        _check_optional_pin(routing_sha, EXPECTED_ROUTING_BACKEND_SHA256,
                            "intermediate adaptive routing backend hash")
        _apply_patch(patch_binary, tmpdir, cancel_patch, "cancel-neutral backend patch")
        new_backend = candidate.read_text(encoding="utf-8")
    backend_new_sha = sha256_bytes(new_backend.encode("utf-8"))
    _check_optional_pin(backend_new_sha, EXPECTED_NEW_BACKEND_SHA256,
                        "patched OpenCode backend hash")

    bridge = bridge_source
    d_start, d_end, old_discovery = function_block(bridge, "_model_discovery_payload")
    if sha256_bytes(old_discovery.encode()) != EXPECTED_MODEL_DISCOVERY_SHA256:
        raise SystemExit("model discovery function does not match approved live lineage")
    bridge = bridge[:d_start] + new_discovery.rstrip() + bridge[d_end:]

    r_start, r_end, old_read = function_block(bridge, "_read_opencode_task")
    if sha256_bytes(old_read.encode()) != EXPECTED_READ_TASK_SHA256:
        raise SystemExit("read_task function does not match approved live acceptance-gate lineage")
    bridge = bridge[:r_start] + new_read_task.rstrip() + bridge[r_end:]

    for old, new, label in (
        (CURRENT_READ_TOOL, ADAPTIVE_READ_TOOL, "read_task tool description"),
        (CURRENT_CANCEL_TOOL_TEXT, ADAPTIVE_CANCEL_TOOL_TEXT, "cancel_task tool description"),
        (CURRENT_LIST_TOOL_TEXT, ADAPTIVE_LIST_TOOL_TEXT, "list_models tool description"),
    ):
        if bridge.count(old) != 1:
            raise SystemExit(f"{label} does not match approved live schema")
        bridge = bridge.replace(old, new, 1)
    bridge = bridge.replace(OLD_VERSION, NEW_VERSION, 1)
    bridge_new_sha = sha256_bytes(bridge.encode("utf-8"))
    _check_optional_pin(bridge_new_sha, EXPECTED_NEW_BRIDGE_SHA256,
                        "patched bridge hash")

    with tempfile.TemporaryDirectory(prefix="devcoveer-adaptive-") as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "model_health.py").write_text(new_health, encoding="utf-8")
        (tmpdir / "opencode_backend.py").write_text(new_backend, encoding="utf-8")
        (tmpdir / "codex-chatgpt-bridge.py").write_text(bridge, encoding="utf-8")
        for path in tmpdir.iterdir():
            py_compile.compile(str(path), doraise=True)

    active = active_tasks()
    candidate_pinned = all(value is not None for value in (
        EXPECTED_PATCH2_SHA256,
        EXPECTED_ROUTING_BACKEND_SHA256,
        EXPECTED_NEW_BACKEND_SHA256,
        EXPECTED_NEW_BRIDGE_SHA256,
    ))
    report = {
        "targetBridgeVersion": "3.10.0",
        "backendOldSha256": backend_old_sha,
        "bridgeOldSha256": bridge_old_sha,
        "adaptivePatch1Sha256": patch1_sha,
        "adaptivePatch2Sha256": patch2_sha,
        "cancelPatchSha256": cancel_patch_sha,
        "backendRoutingSha256": routing_sha,
        "backendNewSha256": backend_new_sha,
        "bridgeNewSha256": bridge_new_sha,
        "candidatePinned": candidate_pinned,
        "activeTasks": active,
        "applyRequested": args.apply,
    }
    print(json.dumps(report, sort_keys=True))
    if not args.apply:
        return 0
    if not candidate_pinned:
        raise SystemExit(
            "candidate hashes are not pinned; run dry-run, pin exact hashes in source, and rerun before --apply"
        )
    if active:
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
    print(json.dumps({
        "status": "installed",
        "restartRequired": True,
        "backups": [p.name for p in backups],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())