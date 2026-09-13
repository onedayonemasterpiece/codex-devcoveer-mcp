from __future__ import annotations

import asyncio
import os
import stat as statmod
import time
from pathlib import Path
from typing import Any

from direct_probe_core import ProbeError, _run_process


SUPPORTED_PROFILES = frozenset({"codex_sessions", "codex_worktrees"})


def resolve_resource_target(raw: Any) -> tuple[Path, str | None]:
    profile: str | None = None
    if isinstance(raw, str):
        path_s = raw
    elif isinstance(raw, dict):
        path_s = raw.get("path")
        profile = raw.get("profile")
        if profile not in SUPPORTED_PROFILES:
            raise ProbeError("config", "resource root profile is not supported")
    else:
        raise ProbeError("config", "resource root binding is invalid")
    if not isinstance(path_s, str) or not path_s.startswith("/"):
        raise ProbeError("config", "resource root path must be absolute")
    path = Path(path_s).resolve(strict=True)
    if not path.is_dir():
        raise ProbeError("config", "resource root must be a directory")
    return path, profile


def _utc_timestamp(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _is_under(target: str, root: Path) -> bool:
    base = str(root)
    return target == base or target.startswith(base + os.sep)


def _proc_targets(root: Path) -> tuple[set[str], set[str], int, int, bool]:
    cwd_targets: set[str] = set()
    fd_targets: set[str] = set()
    cwd_processes: set[str] = set()
    fd_processes: set[str] = set()
    complete = True
    uid = os.getuid()
    try:
        proc_entries = list(os.scandir("/proc"))
    except OSError:
        return cwd_targets, fd_targets, 0, 0, False
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        proc = Path(entry.path)
        try:
            if proc.stat().st_uid != uid:
                continue
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
            continue
        try:
            target = os.readlink(proc / "cwd")
            if _is_under(target, root):
                cwd_targets.add(target)
                cwd_processes.add(entry.name)
        except FileNotFoundError:
            pass
        except OSError:
            complete = False
        try:
            with os.scandir(proc / "fd") as fds:
                for fd in fds:
                    try:
                        target = os.readlink(fd.path)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        complete = False
                        continue
                    if _is_under(target, root):
                        fd_targets.add(target)
                        fd_processes.add(entry.name)
                        if len(fd_targets) > 5000:
                            complete = False
                            break
        except FileNotFoundError:
            pass
        except OSError:
            complete = False
    return cwd_targets, fd_targets, len(cwd_processes), len(fd_processes), complete


def sessions_profile(path: Path) -> dict[str, Any]:
    now = time.time()
    thresholds = (7, 14, 30, 60)
    buckets = {days: {"older_than_days": days, "files": 0, "bytes": 0} for days in thresholds}
    total_files = 0
    total_bytes = 0
    oldest: float | None = None
    newest: float | None = None
    complete = True
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            candidate = Path(dirpath) / name
            try:
                st = candidate.lstat()
            except OSError:
                complete = False
                continue
            if not statmod.S_ISREG(st.st_mode):
                continue
            total_files += 1
            total_bytes += st.st_size
            oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
            newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
            age = now - st.st_mtime
            for days in thresholds:
                if age > days * 86400:
                    buckets[days]["files"] += 1
                    buckets[days]["bytes"] += st.st_size
    _cwd, _fd, cwd_count, fd_count, proc_complete = _proc_targets(path)
    return {
        "profile": "codex_sessions",
        "files": {
            "count": total_files,
            "bytes": total_bytes,
            "oldest_utc": _utc_timestamp(oldest),
            "newest_utc": _utc_timestamp(newest),
        },
        "age_buckets": [buckets[days] for days in thresholds],
        "process_refs": {
            "scope": "same_uid",
            "cwd_processes": cwd_count,
            "fd_processes": fd_count,
            "complete": proc_complete,
        },
        "complete": complete and proc_complete,
    }


def _tree_usage_mtime(path: Path) -> tuple[int, float, bool]:
    total = 0
    newest = 0.0
    complete = True
    try:
        root_dev = path.lstat().st_dev
    except OSError as exc:
        raise ProbeError("resource", f"cannot stat resource tree: {exc}") from exc
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        keep: list[str] = []
        for name in dirnames:
            child = Path(dirpath) / name
            try:
                st = child.lstat()
            except OSError:
                complete = False
                continue
            total += st.st_blocks * 512
            newest = max(newest, st.st_mtime)
            if st.st_dev == root_dev and not statmod.S_ISLNK(st.st_mode):
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            child = Path(dirpath) / name
            try:
                st = child.lstat()
            except OSError:
                complete = False
                continue
            if st.st_dev != root_dev:
                continue
            total += st.st_blocks * 512
            newest = max(newest, st.st_mtime)
    try:
        st = path.lstat()
        total += st.st_blocks * 512
        newest = max(newest, st.st_mtime)
    except OSError:
        complete = False
    return total, newest, complete


def _discover_git_leaves(root: Path, max_depth: int = 5) -> dict[Path, str]:
    leaves: dict[Path, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        try:
            depth = len(here.relative_to(root).parts)
        except ValueError:
            continue
        if ".git" in filenames:
            leaves[here] = "linked_worktree"
        if ".git" in dirnames:
            leaves.setdefault(here, "standalone_clone")
            dirnames.remove(".git")
        if depth >= max_depth:
            dirnames.clear()
    return leaves


async def _git_worktree_metadata(leaf: Path) -> tuple[bool, str, bool, bool]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": os.environ.get("HOME", "/home/dev"),
    }
    status = await _run_process(
        ["/usr/bin/git", "-C", str(leaf), "status", "--porcelain=v1"],
        env=env, timeout=8, output_limit=4096,
    )
    if status.exit_code != 0:
        return False, "UNKNOWN", False, False
    clean = len(status.stdout.strip()) == 0
    branch_result = await _run_process(
        ["/usr/bin/git", "-C", str(leaf), "symbolic-ref", "--short", "-q", "HEAD"],
        env=env, timeout=4, output_limit=1024,
    )
    branch = (
        branch_result.stdout.decode("utf-8", "replace").strip()
        if branch_result.exit_code == 0 else "DETACHED"
    )
    refs = await _run_process(
        ["/usr/bin/git", "-C", str(leaf), "for-each-ref", "--format=%(refname)",
         "--contains", "HEAD", "refs/heads", "refs/remotes"],
        env=env, timeout=6, output_limit=8192,
    )
    if refs.exit_code != 0:
        return clean, branch or "DETACHED", False, False
    return clean, branch or "DETACHED", bool(refs.stdout.strip()), True


async def _worktree_row(
    root: Path,
    leaf: Path,
    layout: str,
    now: float,
    cwd_targets: set[str],
    fd_targets: set[str],
    proc_complete: bool,
) -> dict[str, Any]:
    (usage, newest, scan_complete), (clean, branch, referenced, git_ok) = await asyncio.gather(
        asyncio.to_thread(_tree_usage_mtime, leaf),
        _git_worktree_metadata(leaf),
    )
    active = any(_is_under(target, leaf) for target in cwd_targets) or any(
        _is_under(target, leaf) for target in fd_targets
    )
    # A standalone clone may contain local refs/objects that disappear with the clone.
    # Keep those review-only; linked worktrees share Git storage with their parent repo.
    reference_safe = layout == "linked_worktree" and referenced
    eligible = bool(scan_complete and git_ok and clean and reference_safe and not active)
    auto_delete_safe = bool(eligible and proc_complete)
    return {
        "path": str(leaf),
        "relative_path": str(leaf.relative_to(root)),
        "bytes": usage,
        "age_days": max(0, int((now - newest) // 86400)) if newest else None,
        "newest_utc": _utc_timestamp(newest or None),
        "branch": branch,
        "layout": layout,
        "clean": clean,
        "head_referenced": referenced,
        "reference_safe": reference_safe,
        "active": active,
        "git_ok": git_ok,
        "scan_complete": scan_complete,
        "eligible": eligible,
        "auto_delete_safe": auto_delete_safe,
    }


def _dedupe_nested(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paths = {row["path"] for row in rows}
    return [
        row
        for row in sorted(rows, key=lambda row: (row["path"].count(os.sep), row["path"]))
        if not any(
            row["path"].startswith(parent + os.sep)
            for parent in paths
            if parent != row["path"]
        )
    ]


async def worktrees_profile(path: Path) -> dict[str, Any]:
    now = time.time()
    layouts = await asyncio.to_thread(_discover_git_leaves, path)
    cwd_targets, fd_targets, cwd_count, fd_count, proc_complete = await asyncio.to_thread(
        _proc_targets, path
    )
    rows = await asyncio.gather(*[
        _worktree_row(path, leaf, layout, now, cwd_targets, fd_targets, proc_complete)
        for leaf, layout in sorted(layouts.items(), key=lambda item: str(item[0]))
    ])
    clean_ref_inactive = [row for row in rows if row["eligible"]]
    auto_delete_safe = [row for row in rows if row["auto_delete_safe"]]
    dirty = [row for row in rows if row["git_ok"] and not row["clean"]]
    unsafe_ref = [row for row in rows if not row["reference_safe"]]
    active = [row for row in rows if row["active"]]

    # Avoid double-counting reclaimable bytes when a nested repo sits
    # under another candidate.
    unique_eligible = _dedupe_nested(clean_ref_inactive)
    unique_auto_delete_safe = _dedupe_nested(auto_delete_safe)
    thresholds = (1, 3, 7, 14, 30)

    def age_buckets(candidates: list[dict[str, Any]]) -> list[dict[str, int]]:
        out: list[dict[str, int]] = []
        for days in thresholds:
            matched = [
                row for row in candidates
                if row["age_days"] is not None and row["age_days"] > days
            ]
            out.append({
                "older_than_days": days,
                "count": len(matched),
                "bytes": sum(row["bytes"] for row in matched),
            })
        return out

    def summary(items: list[dict[str, Any]]) -> dict[str, int]:
        return {"count": len(items), "bytes": sum(row["bytes"] for row in items)}

    def public(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row[key] for key in (
            "path", "relative_path", "bytes", "age_days", "newest_utc", "branch", "layout",
            "clean", "head_referenced", "reference_safe", "active", "git_ok", "scan_complete",
            "eligible", "auto_delete_safe",
        )}

    review = [row for row in rows if not row["eligible"]]
    return {
        "profile": "codex_worktrees",
        "bytes_may_overlap_for_nested_repositories": True,
        "process_refs": {
            "scope": "same_uid",
            "cwd_processes": cwd_count,
            "fd_processes": fd_count,
            "complete": proc_complete,
        },
        "leaves": {
            "total": summary(rows),
            "clean_ref_inactive": summary(clean_ref_inactive),
            "eligible_unique": summary(unique_eligible),
            "auto_delete_safe": summary(auto_delete_safe),
            "auto_delete_safe_unique": summary(unique_auto_delete_safe),
            "dirty": summary(dirty),
            "reference_unsafe": summary(unsafe_ref),
            "active": summary(active),
        },
        "age_buckets_unique_eligible": age_buckets(unique_eligible),
        "age_buckets_auto_delete_safe": age_buckets(unique_auto_delete_safe),
        "top_eligible": [
            public(row)
            for row in sorted(unique_eligible, key=lambda row: row["bytes"], reverse=True)[:40]
        ],
        "top_review": [
            public(row)
            for row in sorted(review, key=lambda row: row["bytes"], reverse=True)[:30]
        ],
        "complete": proc_complete and all(row["scan_complete"] and row["git_ok"] for row in rows),
    }


async def run_resource_profile(profile: str, path: Path) -> dict[str, Any]:
    if profile == "codex_sessions":
        return await asyncio.to_thread(sessions_profile, path)
    if profile == "codex_worktrees":
        return await worktrees_profile(path)
    raise ProbeError("config", "resource root profile is not supported")
