#!/usr/bin/env python3
"""Fail-closed bridge refresh for an already deployed Direct Probes runtime.

This is deliberately narrower than install-direct-probes.py: it refreshes only
codex-chatgpt-bridge.py after the initial Direct Probes installation. The
candidate is always regenerated from a trusted pre-Direct-Probes backup.
Service restart remains an explicit deployment step outside this script.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import py_compile
import shutil
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
INSTALLER_PATH = SCRIPT_DIR / "install-direct-probes.py"
EXPECTED_DEPLOYED_BRIDGE_SHA256 = "4de99e63f1c089103a0bd0a4efea6396c1279690109d98fb1232cb218a137ab8"
BACKUP_GLOB = "codex-chatgpt-bridge.py.bak.direct-probes-*"


def load_installer():
    spec = importlib.util.spec_from_file_location("install_direct_probes_for_refresh", INSTALLER_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load Direct Probes installer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def trusted_baseline(installer) -> tuple[Path, bytes]:
    matches: list[tuple[Path, bytes]] = []
    for path in sorted(installer.INSTALL_DIR.glob(BACKUP_GLOB)):
        try:
            data = installer.safe_bytes(path)
        except SystemExit:
            continue
        if installer.sha256(data) == installer.EXPECTED_BRIDGE_SHA256:
            matches.append((path, data))
    if not matches:
        raise SystemExit("trusted pre-direct-probe bridge backup is unavailable")
    return matches[0]


def build_candidate(installer, current_bridge: bytes) -> tuple[bytes, Path]:
    baseline_path, baseline = trusted_baseline(installer)
    try:
        baseline_text = baseline.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("trusted bridge backup is not UTF-8") from exc
    candidate = installer.transform_bridge(baseline_text).encode("utf-8")
    current_digest = sha256(current_bridge)
    candidate_digest = sha256(candidate)
    if current_digest not in {EXPECTED_DEPLOYED_BRIDGE_SHA256, candidate_digest}:
        raise SystemExit(
            f"installed bridge SHA-256 {current_digest} is not the accepted deployed bridge "
            "or the exact desired refresh candidate"
        )
    return candidate, baseline_path


def compile_candidate(candidate: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix="devcoveer-direct-probe-refresh-") as tmp:
        path = Path(tmp) / "codex-chatgpt-bridge.py"
        path.write_bytes(candidate)
        py_compile.compile(str(path), doraise=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--maintenance-task-id")
    args = ap.parse_args()

    installer = load_installer()
    current = installer.safe_bytes(installer.BRIDGE)
    candidate, baseline_path = build_candidate(installer, current)
    compile_candidate(candidate)
    gate = installer.maintenance_gate(args.maintenance_task_id)

    old_digest = sha256(current)
    new_digest = sha256(candidate)
    report = {
        "targetIntegration": "direct-probes-bridge-refresh-v1",
        "applyRequested": args.apply,
        "candidateCompiles": True,
        "bridgeOldSha256": old_digest,
        "bridgeNewSha256": new_digest,
        "bridgeAlreadyDesired": old_digest == new_digest,
        "baselineBackup": baseline_path.name,
        "baselineSha256": installer.EXPECTED_BRIDGE_SHA256,
        "maintenanceGate": gate,
    }
    print(json.dumps(report, sort_keys=True))

    if not args.apply or old_digest == new_digest:
        return 0
    if gate["active"]:
        raise SystemExit("live or unverified DevCoveer tasks exist; bridge refresh is blocked")
    if args.maintenance_task_id is not None and gate["excluded"] != args.maintenance_task_id:
        raise SystemExit("maintenance task self-exclusion was not verified")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rollback = installer.BRIDGE.with_name(
        installer.BRIDGE.name + f".bak.direct-probes-refresh-{stamp}"
    )
    shutil.copy2(installer.BRIDGE, rollback)
    mode = installer.BRIDGE.stat().st_mode & 0o7777
    try:
        installer.atomic_write_bytes(installer.BRIDGE, candidate, mode)
        py_compile.compile(str(installer.BRIDGE), doraise=True)
    except BaseException:
        shutil.copy2(rollback, installer.BRIDGE)
        raise

    print(json.dumps({
        "status": "installed",
        "restartRequired": True,
        "bridgeSha256": sha256(installer.BRIDGE.read_bytes()),
        "rollback": rollback.name,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
