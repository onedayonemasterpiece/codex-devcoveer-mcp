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

DEFAULT_HANDOFF_ROOT = Path.home() / ".local" / "share" / "openai-codex-mcp" / "handoffs"
MAX_PATCH_BYTES = 16 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
AUTO_GC_TRIGGER_COUNT = 8
AUTO_GC_STALE_SECONDS = 60 * 60
AUTO_GC_MAX_REMOVE = 8
MAX_HANDOFF_SCAN = 256
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


def _git_common_dir(cwd: Path) -> Path | None:
    completed = _run_git(cwd, ["rev-parse", "--git-common-dir"], check=False)
    if completed.returncode != 0:
        return None
    raw = completed.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def _same_repository(first: Path, second: Path) -> bool:
    left = _git_common_dir(first)
    right = _git_common_dir(second)
    return left is not None and right is not None and left == right


def _managed_handoff_dirs(root: Path) -> list[Path]:
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    return sorted(
        entry for entry in entries
        if entry.is_dir() and not entry.is_symlink() and HANDOFF_ID_RE.fullmatch(entry.name)
    )


def _process_targets() -> tuple[list[str], bool]:
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return [], False
    uid = os.getuid()
    targets: list[str] = []
    complete = True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        try:
            targets.append(os.readlink(entry / "cwd").removesuffix(" (deleted)"))
        except OSError:
            pass
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                targets.append(os.readlink(fd).removesuffix(" (deleted)"))
            except OSError:
                continue
    return targets, complete


def _active_under(workspace: Path, targets: list[str]) -> bool:
    root = str(workspace.resolve())
    prefix = root + os.sep
    return any(target == root or target.startswith(prefix) for target in targets)


def _change_sets(workspace: Path) -> tuple[list[str], list[str]]:
    tracked_raw = _run_git(
        workspace, ["diff", "HEAD", "--name-only", "-z"], check=False
    ).stdout
    untracked_raw = _run_git(
        workspace, ["ls-files", "--others", "--exclude-standard", "-z"], check=False
    ).stdout
    tracked = sorted({
        item.decode("utf-8", errors="replace")
        for item in tracked_raw.split(b"\0") if item
    })
    untracked = sorted({
        item.decode("utf-8", errors="replace")
        for item in untracked_raw.split(b"\0") if item
    })
    return tracked, untracked


def _default_ref(project: Path) -> str | None:
    symbolic = _run_git(
        project, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], check=False
    )
    if symbolic.returncode == 0:
        value = symbolic.stdout.decode("utf-8", errors="replace").strip()
        if value:
            return value
    for candidate in ("refs/remotes/origin/main", "refs/remotes/origin/master", "refs/heads/main", "refs/heads/master"):
        if _run_git(project, ["show-ref", "--verify", "--quiet", candidate], check=False).returncode == 0:
            return candidate
    return None


def _head_referenced(workspace: Path) -> bool:
    result = _run_git(
        workspace,
        ["for-each-ref", "--format=%(refname)", "--contains", "HEAD", "refs/heads", "refs/remotes"],
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _untracked_matches_ref(project: Path, workspace: Path, ref: str, rel: str) -> bool:
    target = _run_git(project, ["show", f"{ref}:{rel}"], check=False)
    if target.returncode != 0:
        return False
    path = workspace / rel
    try:
        if path.is_symlink():
            current = os.readlink(path).encode("utf-8")
        elif path.is_file():
            current = path.read_bytes()
        else:
            return False
    except OSError:
        return False
    return current == target.stdout


def _changes_subsumed_by_ref(
    project: Path, workspace: Path, ref: str, tracked: list[str], untracked: list[str]
) -> bool:
    if not tracked and not untracked:
        return False
    if tracked:
        result = _run_git(workspace, ["diff", "--quiet", ref, "--", *tracked], check=False)
        if result.returncode != 0:
            return False
    return all(_untracked_matches_ref(project, workspace, ref, rel) for rel in untracked)


def _cleanup_project(meta: dict[str, Any], workspace: Path) -> Path:
    raw = str(meta.get("projectRoot", ""))
    if raw:
        try:
            return _project_root(raw)
        except (OSError, SourceHandoffError):
            pass
    if workspace.is_dir():
        listed = _run_git(workspace, ["worktree", "list", "--porcelain", "-z"], check=False)
        if listed.returncode == 0:
            for chunk in listed.stdout.decode("utf-8", errors="replace").split("\0\0"):
                for line in chunk.split("\0"):
                    if line.startswith("worktree "):
                        candidate = Path(line[9:])
                        if candidate != workspace and candidate.is_dir():
                            try:
                                return _project_root(candidate)
                            except (OSError, SourceHandoffError):
                                continue
    raise SourceHandoffError("source handoff repository root is unavailable")


def _belongs_to_repository(project: Path, meta: dict[str, Any], workspace: Path) -> bool:
    project_common = _git_common_dir(project)
    if project_common is None:
        return False
    if workspace.is_dir() and _git_common_dir(workspace) == project_common:
        return True
    raw = str(meta.get("projectRoot", ""))
    if raw:
        candidate = Path(raw)
        if candidate.is_dir() and _git_common_dir(candidate) == project_common:
            return True
    return False


def _handoff_row(
    project: Path,
    handoff_dir: Path,
    meta: dict[str, Any],
    process_targets: list[str],
    process_scan_complete: bool,
    now: int,
) -> dict[str, Any]:
    workspace = Path(str(meta.get("worktree", "")))
    exists = workspace.is_dir()
    last_used = int(meta.get("lastUsedAt") or meta.get("createdAt") or 0)
    row: dict[str, Any] = {
        "handoffId": handoff_dir.name,
        "projectId": str(meta.get("projectId") or ""),
        "projectRoot": str(meta.get("projectRoot") or ""),
        "worktree": str(workspace),
        "createdAt": int(meta.get("createdAt") or 0),
        "lastUsedAt": last_used,
        "ageSeconds": max(0, now - last_used) if last_used else None,
        "changedFiles": list(meta.get("changedFiles") or [])[:64],
        "workspaceExists": exists,
        "active": False,
        "processScanComplete": process_scan_complete,
        "clean": False,
        "headReferenced": False,
        "defaultRef": None,
        "changesSubsumed": False,
        "currentChangeCount": None,
        "cleanupSafe": False,
        "cleanupReason": "review_required",
    }
    if not exists:
        row["cleanupSafe"] = process_scan_complete
        row["cleanupReason"] = "workspace_missing" if process_scan_complete else "process_scan_incomplete"
        return row

    row["active"] = _active_under(workspace, process_targets)
    status = _run_git(workspace, ["status", "--porcelain=v1", "--untracked-files=all"], check=False)
    if status.returncode != 0:
        row["cleanupReason"] = "git_status_unavailable"
        return row
    row["clean"] = not bool(status.stdout.strip())
    tracked, untracked = _change_sets(workspace)
    row["currentChangeCount"] = len(set(tracked) | set(untracked))
    row["headReferenced"] = _head_referenced(workspace)
    ref = _default_ref(project)
    row["defaultRef"] = ref
    if not row["clean"] and ref is not None:
        row["changesSubsumed"] = _changes_subsumed_by_ref(
            project, workspace, ref, tracked, untracked
        )

    if not process_scan_complete:
        row["cleanupReason"] = "process_scan_incomplete"
    elif row["active"]:
        row["cleanupReason"] = "active"
    elif row["clean"] and row["headReferenced"]:
        row["cleanupSafe"] = True
        row["cleanupReason"] = "clean_head_referenced"
    elif row["changesSubsumed"]:
        row["cleanupSafe"] = True
        row["cleanupReason"] = "changes_subsumed_by_default_ref"
    elif row["clean"]:
        row["cleanupReason"] = "clean_but_unreferenced"
    else:
        row["cleanupReason"] = "unique_changes"
    return row


def list_source_handoffs(
    project_root: str | Path,
    *,
    root: str | Path | None = None,
    limit: int = 50,
    safe_only: bool = False,
) -> dict[str, Any]:
    project = _project_root(project_root)
    handoff_root = _root(root)
    process_targets, process_scan_complete = _process_targets()
    now = _now()
    rows: list[dict[str, Any]] = []
    complete = True
    dirs = _managed_handoff_dirs(handoff_root)
    if len(dirs) > MAX_HANDOFF_SCAN:
        complete = False
    for handoff_dir in dirs[:MAX_HANDOFF_SCAN]:
        try:
            meta = _read_meta(handoff_dir)
            workspace = Path(str(meta.get("worktree", "")))
            if not _belongs_to_repository(project, meta, workspace):
                continue
            rows.append(
                _handoff_row(
                    project, handoff_dir, meta, process_targets, process_scan_complete, now
                )
            )
        except (OSError, SourceHandoffError, TypeError, ValueError):
            complete = False
    rows.sort(key=lambda row: (int(row.get("lastUsedAt") or 0), row["handoffId"]), reverse=True)
    safe_count = sum(bool(row["cleanupSafe"]) for row in rows)
    selected = [row for row in rows if row["cleanupSafe"]] if safe_only else rows
    bounded = selected[: max(1, min(int(limit), 100))]
    return {
        "status": "ok",
        "handoffs": bounded,
        "count": len(rows),
        "cleanupSafeCount": safe_count,
        "hasMore": len(selected) > len(bounded),
        "complete": complete and process_scan_complete and len(selected) <= len(bounded),
        "processScanComplete": process_scan_complete,
        "note": (
            "cleanupSafe is conservative: only inactive workspaces whose state is already durable "
            "in Git refs/default branch, or missing workspaces with only metadata left, qualify."
        ),
    }


def gc_source_handoffs(
    project_root: str | Path,
    *,
    root: str | Path | None = None,
    older_than_seconds: int = AUTO_GC_STALE_SECONDS,
    max_remove: int = AUTO_GC_MAX_REMOVE,
) -> dict[str, Any]:
    older = max(0, int(older_than_seconds))
    cap = max(1, min(int(max_remove), 64))
    inventory = list_source_handoffs(
        project_root, root=root, limit=100, safe_only=True
    )
    candidates = [
        row for row in inventory["handoffs"]
        if row.get("cleanupSafe")
        and isinstance(row.get("ageSeconds"), int)
        and row["ageSeconds"] >= older
    ]
    candidates.sort(key=lambda row: (int(row.get("lastUsedAt") or 0), row["handoffId"]))
    removed: list[str] = []
    failed: list[str] = []
    for row in candidates[:cap]:
        try:
            result = cleanup_source_handoff(row["handoffId"], root=root)
            if result.get("removed"):
                removed.append(row["handoffId"])
        except (OSError, SourceHandoffError):
            failed.append(row["handoffId"])
    return {
        "status": "ok" if not failed else "partial",
        "action": "source_handoff_gc",
        "olderThanSeconds": older,
        "candidateCount": len(candidates),
        "removed": removed,
        "failed": failed,
        "remainingSafeEstimate": max(0, len(candidates) - len(removed)),
        "complete": bool(inventory.get("complete")) and not failed and len(candidates) <= cap,
    }


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
    if not _same_repository(project_root, workspace):
        raise SourceHandoffError("existing handoff belongs to another repository")
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

    auto_cleanup: dict[str, Any] | None = None
    if len(_managed_handoff_dirs(handoff_root)) >= AUTO_GC_TRIGGER_COUNT:
        try:
            auto_cleanup = gc_source_handoffs(
                project,
                root=handoff_root,
                older_than_seconds=AUTO_GC_STALE_SECONDS,
                max_remove=AUTO_GC_MAX_REMOVE,
            )
        except (OSError, SourceHandoffError, TypeError, ValueError):
            auto_cleanup = {
                "status": "unavailable",
                "action": "source_handoff_gc",
                "removed": [],
                "complete": False,
            }

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
        result = {
            "handoffId": handoff_id,
            "project": f"handoff:{handoff_id}",
            "baseSha": base,
            "patchSha256": expected_patch_sha,
            "changedFiles": changed,
            "reused": False,
        }
        if auto_cleanup is not None:
            result["autoCleanup"] = auto_cleanup
        return result
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
    workspace = Path(str(meta.get("worktree", "")))
    project = _cleanup_project(meta, workspace)
    expected_workspace = (handoff_dir / "worktree").resolve()
    if workspace.resolve() != expected_workspace:
        raise SourceHandoffError("source handoff worktree path is unsafe")
    _remove_worktree(project, workspace)
    shutil.rmtree(handoff_dir, ignore_errors=False)
    return {"handoffId": handoff_id, "removed": True}
