from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat as statmod
import tempfile
from pathlib import Path
from typing import Any

from direct_probe_core import (
    ProbeError,
    _git,
    _git_env,
    _redact_text,
    _resolve_commit,
    _run_process,
    _sha256_path,
    _validate_alias,
    _validate_branch,
    _validate_rel_path,
)
from source_handoff import stage_source_handoff

_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
_BRANCH_WRITE_RE = re.compile(r"^chatgpt/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,199}$")
MAX_DIRECT_WRITE_BYTES = 2 * 1024 * 1024


def github_repo_from_url(url: str) -> str | None:
    match = _GITHUB_REMOTE_RE.fullmatch(url.strip())
    return match.group(1) if match else None


async def resolve_github_remote(root: Path, binding: dict[str, Any], alias: str) -> tuple[str, str]:
    alias = _validate_alias(alias)
    result = await _git(root, "remote", "get-url", alias, output_limit=2048)
    url = result.stdout.decode("utf-8", "replace").strip()
    actual = github_repo_from_url(url)
    if actual is None:
        raise ProbeError("remote_denied", "Git remote is not a supported GitHub repository", status="denied")
    configured = (binding.get("remotes") or {}).get(alias)
    if configured is not None and configured != actual:
        raise ProbeError("remote_drift", "configured Git remote no longer matches trusted binding", status="denied")
    return actual, url


async def discover_github_remotes(root: Path) -> dict[str, str]:
    result = await _git(root, "remote", output_limit=4096)
    found: dict[str, str] = {}
    for raw in result.stdout.decode("utf-8", "replace").splitlines()[:32]:
        if not raw.strip():
            continue
        try:
            alias = _validate_alias(raw.strip())
            repo, _url = await resolve_github_remote(root, {}, alias)
        except ProbeError:
            continue
        found[alias] = repo
    return found


async def project_capabilities(root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    sections: dict[str, list[str]] = {}
    for key in (
        "project_actions", "host_actions", "services", "logs", "health", "runtimes",
        "runtime_files", "roots", "host_files",
    ):
        raw = binding.get(key) or {}
        sections[key] = sorted(raw) if isinstance(raw, dict) else []
    return {
        "github_remotes": await discover_github_remotes(root),
        "configured_github_repo": binding.get("github_repo"),
        **sections,
        "direct_project_actions": [
            "sync_ff", "run_tool", "run_profile", "write_text", "mkdir", "move_path",
            "delete_file", "chmod_executable", "git_create_branch", "git_stage",
            "git_commit_push", "capture_handoff", "worktree_capture_handoff", "worktree_reconcile", "owned_service_action", "adb", "github_pr_create", "github_pr_update", "github_pr_merge", "github_branch_delete", "job_start", "job_status", "job_cancel", "job_cleanup", "batch", "handoff_commit_push",
        ],
        "direct_host_actions": [
            "restart_service", "service_action", "run_profile", "write_registered_file",
        ],
        "runners": [
            "pytest", "bash_script", "python_script", "node_script", "npm_script",
            "pnpm_script", "yarn_script", "gradle_task", "make_target", "python_module",
        ],
    }


def _parse_tree(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        try:
            meta, path_b = item.split(b"\t", 1)
            _mode, typ, obj = meta.split(b" ", 2)
            path = path_b.decode("utf-8", "strict")
            if typ != b"blob":
                continue
            _validate_rel_path(path)
        except (ValueError, UnicodeDecodeError, ProbeError):
            continue
        out[path] = obj.decode("ascii")
    return out


def _parse_index(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        try:
            meta, path_b = item.split(b"\t", 1)
            _mode, obj, stage = meta.split(b" ", 2)
            path = path_b.decode("utf-8", "strict")
            if stage != b"0":
                continue
            _validate_rel_path(path)
        except (ValueError, UnicodeDecodeError, ProbeError):
            continue
        out[path] = obj.decode("ascii")
    return out


def _raw_blob_hash(path: Path) -> tuple[str, str, int]:
    st = path.lstat()
    if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISREG(st.st_mode):
        raise ProbeError("file_type", "workspace comparison supports regular files only", status="denied")
    sha1 = hashlib.sha1()
    sha1.update(f"blob {st.st_size}\0".encode("ascii"))
    sha256 = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        while True:
            chunk = handle.read(128 * 1024)
            if not chunk:
                break
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest(), sha256.hexdigest(), st.st_size


async def workspace_compare(root: Path, target_ref: str, *, include_untracked: bool = True, limit: int = 500) -> dict[str, Any]:
    target = await _resolve_commit(root, target_ref)
    head = (await _git(root, "rev-parse", "--verify", "HEAD", output_limit=256)).stdout.decode().strip()
    target_tree, head_tree, index_rows = await asyncio.gather(
        _git(root, "ls-tree", "-r", "-z", target, output_limit=4 * 1024 * 1024),
        _git(root, "ls-tree", "-r", "-z", head, output_limit=4 * 1024 * 1024),
        _git(root, "ls-files", "-s", "-z", output_limit=4 * 1024 * 1024),
    )
    target_map = _parse_tree(target_tree.stdout)
    head_map = _parse_tree(head_tree.stdout)
    index_map = _parse_index(index_rows.stdout)
    untracked: set[str] = set()
    if include_untracked:
        rows = await _git(root, "ls-files", "--others", "--exclude-standard", "-z", output_limit=4 * 1024 * 1024)
        for item in rows.stdout.decode("utf-8", "replace").split("\0"):
            if item:
                try:
                    untracked.add(_validate_rel_path(item).as_posix())
                except ProbeError:
                    pass
    paths = sorted(set(target_map) | set(head_map) | set(index_map) | untracked)
    truncated = len(paths) > limit
    entries = []
    counts: dict[str, int] = {}
    root_real = root.resolve(strict=True)
    for rel in paths[:limit]:
        work_oid = work_sha256 = None
        size = None
        path = root_real.joinpath(*_validate_rel_path(rel).parts)
        if path.exists() and not path.is_symlink():
            try:
                work_oid, work_sha256, size = _raw_blob_hash(path)
            except ProbeError:
                pass
        target_oid = target_map.get(rel)
        head_oid = head_map.get(rel)
        index_oid = index_map.get(rel)
        if rel in untracked and index_oid is None:
            classification = "untracked"
        elif work_oid is None and target_oid is not None:
            classification = "deleted_local"
        elif work_oid == target_oid and target_oid is not None:
            classification = "same_as_target"
        elif target_oid is None and work_oid is not None:
            classification = "local_only"
        elif head_oid == target_oid and work_oid != head_oid:
            classification = "modified_local"
        elif work_oid == head_oid and head_oid != target_oid:
            classification = "head_differs_target"
        elif index_oid != head_oid and work_oid == index_oid:
            classification = "staged"
        else:
            classification = "diverged"
        counts[classification] = counts.get(classification, 0) + 1
        entries.append({
            "path": rel, "classification": classification, "target_blob": target_oid,
            "head_blob": head_oid, "index_blob": index_oid, "worktree_raw_blob": work_oid,
            "worktree_sha256": work_sha256, "size": size,
        })
    return {
        "target": target, "head": head, "entries": entries, "counts": counts,
        "complete": not truncated, "truncated": truncated,
        "note": "worktree_raw_blob hashes raw bytes and intentionally does not execute Git filters",
    }


def _mutation_path(root: Path, raw: str) -> Path:
    rel = _validate_rel_path(raw)
    if rel.parts and rel.parts[0] == ".git":
        raise ProbeError("path_denied", ".git mutations are denied", status="denied")
    root_real = root.resolve(strict=True)
    current = root_real
    for part in rel.parts[:-1]:
        current = current / part
        if not current.exists():
            raise ProbeError("not_found", "parent directory does not exist", status="not_found")
        st = current.lstat()
        if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISDIR(st.st_mode):
            raise ProbeError("path_denied", "parent path is not a regular directory", status="denied")
    path = root_real.joinpath(*rel.parts)
    if path.exists() or path.is_symlink():
        if statmod.S_ISLNK(path.lstat().st_mode):
            raise ProbeError("symlink_escape", "symlink mutation is denied", status="denied")
    return path


def _expected_file(path: Path, expected_sha256: str | None) -> str | None:
    if not path.exists():
        if expected_sha256 is not None:
            raise ProbeError("precondition_failed", "expected file is absent", status="denied")
        return None
    if not path.is_file() or path.is_symlink():
        raise ProbeError("file_type", "only regular files may be mutated", status="denied")
    current = _sha256_path(path, MAX_DIRECT_WRITE_BYTES * 8)
    if expected_sha256 is not None and current != expected_sha256:
        raise ProbeError("precondition_failed", "file SHA-256 does not match expected value", status="denied")
    return current


def write_text(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    data = action["content"].encode("utf-8")
    if len(data) > MAX_DIRECT_WRITE_BYTES:
        raise ProbeError("file_too_large", "direct write exceeds size limit", status="denied")
    path = _mutation_path(root, action["path"])
    if action.get("must_not_exist") and path.exists():
        raise ProbeError("precondition_failed", "target already exists", status="denied")
    old_sha = _expected_file(path, action.get("expected_sha256"))
    old_mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    fd, temp_name = tempfile.mkstemp(prefix=".devcoveer-write-", dir=path.parent)
    try:
        os.fchmod(fd, old_mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return {
        "status": "ok", "action": "write_text", "path": action["path"],
        "old_sha256": old_sha, "new_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
    }


def mkdir(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    rel = _validate_rel_path(action["path"])
    if rel.parts and rel.parts[0] == ".git":
        raise ProbeError("path_denied", ".git mutations are denied", status="denied")
    root_real = root.resolve(strict=True)
    target = root_real.joinpath(*rel.parts)
    if action.get("parents", False):
        current = root_real
        created = []
        for part in rel.parts:
            current = current / part
            if current.exists():
                st = current.lstat()
                if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISDIR(st.st_mode):
                    raise ProbeError("path_denied", "mkdir path collides with non-directory", status="denied")
            else:
                current.mkdir(mode=0o755)
                created.append(str(current.relative_to(root_real)))
        return {"status": "ok", "action": "mkdir", "path": action["path"], "created": created}
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise ProbeError("not_found", "parent directory does not exist", status="not_found")
    if target.exists():
        if target.is_dir() and not target.is_symlink():
            return {"status": "ok", "action": "mkdir", "path": action["path"], "created": []}
        raise ProbeError("path_denied", "mkdir target is not a directory", status="denied")
    target.mkdir(mode=0o755)
    return {"status": "ok", "action": "mkdir", "path": action["path"], "created": [action["path"]]}


def delete_file(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    path = _mutation_path(root, action["path"])
    old_sha = _expected_file(path, action.get("expected_sha256"))
    if old_sha is None:
        raise ProbeError("not_found", "file does not exist", status="not_found")
    path.unlink()
    return {"status": "ok", "action": "delete_file", "path": action["path"], "old_sha256": old_sha}


def move_path(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    source = _mutation_path(root, action["source"])
    target = _mutation_path(root, action["target"])
    old_sha = _expected_file(source, action.get("expected_sha256"))
    if old_sha is None:
        raise ProbeError("not_found", "source file does not exist", status="not_found")
    if target.exists() or target.is_symlink():
        raise ProbeError("precondition_failed", "move target already exists", status="denied")
    os.replace(source, target)
    return {
        "status": "ok", "action": "move_path", "source": action["source"],
        "target": action["target"], "sha256": old_sha,
    }


def chmod_executable(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    path = _mutation_path(root, action["path"])
    _expected_file(path, action.get("expected_sha256"))
    mode = path.stat().st_mode & 0o777
    new_mode = (mode | 0o111) if action["executable"] else (mode & ~0o111)
    path.chmod(new_mode)
    return {
        "status": "ok", "action": "chmod_executable", "path": action["path"],
        "old_mode": oct(mode), "new_mode": oct(new_mode),
    }


async def git_create_branch(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    branch = _validate_branch(action["branch"])
    if _BRANCH_WRITE_RE.fullmatch(branch) is None:
        raise ProbeError("branch_denied", "direct branch creation is limited to chatgpt/*", status="denied")
    start = await _resolve_commit(root, action.get("start_ref", "HEAD"))
    existing = await _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", ok_codes=(0, 1))
    if existing.exit_code == 0:
        raise ProbeError("branch_exists", "target branch already exists", status="denied")
    changed = await _git(root, "switch", "-c", branch, start, ok_codes=(0, 1), timeout=30)
    if changed.exit_code != 0:
        err, _ = _redact_text(changed.stderr.decode("utf-8", "replace"))
        raise ProbeError("branch_create", err[-1200:] or "could not create branch", exit_code=changed.exit_code)
    return {"status": "ok", "action": "git_create_branch", "branch": branch, "start_sha": start}


async def git_stage(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    rels = [_validate_rel_path(p).as_posix() for p in action["paths"]]
    result = await _git(root, "add", "-A", "--", *rels, timeout=60, output_limit=32 * 1024)
    if result.exit_code != 0:
        err, _ = _redact_text(result.stderr.decode("utf-8", "replace"))
        raise ProbeError("git_stage", err[-1200:] or "git add failed", exit_code=result.exit_code)
    staged = await _git(root, "diff", "--cached", "--name-only", "-z", "--", *rels, output_limit=64 * 1024)
    return {"status": "ok", "action": "git_stage",
            "paths": [x for x in staged.stdout.decode("utf-8", "replace").split("\0") if x]}


async def git_commit_push(root: Path, binding: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    branch_res = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", ok_codes=(0, 1), output_limit=512)
    branch = branch_res.stdout.decode().strip() if branch_res.exit_code == 0 else ""
    if _BRANCH_WRITE_RE.fullmatch(branch) is None:
        raise ProbeError("branch_denied", "direct commit/push requires current chatgpt/* branch", status="denied")
    staged = await _git(root, "diff", "--cached", "--quiet", ok_codes=(0, 1))
    if staged.exit_code == 0:
        raise ProbeError("empty_commit", "no staged changes", status="denied")
    check = await _git(root, "diff", "--cached", "--check", ok_codes=(0, 2), timeout=30)
    if check.exit_code != 0:
        raise ProbeError("patch_check", check.stdout.decode("utf-8", "replace")[-1200:] or "staged patch check failed", status="denied")
    committed = await _git(root, "-c", "commit.gpgsign=false", "commit", "-m", action["commit_message"].strip(), ok_codes=(0, 1), timeout=60)
    if committed.exit_code != 0:
        err, _ = _redact_text(committed.stderr.decode("utf-8", "replace"))
        raise ProbeError("commit_failed", err[-1200:] or "git commit failed", exit_code=committed.exit_code)
    head = (await _git(root, "rev-parse", "HEAD", output_limit=256)).stdout.decode().strip()
    remote = _validate_alias(action.get("remote", "origin"))
    repo, _url = await resolve_github_remote(root, binding, remote)
    pushed = await _git(root, "push", remote, f"HEAD:refs/heads/{branch}", ok_codes=(0, 1), timeout=120)
    if pushed.exit_code != 0:
        err, _ = _redact_text(pushed.stderr.decode("utf-8", "replace"))
        raise ProbeError("push_failed", err[-1200:] or "git push failed", exit_code=pushed.exit_code)
    return {"status": "ok", "action": "git_commit_push", "branch": branch, "commit_sha": head, "repository": repo}


async def capture_handoff(root: Path, project_id: str, action: dict[str, Any]) -> dict[str, Any]:
    base = await _resolve_commit(root, action["base_sha"])
    selected = None
    path_args: list[str] = []
    if action.get("paths"):
        selected = {_validate_rel_path(p).as_posix() for p in action["paths"]}
        path_args = ["--", *sorted(selected)]
    changed_names = await _git(root, "diff", "--name-only", "-z", base, *path_args, output_limit=4 * 1024 * 1024)
    for raw in changed_names.stdout.decode("utf-8", "replace").split("\0"):
        if raw:
            _validate_rel_path(raw)
    tracked = await _git(root, "diff", "--binary", "--no-ext-diff", "--no-textconv", base, *path_args,
                         timeout=60, output_limit=16 * 1024 * 1024)
    patch_parts = [tracked.stdout]
    captured_untracked = []
    if action.get("include_untracked", True):
        others = await _git(root, "ls-files", "--others", "--exclude-standard", "-z", output_limit=4 * 1024 * 1024)
        for raw in others.stdout.decode("utf-8", "replace").split("\0"):
            if not raw:
                continue
            rel = _validate_rel_path(raw).as_posix()
            if selected is not None and rel not in selected:
                continue
            proc = await _run_process(
                ["/usr/bin/git", "-C", str(root), "diff", "--no-index", "--binary",
                 "--no-ext-diff", "--no-textconv", "--", "/dev/null", rel],
                env=_git_env(), timeout=30, output_limit=4 * 1024 * 1024,
            )
            if proc.exit_code not in (0, 1):
                raise ProbeError("capture_failed", "could not encode untracked file", exit_code=proc.exit_code)
            if proc.stdout:
                patch_parts.append(proc.stdout)
                captured_untracked.append(rel)
    patch_bytes = b"".join(patch_parts)
    if not patch_bytes:
        raise ProbeError("empty_patch", "workspace has no selected changes", status="denied")
    try:
        patch_text = patch_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeError("binary_patch_encoding", "workspace patch is not UTF-8 encodable") from exc
    patch_sha = hashlib.sha256(patch_bytes).hexdigest()
    staged = await asyncio.to_thread(stage_source_handoff, root, project_id, base, patch_text, patch_sha)
    staged["capturedUntracked"] = captured_untracked
    staged["capturedFromHead"] = (await _git(root, "rev-parse", "HEAD", output_limit=256)).stdout.decode().strip()
    return staged


async def service_action(binding: dict[str, Any], service_alias: str, operation: str) -> dict[str, Any]:
    alias = _validate_alias(service_alias)
    cfg = (binding.get("services") or {}).get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "service is not registered", status="denied")
    unit = cfg.get("unit")
    if cfg.get("scope", "user") != "user" or not isinstance(unit, str) or re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", unit) is None:
        raise ProbeError("target_denied", "only registered user services may be controlled", status="denied")
    if operation not in {"start", "stop", "restart", "reload", "enable", "disable"}:
        raise ProbeError("invalid_input", "unsupported service operation")
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/home/dev"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
    }
    if operation in {"stop", "restart"}:
        token = hashlib.sha256(f"{unit}\0{operation}\0{os.getpid()}".encode()).hexdigest()[:12]
        transient = f"devcoveer-direct-{operation}-{token}"
        result = await _run_process(
            ["/usr/bin/systemd-run", "--user", "--quiet", "--collect", f"--unit={transient}",
             "--on-active=1s", "/usr/bin/systemctl", "--user", operation, unit],
            env=env, timeout=15, output_limit=16 * 1024,
        )
        if result.exit_code != 0:
            raise ProbeError("service_action", f"could not schedule {operation}", exit_code=result.exit_code)
        return {"status": "ok", "action": "service_action", "service": alias, "operation": operation,
                "unit": unit, "scheduled_after_seconds": 1, "transient_unit": transient}
    result = await _run_process(["/usr/bin/systemctl", "--user", operation, unit], env=env,
                                timeout=30, output_limit=16 * 1024)
    if result.exit_code != 0:
        err, _ = _redact_text(result.stderr.decode("utf-8", "replace"))
        raise ProbeError("service_action", err[-1200:] or f"systemctl {operation} failed", exit_code=result.exit_code)
    return {"status": "ok", "action": "service_action", "service": alias, "operation": operation, "unit": unit}


def write_registered_file(binding: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    alias = _validate_alias(action["file"])
    cfg = (binding.get("host_files") or {}).get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "host file is not registered", status="denied")
    raw_path = cfg.get("path")
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        raise ProbeError("config", "registered host file path is invalid")
    path = Path(raw_path)
    if path.is_symlink():
        raise ProbeError("symlink_escape", "registered host file is a symlink", status="denied")
    data = action["content"].encode("utf-8")
    if len(data) > MAX_DIRECT_WRITE_BYTES:
        raise ProbeError("file_too_large", "registered host write exceeds size limit", status="denied")
    old_sha = None
    if path.exists():
        if not path.is_file():
            raise ProbeError("file_type", "registered host target is not a regular file", status="denied")
        old_sha = _sha256_path(path, MAX_DIRECT_WRITE_BYTES * 8)
    expected = action.get("expected_sha256")
    if expected is not None and old_sha != expected:
        raise ProbeError("precondition_failed", "host file SHA-256 does not match expected value", status="denied")
    if not path.parent.is_dir():
        raise ProbeError("not_found", "registered host file parent does not exist", status="not_found")
    mode = int(cfg.get("mode", 0o644))
    fd, temp_name = tempfile.mkstemp(prefix=".devcoveer-host-write-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return {"status": "ok", "action": "write_registered_file", "file": alias, "old_sha256": old_sha,
            "new_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def runner_argv(root: Path, action: dict[str, Any], cwd: Path) -> list[str] | None:
    tool = action["tool"]
    task = action.get("script")
    args = action.get("args") or []
    if tool == "pnpm_script":
        if not isinstance(task, str) or _TASK_RE.fullmatch(task) is None:
            raise ProbeError("invalid_input", "pnpm_script requires a safe script name")
        for candidate in ("/home/dev/.local/bin/pnpm", "/usr/local/bin/pnpm", "/usr/bin/pnpm"):
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return [candidate, "run", "--silent", task, "--", *args]
        raise ProbeError("tool_unavailable", "pnpm is not installed", status="unavailable")
    if tool == "yarn_script":
        if not isinstance(task, str) or _TASK_RE.fullmatch(task) is None:
            raise ProbeError("invalid_input", "yarn_script requires a safe script name")
        for candidate in ("/home/dev/.local/bin/yarn", "/usr/local/bin/yarn", "/usr/bin/yarn"):
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return [candidate, "run", "--silent", task, *args]
        raise ProbeError("tool_unavailable", "yarn is not installed", status="unavailable")
    if tool == "gradle_task":
        if not isinstance(task, str) or _TASK_RE.fullmatch(task) is None:
            raise ProbeError("invalid_input", "gradle_task requires a safe task name")
        wrapper = cwd / "gradlew"
        if not wrapper.is_file() or wrapper.is_symlink():
            raise ProbeError("not_found", "gradlew is missing", status="not_found")
        return ["/usr/bin/bash", str(wrapper), task, *args]
    if tool == "make_target":
        if not isinstance(task, str) or _TASK_RE.fullmatch(task) is None:
            raise ProbeError("invalid_input", "make_target requires a safe target")
        if not (cwd / "Makefile").is_file():
            raise ProbeError("not_found", "Makefile is missing", status="not_found")
        return ["/usr/bin/make", task, *args]
    if tool == "python_module":
        if not isinstance(task, str) or _MODULE_RE.fullmatch(task) is None:
            raise ProbeError("invalid_input", "python_module requires a safe module name")
        return ["/usr/bin/python3", "-m", task, *args]
    return None
