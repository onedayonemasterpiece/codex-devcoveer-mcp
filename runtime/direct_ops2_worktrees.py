from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from direct_probe_core import ProbeError, _git, _git_env, _redact_text, _run_process, _validate_rel_path
from direct_ops2 import capture_handoff, workspace_compare
from source_handoff import resolve_source_handoff

CAPTURE_RECEIPT_ROOT = Path.home() / ".local" / "share" / "openai-codex-mcp" / "capture-receipts"
HANDOFF_ID_RE = re.compile(r"^src_[0-9a-f]{24}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".receipt-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


async def repo_worktrees(root: Path) -> list[dict[str, Any]]:
    result = await _git(root, "worktree", "list", "--porcelain", "-z", output_limit=128 * 1024)
    chunks = [x for x in result.stdout.decode("utf-8", "replace").split("\0\0") if x.strip()]
    rows = []
    for chunk in chunks[:128]:
        row: dict[str, Any] = {}
        for line in chunk.split("\0"):
            if not line:
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                row["path"] = str(Path(value).resolve(strict=True))
            elif key == "HEAD":
                row["head"] = value
            elif key == "branch":
                row["branch"] = value.removeprefix("refs/heads/")
            elif key in {"detached", "bare", "locked", "prunable"}:
                row[key] = value or True
        if row.get("path"):
            rows.append(row)
    return rows


async def resolve_repo_worktree(root: Path, raw_path: str, *, external_only: bool = False) -> Path:
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        raise ProbeError("invalid_input", "worktree path must be absolute")
    try:
        requested = Path(raw_path).resolve(strict=True)
    except OSError as exc:
        raise ProbeError("not_found", "worktree path does not exist", status="not_found") from exc
    rows = await repo_worktrees(root)
    allowed = {Path(row["path"]): row for row in rows}
    if requested not in allowed:
        raise ProbeError("worktree_denied", "path is not a worktree of the resolved project", status="denied")
    if external_only and requested == root.resolve(strict=True):
        raise ProbeError("canonical_denied", "operation requires a non-canonical project worktree", status="denied")
    return requested


async def worktree_compare(root: Path, check: dict[str, Any]) -> dict[str, Any]:
    worktree = await resolve_repo_worktree(root, check["worktree"])
    result = await workspace_compare(
        worktree, check["target_ref"],
        include_untracked=bool(check.get("include_untracked", True)),
        limit=int(check.get("limit", 500)),
    )
    result["worktree"] = str(worktree)
    return result


async def _patch_bytes(
    worktree: Path,
    base_sha: str,
    *,
    include_untracked: bool,
    paths: list[str] | None,
) -> tuple[bytes, list[str]]:
    selected = None
    path_args: list[str] = []
    if paths:
        selected = {_validate_rel_path(p).as_posix() for p in paths}
        path_args = ["--", *sorted(selected)]
    tracked = await _git(
        worktree, "diff", "--binary", "--no-ext-diff", "--no-textconv",
        base_sha, *path_args, timeout=60, output_limit=16 * 1024 * 1024,
    )
    parts = [tracked.stdout]
    captured_untracked: list[str] = []
    if include_untracked:
        others = await _git(
            worktree, "ls-files", "--others", "--exclude-standard", "-z",
            output_limit=4 * 1024 * 1024,
        )
        for raw in others.stdout.decode("utf-8", "replace").split("\0"):
            if not raw:
                continue
            rel = _validate_rel_path(raw).as_posix()
            if selected is not None and rel not in selected:
                continue
            proc = await _run_process(
                [
                    "/usr/bin/git", "-C", str(worktree), "diff", "--no-index", "--binary",
                    "--no-ext-diff", "--no-textconv", "--", "/dev/null", rel,
                ],
                env=_git_env(), timeout=30, output_limit=4 * 1024 * 1024,
            )
            if proc.exit_code not in (0, 1):
                raise ProbeError("capture_failed", "could not encode untracked file", exit_code=proc.exit_code)
            if proc.stdout:
                parts.append(proc.stdout)
                captured_untracked.append(rel)
    return b"".join(parts), captured_untracked


def _receipt_path(handoff_id: str) -> Path:
    if HANDOFF_ID_RE.fullmatch(handoff_id) is None:
        raise ProbeError("invalid_input", "invalid handoff id")
    return CAPTURE_RECEIPT_ROOT / f"{handoff_id}.json"


def _read_receipt(handoff_id: str) -> dict[str, Any]:
    path = _receipt_path(handoff_id)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ProbeError("receipt_not_found", "capture receipt is missing", status="not_found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProbeError("receipt_invalid", "capture receipt is invalid") from exc
    if not isinstance(data, dict) or data.get("handoffId") != handoff_id:
        raise ProbeError("receipt_invalid", "capture receipt does not match handoff")
    return data


async def capture_worktree_handoff(root: Path, project_id: str, action: dict[str, Any]) -> dict[str, Any]:
    worktree = await resolve_repo_worktree(root, action["worktree"], external_only=True)
    include_untracked = bool(action.get("include_untracked", True))
    paths = action.get("paths")
    base = action["base_sha"]
    patch, captured_untracked = await _patch_bytes(
        worktree, base, include_untracked=include_untracked, paths=paths,
    )
    patch_sha = hashlib.sha256(patch).hexdigest()
    if not patch:
        raise ProbeError("empty_patch", "selected worktree has no changes", status="denied")
    staged = await capture_handoff(
        worktree, project_id,
        {"base_sha": base, "include_untracked": include_untracked, **({"paths": paths} if paths else {})},
    )
    if staged.get("patchSha256") != patch_sha:
        raise ProbeError("capture_mismatch", "capture patch digest changed during capture", status="denied")
    head = (await _git(worktree, "rev-parse", "HEAD", output_limit=256)).stdout.decode().strip()
    receipt = {
        "version": 1,
        "handoffId": staged["handoffId"],
        "projectId": project_id,
        "canonicalRoot": str(root.resolve(strict=True)),
        "sourceWorktree": str(worktree),
        "baseSha": staged["baseSha"],
        "patchSha256": patch_sha,
        "capturedHead": head,
        "includeUntracked": include_untracked,
        "paths": list(paths) if paths else None,
        "fullCapture": include_untracked and not paths,
        "capturedUntracked": captured_untracked,
        "createdAt": int(time.time()),
    }
    _atomic_json(_receipt_path(staged["handoffId"]), receipt)
    return {**staged, "captureReceipt": {
        "patchSha256": patch_sha, "sourceWorktree": str(worktree),
        "capturedHead": head, "fullCapture": receipt["fullCapture"],
    }}


async def reconcile_worktree(root: Path, project_id: str, action: dict[str, Any]) -> dict[str, Any]:
    handoff_id = action["handoff_id"]
    receipt = _read_receipt(handoff_id)
    if receipt.get("projectId") != project_id or receipt.get("canonicalRoot") != str(root.resolve(strict=True)):
        raise ProbeError("receipt_mismatch", "capture receipt belongs to another project", status="denied")
    if receipt.get("fullCapture") is not True:
        raise ProbeError("partial_capture_denied", "reconcile requires a full capture including untracked files", status="denied")
    worktree = await resolve_repo_worktree(root, action["worktree"], external_only=True)
    if str(worktree) != receipt.get("sourceWorktree"):
        raise ProbeError("receipt_mismatch", "capture receipt belongs to another worktree", status="denied")

    branch = await _git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", ok_codes=(0, 1), output_limit=512)
    if branch.exit_code == 0:
        raise ProbeError("branch_worktree_denied", "guarded reconcile requires a detached runtime worktree", status="denied")
    head = (await _git(worktree, "rev-parse", "HEAD", output_limit=256)).stdout.decode().strip()
    if head != receipt.get("capturedHead"):
        raise ProbeError("workspace_drift", "runtime HEAD changed after capture", status="denied")
    current_patch, _ = await _patch_bytes(
        worktree, receipt["baseSha"], include_untracked=True, paths=None,
    )
    current_digest = hashlib.sha256(current_patch).hexdigest()
    if current_digest != receipt.get("patchSha256"):
        raise ProbeError("workspace_drift", "runtime overlay changed after capture", status="denied")

    try:
        handoff = resolve_source_handoff(handoff_id)
    except Exception as exc:
        raise ProbeError("handoff_invalid", "captured handoff is unavailable") from exc
    tree = (await _git(handoff, "write-tree", output_limit=256)).stdout.decode().strip()
    meta = handoff.parent / "meta.json"
    try:
        handoff_meta = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProbeError("handoff_invalid", "handoff metadata is invalid") from exc
    if handoff_meta.get("patchSha256") != receipt.get("patchSha256") or handoff_meta.get("baseSha") != receipt.get("baseSha"):
        raise ProbeError("handoff_invalid", "handoff metadata does not match capture receipt", status="denied")

    env = _git_env()
    env.update({
        "GIT_AUTHOR_NAME": "DevCoveer Direct Ops",
        "GIT_AUTHOR_EMAIL": "devcoveer@localhost",
        "GIT_COMMITTER_NAME": "DevCoveer Direct Ops",
        "GIT_COMMITTER_EMAIL": "devcoveer@localhost",
    })
    message = f"DevCoveer guarded release {tree[:12]}"
    commit = await _run_process(
        [
            "/usr/bin/git", "-C", str(root), "commit-tree", tree,
            "-p", receipt["baseSha"], "-m", message,
        ],
        env=env, timeout=30, output_limit=4096,
    )
    if commit.exit_code != 0:
        err, _ = _redact_text(commit.stderr.decode("utf-8", "replace"))
        raise ProbeError("release_commit_failed", err[-1200:] or "git commit-tree failed", exit_code=commit.exit_code)
    release_commit = commit.stdout.decode().strip()

    reset = await _git(worktree, "reset", "--hard", release_commit, timeout=120, output_limit=64 * 1024)
    if reset.exit_code != 0:
        raise ProbeError("reconcile_failed", "guarded reset failed", exit_code=reset.exit_code)
    after = (await _git(worktree, "rev-parse", "HEAD", output_limit=256)).stdout.decode().strip()
    if after != release_commit:
        raise ProbeError("reconcile_failed", "runtime HEAD did not reach release commit")
    remaining = await _git(worktree, "status", "--porcelain=v1", "-z", output_limit=128 * 1024)
    rows = [x for x in remaining.stdout.decode("utf-8", "replace").split("\0") if x]
    return {
        "status": "ok", "action": "worktree_reconcile", "worktree": str(worktree),
        "old_head": head, "release_commit": release_commit, "tree": tree,
        "capture_patch_sha256": current_digest, "remaining_status": rows,
        "canonical_checkout_untouched": True,
    }


async def owned_service_info(root: Path, unit: str) -> dict[str, Any]:
    if UNIT_RE.fullmatch(unit) is None:
        raise ProbeError("invalid_input", "service unit name is invalid")
    uid = os.getuid()
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/home/dev"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }
    props = "LoadState,ActiveState,SubState,MainPID,WorkingDirectory,ExecMainStartTimestamp"
    result = await _run_process(
        ["/usr/bin/systemctl", "--user", "show", unit, f"--property={props}", "--no-pager"],
        env=env, timeout=15, output_limit=16 * 1024,
    )
    if result.exit_code != 0:
        raise ProbeError("service_not_found", "user service could not be inspected", status="not_found", exit_code=result.exit_code)
    data: dict[str, str] = {}
    for raw in result.stdout.decode("utf-8", "replace").splitlines():
        key, sep, value = raw.partition("=")
        if sep:
            data[key] = value
    working = data.get("WorkingDirectory", "")
    if not working:
        raise ProbeError("service_ownership_denied", "service has no WorkingDirectory", status="denied")
    verified = await resolve_repo_worktree(root, working)
    return {
        "unit": unit, "working_directory": str(verified),
        "LoadState": data.get("LoadState"), "ActiveState": data.get("ActiveState"),
        "SubState": data.get("SubState"), "MainPID": data.get("MainPID"),
        "ExecMainStartTimestamp": data.get("ExecMainStartTimestamp"),
        "ownership_verified": True,
    }


async def owned_service_action(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    unit = action["unit"]
    operation = action["operation"]
    info = await owned_service_info(root, unit)
    if operation == "status":
        return {"status": "ok", "action": "owned_service_action", "operation": operation, **info}
    if operation not in {"start", "stop", "restart", "reload", "enable", "disable"}:
        raise ProbeError("invalid_input", "unsupported owned-service operation")
    uid = os.getuid()
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/home/dev"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }
    if operation in {"stop", "restart"}:
        token = hashlib.sha256(f"{unit}\0{operation}\0{time.time_ns()}".encode()).hexdigest()[:12]
        transient = f"devcoveer-owned-{operation}-{token}"
        result = await _run_process(
            [
                "/usr/bin/systemd-run", "--user", "--quiet", "--collect",
                f"--unit={transient}", "--on-active=1s",
                "/usr/bin/systemctl", "--user", operation, unit,
            ],
            env=env, timeout=15, output_limit=16 * 1024,
        )
        if result.exit_code != 0:
            raise ProbeError("service_action", f"could not schedule {operation}", exit_code=result.exit_code)
        return {
            "status": "ok", "action": "owned_service_action", "operation": operation,
            "unit": unit, "working_directory": info["working_directory"],
            "ownership_verified": True, "scheduled_after_seconds": 1,
            "transient_unit": transient,
        }
    result = await _run_process(
        ["/usr/bin/systemctl", "--user", operation, unit],
        env=env, timeout=30, output_limit=16 * 1024,
    )
    if result.exit_code != 0:
        err, _ = _redact_text(result.stderr.decode("utf-8", "replace"))
        raise ProbeError("service_action", err[-1200:] or f"systemctl {operation} failed", exit_code=result.exit_code)
    return {
        "status": "ok", "action": "owned_service_action", "operation": operation,
        "unit": unit, "working_directory": info["working_directory"],
        "ownership_verified": True,
    }
