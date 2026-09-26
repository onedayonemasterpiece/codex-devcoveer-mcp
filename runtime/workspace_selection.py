from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

PROJECTS_ROOT = Path("/home/dev/projects")


def addressable_project_name(raw_path: str | Path, *, projects_root: Path = PROJECTS_ROOT) -> str | None:
    """Return the exact project hint for a direct-child DevCoveer checkout."""
    try:
        root = projects_root.resolve(strict=True)
        path = Path(raw_path).resolve(strict=True)
    except OSError:
        return None
    if path.parent != root or path.is_symlink() or not path.is_dir():
        return None
    return path.name


def parse_worktree_porcelain(
    text: str,
    *,
    current_path: str | Path | None = None,
    projects_root: Path = PROJECTS_ROOT,
) -> list[dict[str, Any]]:
    current: Path | None = None
    if current_path is not None:
        try:
            current = Path(current_path).resolve(strict=True)
        except OSError:
            current = None
    rows: list[dict[str, Any]] = []
    for chunk in (part for part in text.split("\0\0") if part.strip()):
        row: dict[str, Any] = {}
        for line in chunk.split("\0"):
            if not line:
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                try:
                    row["path"] = str(Path(value).resolve(strict=True))
                except OSError:
                    row["path"] = value
            elif key == "HEAD":
                row["head"] = value
            elif key == "branch":
                row["branch"] = value.removeprefix("refs/heads/")
            elif key in {"detached", "bare", "locked", "prunable"}:
                row[key] = value or True
        path = row.get("path")
        if not isinstance(path, str):
            continue
        project = addressable_project_name(path, projects_root=projects_root)
        if project is None:
            continue
        row["project"] = project
        row["tool_addressable"] = True
        if current is not None:
            try:
                row["current"] = Path(path).resolve(strict=True) == current
            except OSError:
                row["current"] = False
        rows.append(row)
    rows.sort(key=lambda row: (not bool(row.get("current")), str(row.get("project", "")).casefold()))
    return rows


def related_addressable_worktrees(project_path: str | Path) -> list[dict[str, Any]]:
    """List only worktrees that can be addressed by the normal project argument."""
    try:
        project = Path(project_path).resolve(strict=True)
    except OSError:
        return []
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/home/dev"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    try:
        completed = subprocess.run(
            ["/usr/bin/git", "-C", str(project), "worktree", "list", "--porcelain", "-z"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0 or len(completed.stdout) > 128 * 1024:
        return []
    return parse_worktree_porcelain(
        completed.stdout.decode("utf-8", "replace"),
        current_path=project,
    )


def workspace_context(project_path: str | Path) -> dict[str, Any] | None:
    related = related_addressable_worktrees(project_path)
    if len(related) <= 1:
        return None
    return {
        "addressableWorktreeCount": len(related),
        "relatedWorktrees": related,
        "continuationCheckRequired": True,
        "guidance": (
            "Exact project resolution identifies only this physical checkout. "
            "For continue/checkpoint/branch/SHA work, choose the relatedWorktrees project whose branch/HEAD "
            "matches the task before read/write/test/build. When a checkpoint SHA is known, pass expected_head "
            "to guarded direct actions. stage_source_handoff isolates a ChatGPT-authored patch; it does not select "
            "an already existing worktree."
        ),
    }
