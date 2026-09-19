from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

DEFAULT_HANDOFF_ROOT = Path.home() / ".local" / "share" / "openai-codex-mcp" / "handoffs"
MAX_PATCH_BYTES = 16 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
HANDOFF_ID_RE = re.compile(r"^src_[0-9a-f]{24}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceHandoffError(RuntimeError):
    """Raised when a source handoff cannot be staged or resolved safely."""


def _now() -> int:
    return int(time.time())


def _patch_bytes(patch: str | bytes) -> bytes:
    data = patch.encode("utf-8") if isinstance(patch, str) else bytes(patch)
    if not data:
        raise SourceHandoffError("patch is empty")
    if len(data) > MAX_PATCH_BYTES:
        raise SourceHandoffError(f"patch exceeds {MAX_PATCH_BYTES} byte limit")
    return data


def _root(root: str | Path | None) -> Path:
    path = Path(root) if root is not None else DEFAULT_HANDOFF_ROOT
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _run_git(
    cwd: Path,
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: int = 30,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:1000]
        raise SourceHandoffError(detail or f"git {' '.join(args)} failed")
    return completed


def _project_root(project_root: str | Path) -> Path:
    candidate = Path(project_root).expanduser().resolve(strict=True)
    if not candidate.is_dir():
        raise SourceHandoffError("project root is not a directory")
    top = _run_git(candidate, ["rev-parse", "--show-toplevel"]).stdout.decode(
        "utf-8", errors="strict"
    ).strip()
    resolved_top = Path(top).resolve(strict=True)
    if candidate != resolved_top:
        raise SourceHandoffError("project root must be the git worktree root")
    return candidate


def _validate_sha(value: str, pattern: re.Pattern[str], label: str) -> str:
    normalized = value.strip().lower()
    if pattern.fullmatch(normalized) is None:
        raise SourceHandoffError(f"invalid {label}")
    return normalized


def _handoff_id(project_id: str, base_sha: str, patch_sha256: str) -> str:
    if not project_id or "\x00" in project_id:
        raise SourceHandoffError("invalid project id")
    digest = hashlib.sha256(
        f"{project_id}\0{base_sha}\0{patch_sha256}".encode("utf-8")
    ).hexdigest()
    return f"src_{digest[:24]}"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".meta-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_meta(handoff_dir: Path) -> dict[str, Any]:
    meta_path = handoff_dir / "meta.json"
    if not meta_path.is_file() or meta_path.is_symlink() or meta_path.stat().st_size > 1_000_000:
        raise SourceHandoffError("handoff metadata is missing or unsafe")
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SourceHandoffError("handoff metadata is invalid") from exc
    if not isinstance(payload, dict):
        raise SourceHandoffError("handoff metadata is invalid")
    return payload


def _changed_files(workspace: Path, base_sha: str) -> list[str]:
    raw = _run_git(workspace, ["diff", "--cached", "--name-only", "-z", base_sha]).stdout
    return [item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item]


def _reject_gitlinks(workspace: Path, base_sha: str) -> None:
    raw = _run_git(
        workspace,
        ["diff", "--cached", "--raw", "--no-abbrev", "-z", base_sha],
    ).stdout
    if re.search(rb":(?:160000 [0-9]{6}|[0-9]{6} 160000) ", raw):
        raise SourceHandoffError("submodule/gitlink changes are not supported by source handoff")


def _remove_worktree(project_root: Path, workspace: Path) -> None:
    _run_git(
        project_root,
        ["worktree", "remove", "--force", str(workspace)],
        timeout=60,
        check=False,
    )
    _run_git(project_root, ["worktree", "prune"], timeout=30, check=False)


def _existing_result(
    handoff_dir: Path,
    *,
    project_root: Path,
    project_id: str,
    base_sha: str,
    patch_sha256: str,
) -> dict[str, Any] | None:
    if not handoff_dir.exists():
        return None
    meta = _read_meta(handoff_dir)
    expected = {
        "projectId": project_id,
        "projectRoot": str(project_root),
        "baseSha": base_sha,
        "patchSha256": patch_sha256,
    }
    for key, value in expected.items():
        if meta.get(key) != value:
            raise SourceHandoffError("handoff id collision or metadata mismatch")
    workspace = Path(str(meta.get("worktree", "")))
    expected_workspace = (handoff_dir / "worktree").resolve()
    if workspace.resolve() != expected_workspace or not workspace.is_dir():
        raise SourceHandoffError("existing handoff worktree is missing or unsafe")
    inside = _run_git(workspace, ["rev-parse", "--is-inside-work-tree"]).stdout.strip()
    if inside != b"true":
        raise SourceHandoffError("existing handoff is not a git worktree")
    meta["lastUsedAt"] = _now()
    _atomic_json(handoff_dir / "meta.json", meta)
    return {
        "handoffId": meta["id"],
        "project": f"handoff:{meta['id']}",
        "baseSha": base_sha,
        "patchSha256": patch_sha256,
        "changedFiles": list(meta.get("changedFiles") or []),
        "reused": True,
    }


def stage_source_handoff(
    project_root: str | Path,
    project_id: str,
    base_sha: str,
    patch: str | bytes,
    patch_sha256: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Materialize a Git patch in an isolated detached worktree.

    `project_root` must already have been resolved by the DevCoveer project resolver;
    it is deliberately not an arbitrary path accepted from the MCP caller. The patch
    is consumed through stdin and is never written to disk.
    """

    project = _project_root(project_root)
    base = _validate_sha(base_sha, SHA1_RE, "base sha")
    expected_patch_sha = _validate_sha(patch_sha256, SHA256_RE, "patch sha256")
    patch_bytes = _patch_bytes(patch)
    actual_patch_sha = hashlib.sha256(patch_bytes).hexdigest()
    if actual_patch_sha != expected_patch_sha:
        raise SourceHandoffError("patch sha256 mismatch")

    exists = _run_git(project, ["cat-file", "-e", f"{base}^{{commit}}"], check=False)
    if exists.returncode != 0:
        raise SourceHandoffError("base commit is not available in the project repository")

    handoff_root = _root(root)
    handoff_id = _handoff_id(project_id, base, expected_patch_sha)
    handoff_dir = handoff_root / handoff_id
    workspace = handoff_dir / "worktree"

    reused = _existing_result(
        handoff_dir,
        project_root=project,
        project_id=project_id,
        base_sha=base,
        patch_sha256=expected_patch_sha,
    )
    if reused is not None:
        return reused

    if shutil.disk_usage(handoff_root).free < MIN_FREE_BYTES:
        raise SourceHandoffError("insufficient free disk space for source handoff")

    handoff_dir.mkdir(mode=0o700)
    created_worktree = False
    try:
        _run_git(project, ["worktree", "add", "--detach", str(workspace), base], timeout=60)
        created_worktree = True
        _run_git(
            workspace,
            ["apply", "--check", "--index", "--whitespace=nowarn", "-"],
            input_bytes=patch_bytes,
            timeout=60,
        )
        _run_git(
            workspace,
            ["apply", "--index", "--whitespace=nowarn", "-"],
            input_bytes=patch_bytes,
            timeout=60,
        )
        _reject_gitlinks(workspace, base)
        changed = _changed_files(workspace, base)
        if not changed:
            raise SourceHandoffError("patch produced no staged source changes")
        now = _now()
        meta = {
            "id": handoff_id,
            "projectId": project_id,
            "projectRoot": str(project),
            "baseSha": base,
            "patchSha256": expected_patch_sha,
            "worktree": str(workspace.resolve()),
            "createdAt": now,
            "lastUsedAt": now,
            "changedFiles": changed,
        }
        _atomic_json(handoff_dir / "meta.json", meta)
        return {
            "handoffId": handoff_id,
            "project": f"handoff:{handoff_id}",
            "baseSha": base,
            "patchSha256": expected_patch_sha,
            "changedFiles": changed,
            "reused": False,
        }
    except BaseException:
        if created_worktree:
            _remove_worktree(project, workspace)
        shutil.rmtree(handoff_dir, ignore_errors=True)
        raise


def resolve_source_handoff(
    handoff_id: str,
    *,
    root: str | Path | None = None,
) -> Path:
    """Resolve a managed handoff id to its worktree and refresh last-used time."""
    if HANDOFF_ID_RE.fullmatch(handoff_id) is None:
        raise SourceHandoffError("invalid source handoff id")
    handoff_root = _root(root)
    handoff_dir = handoff_root / handoff_id
    meta = _read_meta(handoff_dir)
    if meta.get("id") != handoff_id:
        raise SourceHandoffError("source handoff metadata id mismatch")
    workspace = Path(str(meta.get("worktree", "")))
    expected_workspace = (handoff_dir / "worktree").resolve()
    if workspace.resolve() != expected_workspace or not workspace.is_dir():
        raise SourceHandoffError("source handoff worktree is missing or unsafe")
    if _run_git(workspace, ["rev-parse", "--is-inside-work-tree"]).stdout.strip() != b"true":
        raise SourceHandoffError("source handoff is not a git worktree")
    meta["lastUsedAt"] = _now()
    _atomic_json(handoff_dir / "meta.json", meta)
    return workspace


def cleanup_source_handoff(
    handoff_id: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Remove one managed handoff worktree and its small metadata directory."""
    if HANDOFF_ID_RE.fullmatch(handoff_id) is None:
        raise SourceHandoffError("invalid source handoff id")
    handoff_root = _root(root)
    handoff_dir = handoff_root / handoff_id
    if not handoff_dir.exists():
        return {"handoffId": handoff_id, "removed": False, "reason": "not_found"}
    meta = _read_meta(handoff_dir)
    if meta.get("id") != handoff_id:
        raise SourceHandoffError("source handoff metadata id mismatch")
    project = _project_root(str(meta.get("projectRoot", "")))
    workspace = Path(str(meta.get("worktree", "")))
    expected_workspace = (handoff_dir / "worktree").resolve()
    if workspace.resolve() != expected_workspace:
        raise SourceHandoffError("source handoff worktree path is unsafe")
    _remove_worktree(project, workspace)
    shutil.rmtree(handoff_dir, ignore_errors=False)
    return {"handoffId": handoff_id, "removed": True}
