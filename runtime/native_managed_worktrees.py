"""Owned, resumable Git worktrees for parallel DevCoveer development tasks.

One exact native thread/task -> one isolated branch and worktree.
No clones, arbitrary paths, force-removals, broad pruning or silent data loss.
The finish operation merges only into the original clean checked-out target
branch, then removes *only* its own clean and inactive temporary worktree.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from direct_probe_core import ProbeError

WORKTREE_ROOT = Path("/home/dev/.codex/worktrees/devcoveer-managed")
RECORD_ROOT = Path.home() / ".local/share/openai-codex-mcp/managed-worktrees"
THREAD_RE = re.compile(
    r"^(?:dvt_[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
PROJECT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,80}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_ACTIVE_PER_PROJECT = 12
# Only adopt explicitly addressed, already registered repository worktrees.
# The caller cannot give us an arbitrary path to clone, move or delete.
ADOPT_ROOTS = (
    Path("/home/dev/projects"),
    Path("/home/dev/worktrees"),
    Path("/home/dev/.codex/worktrees"),
)
ENV = {"HOME": "/home/dev", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
       "GIT_AUTHOR_NAME": "DevCoveer", "GIT_AUTHOR_EMAIL": "devcoveer@localhost",
       "GIT_COMMITTER_NAME": "DevCoveer", "GIT_COMMITTER_EMAIL": "devcoveer@localhost"}


def _git(root: Path, *args: str, check: bool = True, timeout: int = 40) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
        text=True, capture_output=True, timeout=timeout, env=ENV,
    )
    if check and process.returncode != 0:
        raise ProbeError("managed_git_failed", f"Git {args[0]} failed (exit={process.returncode}); no force operation was used")
    return process


def _validate(root: Path, project: str, ref: str) -> tuple[Path, Path, str]:
    if PROJECT_RE.fullmatch(project) is None or THREAD_RE.fullmatch(ref) is None:
        raise ProbeError("invalid_input", "managed worktree requires exact project and native task/thread ID", status="denied")
    if root.is_symlink():
        raise ProbeError("root_denied", "canonical checkout is a symlink", status="denied")
    root = root.resolve(strict=True)
    top = _git(root, "rev-parse", "--show-toplevel").stdout.strip()
    if top != str(root):
        raise ProbeError("root_denied", "operation needs the exact canonical Git root", status="denied")
    first = _git(root, "worktree", "list", "--porcelain").stdout.splitlines()
    if not first or not first[0].startswith("worktree ") or Path(first[0][9:]).resolve() != root:
        raise ProbeError("root_denied", "cannot create an isolated task from another agent's secondary worktree", status="denied")
    token = ref.lower().replace("-", "").removeprefix("dvt_")
    slug = project[:48].lower()
    key = f"{slug}-{token}"
    worktree = WORKTREE_ROOT / key
    marker = RECORD_ROOT / f"{key}.json"
    branch = f"chatgpt/devcoveer-{slug[:20]}-{token}"
    if len(branch) >= 200:
        raise ProbeError("invalid_input", "managed branch name is too long", status="denied")
    return worktree, marker, branch


@contextmanager
def _guard():
    for folder in (RECORD_ROOT, WORKTREE_ROOT):
        if folder.is_symlink():
            raise ProbeError("storage_denied", "managed worktree root cannot be symlinked", status="denied")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not RECORD_ROOT.resolve().is_dir() or not WORKTREE_ROOT.resolve().is_dir():
        raise ProbeError("storage_denied", "managed worktree root is unavailable", status="denied")
    lock = RECORD_ROOT / ".lock"
    if lock.is_symlink():
        raise ProbeError("storage_denied", "managed lock cannot be symlinked", status="denied")
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _save(path: Path, record: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=".managed-", dir=RECORD_ROOT)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def _load(marker: Path) -> dict[str, Any] | None:
    if not marker.exists():
        return None
    if marker.is_symlink() or marker.stat().st_size > 16384:
        raise ProbeError("record_denied", "managed record is invalid", status="denied")
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ProbeError("record_denied", "managed record cannot be read", status="denied") from exc
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise ProbeError("record_denied", "managed record has the wrong schema", status="denied")
    return record


def _branch(root: Path) -> str | None:
    r = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def _head(root: Path) -> str:
    head = _git(root, "rev-parse", "HEAD").stdout.strip().lower()
    if SHA_RE.fullmatch(head) is None:
        raise ProbeError("head_invalid", "Git HEAD is not a valid commit")
    return head


def _dirty(root: Path, *, include_ignored: bool = False) -> bool:
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if include_ignored:
        args.append("--ignored=matching")
    return bool(_git(root, *args, timeout=35).stdout)


def _active(root: Path) -> list[str]:
    return [part.name for part in RECORD_ROOT.glob(f"{root.name.lower()}-*.json")
            if (row := _load(part)) is not None
            and row.get("repository") == str(root)
            and row.get("state") != "finished"]


def _registered(root: Path, worktree: Path) -> bool:
    data = _git(root, "worktree", "list", "--porcelain", "-z").stdout
    return f"worktree {worktree}\0" in data


def _transport_process(proc: Path, uid: int) -> bool:
    """Skip only ptrace-hidden systemd session transport, never application workers."""
    try:
        name = (proc / "comm").read_text(encoding="utf-8").strip()
        group = (proc / "cgroup").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    prefix = rf"0::/user\.slice/user-{uid}\.slice/"
    if name in {"systemd", "(sd-pam)"}:
        return re.fullmatch(prefix + rf"user@{uid}\.service/init\.scope", group) is not None
    if name == "sshd-session":
        return re.fullmatch(prefix + r"session-[0-9]+\.scope", group) is not None
    return False


def _in_use(worktree: Path) -> bool:
    prefix = str(worktree) + os.sep
    uid = os.getuid()
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        proc = Path(entry.path)
        try:
            if proc.stat().st_uid != uid:
                continue
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if _transport_process(proc, uid):
            continue
        try:
            links = [proc / "cwd"]
            with os.scandir(proc / "fd") as fds:
                links.extend(Path(fd.path) for fd in fds)
            for item in links:
                try:
                    location = os.readlink(item)
                    if location == str(worktree) or location.startswith(prefix):
                        return True
                except FileNotFoundError:
                    continue
                except OSError:
                    return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _public(record: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "status": "ok", "action": "managed_worktree",
        "state": record["state"], "repository": record["repository"],
        "worktree": record["worktree"], "branch": record["branch"],
        "target_branch": record["target_branch"],
        "base_sha": record["base_sha"], "task_ref": record["task_ref"],
        **extra,
    }


def _blocked(record: dict[str, Any], reason: str, **details: Any) -> dict[str, Any]:
    return _public(record, status="blocked", blocker=reason,
                   worktree_preserved=True, **details)


def _check_record(record: dict[str, Any], root: Path, worktree: Path, branch: str, ref: str) -> None:
    adopted = record.get("adopted", False)
    if adopted is not True and adopted is not False:
        raise ProbeError("record_denied", "invalid managed adoption receipt", status="denied")
    if adopted:
        adopted_path = record.get("worktree")
        adopted_branch = record.get("branch")
        if (not isinstance(adopted_path, str) or not adopted_path.startswith("/")
                or not isinstance(adopted_branch, str)
                or re.fullmatch(r"chatgpt/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", adopted_branch) is None):
            raise ProbeError("record_denied", "invalid adopted worktree identity", status="denied")
        worktree = Path(adopted_path)
        branch = adopted_branch
        if (worktree.is_symlink() or not any(
                worktree.is_relative_to(parent) and worktree != parent
                for parent in ADOPT_ROOTS)):
            raise ProbeError("record_denied", "adopted worktree escapes allowed roots", status="denied")
    if (record.get("repository") != str(root)
            or record.get("worktree") != str(worktree)
            or record.get("branch") != branch
            or record.get("task_ref") != ref
            or not isinstance(record.get("base_sha"), str)
            or SHA_RE.fullmatch(record["base_sha"]) is None
            or not isinstance(record.get("target_branch"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", record["target_branch"])):
        raise ProbeError("record_denied", "managed worktree record ownership mismatch", status="denied")


def _registered_external(root: Path, raw_path: str, expected_branch: str,
                         expected_head: str) -> Path:
    """Prove exact existing worktree identity without modifying any checkout."""
    if (not isinstance(raw_path, str) or not raw_path.startswith("/")
            or not isinstance(expected_branch, str)
            or re.fullmatch(r"chatgpt/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", expected_branch) is None
            or not isinstance(expected_head, str)
            or SHA_RE.fullmatch(expected_head) is None):
        raise ProbeError("invalid_input", "adopt requires absolute registered worktree, exact chatgpt branch and SHA",
                         status="denied")
    candidate = Path(raw_path)
    if candidate.is_symlink() or not candidate.is_dir():
        raise ProbeError("not_found", "candidate worktree is missing or symlinked",
                         status="not_found")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProbeError("not_found", "candidate worktree cannot be resolved",
                         status="not_found") from exc
    if candidate != resolved or candidate == root:
        raise ProbeError("worktree_denied", "candidate worktree must be exact and non-canonical",
                         status="denied")
    if not any(resolved.is_relative_to(parent) and resolved != parent for parent in ADOPT_ROOTS):
        raise ProbeError("worktree_denied", "candidate worktree is outside dedicated checkout roots",
                         status="denied")
    if (_git(resolved, "rev-parse", "--show-toplevel").stdout.strip() != str(resolved)
            or not _registered(root, resolved)):
        raise ProbeError("worktree_denied", "candidate is not registered under this repository",
                         status="denied")
    if _branch(resolved) != expected_branch or _head(resolved) != expected_head:
        raise ProbeError("workspace_drift", "candidate branch or HEAD does not match preview",
                         status="denied")
    return resolved


def _existing_owner(path: Path, branch: str) -> str | None:
    for marker in RECORD_ROOT.glob("*.json"):
        recorded = _load(marker)
        if (recorded is not None and recorded.get("state") != "finished"
                and (recorded.get("worktree") == str(path)
                     or recorded.get("branch") == branch)):
            return recorded.get("task_ref") or "other"
    return None


def _retire_integrated(
    root: Path, worktree: Path, marker: Path, branch: str,
    record: dict[str, Any], expected_head: str,
) -> dict[str, Any]:
    """Safely finish an already merged lifecycle, even after a process crash."""
    if record.get("state") != "integrated" or record.get("feature_head") != expected_head:
        return _blocked(record, "integration_receipt_mismatch")
    target_commit = _git(root, "rev-parse", f"refs/heads/{record['target_branch']}",
                         check=False).stdout.strip()
    if SHA_RE.fullmatch(target_commit) is None or _git(
        root, "merge-base", "--is-ancestor", expected_head, target_commit,
        check=False
    ).returncode != 0:
        return _blocked(record, "integration_no_longer_proven")
    if worktree.is_symlink():
        return _blocked(record, "worktree_symlink_denied")
    if worktree.exists():
        if not worktree.is_dir() or not _registered(root, worktree):
            return _blocked(record, "worktree_missing_or_unregistered")
        if _branch(worktree) != branch or _head(worktree) != expected_head:
            return _blocked(record, "worktree_changed_after_integration")
        if _dirty(worktree, include_ignored=True):
            return _blocked(record, "uncommitted_or_untracked_work")
        if _in_use(worktree):
            return _blocked(record, "worktree_in_use", integrated=True,
                            integrated_head=target_commit)
        removed = _git(root, "worktree", "remove", "--", str(worktree),
                       check=False, timeout=90)
        if removed.returncode != 0:
            return _blocked(record, "git_refused_worktree_removal",
                            integrated=True, integrated_head=target_commit)
    elif _registered(root, worktree):
        return _blocked(record, "missing_worktree_still_registered")
    ref_exists = _git(root, "show-ref", "--verify", "--quiet",
                      f"refs/heads/{branch}", check=False).returncode == 0
    if ref_exists:
        retired = _git(root, "branch", "-d", "--", branch, check=False)
        if retired.returncode != 0:
            return _blocked(record, "branch_retirement_failed",
                            integrated=True, integrated_head=target_commit)
    record["state"] = "finished"
    record["integrated_head"] = target_commit
    _save(marker, record)
    return _public(record, merged=True, cleaned=True, integrated_head=target_commit)


def managed_worktree(root: Path, project: str, action: dict[str, Any]) -> dict[str, Any]:
    """Create, inspect or integrate-and-retire a task-owned worktree."""
    mode = action["mode"]
    ref = action["task_ref"].lower()
    root = Path(root)
    worktree, marker, branch = _validate(root, project, ref)
    root = root.resolve(strict=True)

    with _guard():
        record = _load(marker)
        if record is not None:
            _check_record(record, root, worktree, branch, ref)
            if record.get("adopted"):
                worktree = Path(record["worktree"])
                branch = record["branch"]
        if mode == "adopt":
            # Existing exact Git worktree only; no branch or directory creation.
            candidate = _registered_external(
                root, action.get("worktree"), action.get("expected_branch"),
                action.get("expected_head"))
            wanted_branch = action["expected_branch"]
            if record is not None:
                if (record["worktree"] == str(candidate)
                        and record["branch"] == wanted_branch
                        and record["state"] in {"active", "creating", "integrated", "finished"}):
                    return _public(record, reused=True, adopted=bool(record.get("adopted")))
                return _blocked(record, "task_already_bound_to_another_worktree")
            if len(_active(root)) >= MAX_ACTIVE_PER_PROJECT:
                raise ProbeError("capacity", "too many active managed worktrees",
                                 status="denied")
            other = _existing_owner(candidate, wanted_branch)
            if other is not None:
                raise ProbeError("already_owned", "worktree or branch belongs to another task",
                                 status="denied")
            target_branch = _branch(root)
            if target_branch is None or target_branch == wanted_branch:
                raise ProbeError("target_branch_denied", "target branch is missing or overlaps the feature",
                                 status="denied")
            base_result = _git(root, "merge-base", "HEAD", action["expected_head"],
                               check=False)
            base_sha = base_result.stdout.strip().lower()
            if base_result.returncode != 0 or SHA_RE.fullmatch(base_sha) is None:
                raise ProbeError("unrelated_history", "worktree cannot be integrated into target",
                                 status="denied")
            record = {
                "schema": 1, "repository": str(root), "task_ref": ref,
                "worktree": str(candidate), "branch": wanted_branch,
                "target_branch": target_branch, "base_sha": base_sha,
                "state": "active", "adopted": True,
                "adopted_head": action["expected_head"],
                "created_at": int(time.time()),
            }
            _save(marker, record)
            return _public(record, adopted=True, reused=False,
                           head=action["expected_head"],
                           target_dirty=_dirty(root), needs_integration=True)
        if mode == "create":
            if record is not None:
                if record["state"] in {"active", "creating"} and worktree.is_dir() and _registered(root, worktree):
                    if _branch(worktree) != branch:
                        return _blocked(record, "branch_changed")
                    if record["state"] == "creating":
                        record["state"] = "active"
                        _save(marker, record)
                    return _public(record, reused=True)
                return _blocked(record, "existing_lifecycle_requires_reconciliation")
            if worktree.exists() or worktree.is_symlink():
                raise ProbeError("path_occupied", "managed path already exists", status="denied")
            if len(_active(root)) >= MAX_ACTIVE_PER_PROJECT:
                raise ProbeError("capacity", "too many active managed worktrees for this project", status="denied")
            target = _branch(root)
            if not target:
                raise ProbeError("head_detached", "canonical project must have a named target branch", status="denied")
            base = _head(root)
            if _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
                raise ProbeError("branch_occupied", "managed branch already exists", status="denied")
            record = {
                "schema": 1, "repository": str(root), "task_ref": ref,
                "worktree": str(worktree), "branch": branch,
                "target_branch": target, "base_sha": base,
                "state": "creating", "created_at": int(time.time()),
            }
            _save(marker, record)
            result = _git(root, "worktree", "add", "-b", branch, "--", str(worktree), base, check=False, timeout=90)
            if result.returncode != 0:
                return _blocked(record, "worktree_creation_failed")
            record["state"] = "active"
            _save(marker, record)
            return _public(record, created=True, needs_integration=True)

        if record is None:
            raise ProbeError("not_found", "no managed worktree belongs to this task", status="not_found")
        if record["state"] == "finished":
            return _public(record, already_finished=True)
        if record["state"] == "creating":
            if not worktree.is_dir() or not _registered(root, worktree):
                return _blocked(record, "creation_incomplete")
            record["state"] = "active"
            _save(marker, record)
        if record["state"] == "integrated":
            expected = action.get("expected_head")
            if mode == "status":
                return _public(record, cleanup_pending=True,
                               worktree_exists=worktree.is_dir())
            if mode != "finish" or not isinstance(expected, str) or not SHA_RE.fullmatch(expected):
                raise ProbeError("invalid_input", "integrated cleanup requires expected_head", status="denied")
            return _retire_integrated(root, worktree, marker, branch, record, expected)
        if worktree.is_symlink() or not worktree.is_dir() or not _registered(root, worktree):
            return _blocked(record, "worktree_missing_or_unregistered")
        if _branch(worktree) != branch:
            return _blocked(record, "branch_changed")
        current = _head(worktree)
        if mode == "status":
            return _public(record, head=current, dirty=_dirty(worktree, include_ignored=True),
                           target_head=_head(root), target_dirty=_dirty(root))
        if mode != "finish":
            raise ProbeError("invalid_input", "supported modes are create/adopt/status/finish")
        expected = action.get("expected_head")
        if not isinstance(expected, str) or not SHA_RE.fullmatch(expected):
            raise ProbeError("invalid_input", "finish requires exact expected_head", status="denied")
        if current != expected:
            return _blocked(record, "expected_head_mismatch", head=current)
        if _dirty(worktree, include_ignored=True):
            return _blocked(record, "uncommitted_or_untracked_work")
        if record.get("adopted") and _in_use(worktree):
            return _blocked(record, "worktree_in_use")
        if _git(root, "merge-base", "--is-ancestor", record["base_sha"], current, check=False).returncode != 0:
            return _blocked(record, "branch_history_rewritten")
        if _branch(root) != record["target_branch"]:
            return _blocked(record, "target_branch_changed")
        if _dirty(root):
            return _blocked(record, "target_checkout_dirty")
        target_before = _head(root)
        integrated = _git(root, "merge-base", "--is-ancestor", current, target_before, check=False).returncode == 0
        if not integrated:
            # Fast-forward when possible; otherwise a normal conflict-free Git
            # merge. On conflicts abort; do not force, reset or delete evidence.
            if _git(root, "merge-base", "--is-ancestor", target_before, current, check=False).returncode == 0:
                merged = _git(root, "merge", "--ff-only", "--", branch, check=False, timeout=90)
            else:
                merged = _git(root, "merge", "--no-ff", "--no-edit", "--", branch, check=False, timeout=90)
            if merged.returncode != 0:
                aborted = _git(root, "merge", "--abort", check=False, timeout=40)
                if aborted.returncode != 0 and _head(root) != target_before:
                    return _blocked(record, "merge_abort_needs_manual_recovery")
                return _blocked(record, "merge_conflict_or_failure")
            integrated = _git(root, "merge-base", "--is-ancestor", current, _head(root), check=False).returncode == 0
            if not integrated:
                return _blocked(record, "merge_outcome_unknown")
        record["state"] = "integrated"
        record["feature_head"] = current
        record["integrated_head"] = _head(root)
        _save(marker, record)
        return _retire_integrated(root, worktree, marker, branch, record, current)


def policy_prompt(access: str | None, task_ref: str) -> str:
    if access != "write":
        return ""
    return (
        "\nPARALLEL WORKTREE LIFECYCLE (owner-approved):\n"
        f"- Current native thread: {task_ref}. Parallel implementation may use one managed isolated Git worktree.\n"
        "- Call direct_ops_v2 with project=<canonical project>, plane=project_action, "
        "operation=managed_worktree. If a separate Git worktree already exists for "
        "this task, use mode=adopt with task_ref=<thread ID>, worktree=<exact "
        "existing absolute path>, expected_branch=<exact branch>, expected_head=<SHA>. "
        "Never create a second copy while adopting one. Otherwise use mode=create "
        "with task_ref=<thread ID>. "
        "This returns exact worktree path, feature branch and target branch.\n"
        "- Make changes and run tests inside that worktree, without editing the canonical "
        "checkout or other agents' worktrees. Commit only the tested intended changes.\n"
        "- After successful tests call managed_worktree mode=finish with the SAME "
        "task_ref and expected_head=verified feature HEAD. This integrates into the "
        "original target branch when its checkout is clean, then removes only this "
        "owned worktree and merged temporary branch. No force deletion.\n"
        "- If target is dirty, histories conflict, tests fail, an agent is active or "
        "cleanup is unsafe: preserve the worktree and commits. Report exact path, branch "
        "and blocker; resume that lifecycle later. Never claim completion or abandon it.\n"
        "- Do not create manual unregistered clones/worktrees and do not prune unrelated ones.\n"
    )
