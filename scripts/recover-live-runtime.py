#!/usr/bin/env python3
"""Read-only recovery helper for the installed DevCoveer MCP source.

The script never modifies the installed runtime, invokes Git, or restarts a
service. It inventories the fixed host-local source tree and copies only
eligible UTF-8 source text into the repository's ignored .recovery/ staging
area after a bounded secret-pattern scan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path("/home/dev/.local/libexec/openai-codex-mcp")
MAX_FILE_BYTES = 2_000_000
SOURCE_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sh"}
SOURCE_NAMES = {"run-codex-mcp", "run-codex-mcp-legacy"}
BLOCKED_SUFFIXES = {".env", ".pem", ".key", ".p12", ".pfx", ".token", ".credentials"}
SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}\b")),
    ("basic_credential", re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{24,}\b")),
)
VERSION_RE = re.compile(r'^BRIDGE_VERSION\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_backup_or_runtime(path: Path) -> bool:
    parts = path.parts
    name = path.name
    return (
        "__pycache__" in parts
        or name.endswith((".pyc", ".pyo"))
        or ".bak." in name
        or name.startswith(".bak")
        or name.endswith("~")
    )


def blocked_name(path: Path) -> bool:
    name = path.name.lower()
    if name == ".env" or name.startswith(".env."):
        return True
    if any(name.endswith(suffix) for suffix in BLOCKED_SUFFIXES):
        return True
    return name in {"credentials.json", "token.json", "secrets.json"}


def candidate_source(path: Path) -> bool:
    return path.suffix.lower() in SOURCE_SUFFIXES or path.name in SOURCE_NAMES


def scan_secret(text: str) -> str | None:
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expected-version",
        default="3.8.0",
        help="Expected BRIDGE_VERSION in the installed codex-chatgpt-bridge.py",
    )
    args = parser.parse_args()

    if not SOURCE.is_dir() or SOURCE.is_symlink():
        print(f"ERROR: trusted source directory unavailable: {SOURCE}", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    staging = repo_root / ".recovery" / f"live-runtime-{stamp}"
    candidate_root = staging / "candidate"
    candidate_root.mkdir(parents=True, mode=0o700)

    records: list[dict[str, object]] = []
    copied = 0
    blocked = 0

    for path in sorted(SOURCE.rglob("*")):
        relative = path.relative_to(SOURCE)
        record: dict[str, object] = {"path": relative.as_posix()}

        if path.is_symlink():
            record.update(classification="rejected", reason="symlink")
            records.append(record)
            continue
        if not path.is_file():
            continue

        try:
            metadata = path.stat()
        except OSError as exc:
            record.update(classification="rejected", reason=f"stat_error:{type(exc).__name__}")
            records.append(record)
            continue

        record["size"] = metadata.st_size
        record["mode"] = stat.S_IMODE(metadata.st_mode)

        if is_backup_or_runtime(relative):
            record.update(classification="ignored", reason="backup_or_runtime_artifact")
            records.append(record)
            continue
        if blocked_name(relative):
            record.update(classification="rejected", reason="credential_like_filename")
            records.append(record)
            blocked += 1
            continue
        if metadata.st_size > MAX_FILE_BYTES:
            record.update(classification="rejected", reason="oversized")
            records.append(record)
            blocked += 1
            continue

        try:
            data = path.read_bytes()
        except OSError as exc:
            record.update(classification="rejected", reason=f"read_error:{type(exc).__name__}")
            records.append(record)
            continue

        record["sha256"] = sha256(data)
        if not candidate_source(relative):
            record.update(classification="inventory_only", reason="non_source_extension")
            records.append(record)
            continue

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            record.update(classification="rejected", reason="non_utf8")
            records.append(record)
            blocked += 1
            continue

        secret_label = scan_secret(text)
        if secret_label:
            record.update(classification="rejected", reason=f"secret_pattern:{secret_label}")
            records.append(record)
            blocked += 1
            continue

        target = candidate_root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(data)
        os.chmod(target, 0o600)
        record.update(classification="copied_candidate")
        records.append(record)
        copied += 1

    bridge_path = SOURCE / "codex-chatgpt-bridge.py"
    observed_version: str | None = None
    if bridge_path.is_file() and not bridge_path.is_symlink() and bridge_path.stat().st_size <= MAX_FILE_BYTES:
        try:
            bridge_text = bridge_path.read_text(encoding="utf-8")
            match = VERSION_RE.search(bridge_text)
            observed_version = match.group(1) if match else None
        except (OSError, UnicodeError):
            pass

    inventory = {
        "schema": "codex-devcoveer-mcp.runtime-recovery.v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": str(SOURCE),
        "source_mutated": False,
        "git_invoked": False,
        "services_restarted": False,
        "expected_bridge_version": args.expected_version,
        "observed_bridge_version": observed_version,
        "version_matches": observed_version == args.expected_version,
        "copied_candidate_files": copied,
        "blocked_files": blocked,
        "files": records,
    }
    inventory_path = staging / "inventory.json"
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(inventory_path, 0o600)

    print(f"inventory={inventory_path}")
    print(f"candidate={candidate_root}")
    print(f"observed_bridge_version={observed_version or 'UNKNOWN'}")
    print(f"copied_candidate_files={copied}")
    print(f"blocked_files={blocked}")

    if observed_version != args.expected_version:
        print("ERROR: installed bridge version does not match expected version", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
