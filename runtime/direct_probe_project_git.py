from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import stat as statmod
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from direct_ops2 import resolve_github_remote
from workspace_selection import addressable_project_name
from direct_probe_core import (
    MAX_DIFF_BYTES, MAX_FILE_BYTES, MAX_LIST_ENTRIES, MAX_PROCESS_OUTPUT, MAX_SEARCH_BYTES,
    MAX_SEARCH_FILES, MAX_SEARCH_MATCHES, MAX_TEXT_BYTES, MAX_TREE_ENTRIES, HEX40_RE, ProcessResult, ProbeError,
    SKIP_SEARCH_SEGMENTS, _assert_nonsecret_parts, _git, _resolve_commit, _is_binary, _redact_text, _run_process,
    _safe_worktree_path, _sha256_path, _validate_alias, _validate_branch, _validate_ref, _validate_rel_path,
)

async def _head_marker(root: Path) -> dict[str, str | None]:
    head = None
    index = None
    try:
        head = (await _git(root, "rev-parse", "--verify", "HEAD", output_limit=128)).stdout.decode().strip()
    except ProbeError:
        pass
    index_path = root / ".git" / "index"
    try:
        st = index_path.stat()
        index = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        pass
    return {"head": head, "index": index}


async def _git_state(root: Path) -> dict[str, Any]:
    before = await _head_marker(root)
    symbolic = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", ok_codes=(0, 1), output_limit=512)
    branch = symbolic.stdout.decode().strip() if symbolic.exit_code == 0 else None
    head_res = await _git(root, "rev-parse", "--verify", "HEAD", ok_codes=(0, 128), output_limit=256)
    head = head_res.stdout.decode().strip() if head_res.exit_code == 0 else None
    if branch and head:
        state = "branch"
    elif branch and not head:
        state = "unborn"
    elif head:
        state = "detached"
    else:
        state = "unborn"
    staged_res, unstaged_res, untracked_res, conflicts_res = await asyncio.gather(
        _git(root, "diff", "--cached", "--name-only", "-z", "--no-ext-diff", "--no-textconv", "--", output_limit=16 * 1024),
        _git(root, "diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", "--", output_limit=16 * 1024),
        _git(root, "ls-files", "--others", "--exclude-standard", "-z", output_limit=16 * 1024),
        _git(root, "diff", "--name-only", "--diff-filter=U", "-z", "--", output_limit=8 * 1024),
    )
    staged = [x for x in staged_res.stdout.decode("utf-8", "strict").split("\0") if x]
    unstaged = [x for x in unstaged_res.stdout.decode("utf-8", "strict").split("\0") if x]
    untracked = [x for x in untracked_res.stdout.decode().split("\0") if x]
    conflicts = [x for x in conflicts_res.stdout.decode("utf-8", "strict").split("\0") if x]
    complete = not any(x.truncated for x in (staged_res, unstaged_res, untracked_res, conflicts_res))
    upstream = None
    ahead = behind = None
    if branch:
        up = await _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", ok_codes=(0, 128), output_limit=512)
        if up.exit_code == 0:
            upstream = up.stdout.decode().strip()
            counts = await _git(root, "rev-list", "--left-right", "--count", "HEAD...@{upstream}", output_limit=512)
            parts = counts.stdout.decode().split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
    after = await _head_marker(root)
    return {
        "cwd": str(root.resolve()), "head_state": state, "branch": branch, "head": head,
        "staged": staged, "unstaged": unstaged, "untracked": untracked, "conflicts": conflicts,
        "upstream": upstream, "ahead": ahead, "behind": behind,
        "scan_complete": complete, "clean": (not staged and not unstaged and not untracked and not conflicts) if complete else None,
        "changed_during_read": before != after,
    }


async def _gh(binding: dict[str, Any], args: list[str], *, timeout: float = 15.0, limit: int = MAX_PROCESS_OUTPUT) -> ProcessResult:
    gh_path = str(binding.get("executables", {}).get("gh", "/usr/bin/gh"))
    if not gh_path.startswith("/"):
        raise ProbeError("config", "gh executable is not trusted")
    gh = Path(gh_path)
    if not gh.is_file() or not os.access(gh, os.X_OK):
        raise ProbeError("tool_unavailable", f"trusted gh executable is unavailable: {gh_path}")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/home/dev"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    result = await _run_process([gh_path, *args], env=env, timeout=timeout, output_limit=limit)
    if result.exit_code != 0:
        text, _ = _redact_text(result.stderr.decode("utf-8", "replace")[-2000:])
        low = text.lower()
        category = "github_scope" if "resource not accessible" in low or "permission" in low or "scope" in low else "github"
        raise ProbeError(category, text.strip() or f"gh exited {result.exit_code}", exit_code=result.exit_code)
    return result


async def _remote_head(root: Path, binding: dict[str, Any], remote: str, branch: str) -> dict[str, Any]:
    remote = _validate_alias(remote)
    branch = _validate_branch(branch)
    configured_remotes = binding.get("remotes") or {}
    if configured_remotes:
        repo = configured_remotes.get(remote)
        if not repo or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(repo)):
            raise ProbeError("remote_denied", "remote is not bound to a trusted GitHub repository", status="denied")
        url_res = await _git(root, "remote", "get-url", remote, output_limit=2048)
        remote_url = url_res.stdout.decode("utf-8", "replace").strip()
        expected = str(repo).removesuffix(".git")
        trusted_urls = {
            f"https://github.com/{expected}", f"https://github.com/{expected}.git",
            f"git@github.com:{expected}", f"git@github.com:{expected}.git",
            f"ssh://git@github.com/{expected}", f"ssh://git@github.com/{expected}.git",
        }
        if remote_url not in trusted_urls:
            raise ProbeError("remote_drift", "configured Git remote no longer matches the trusted GitHub binding", status="error")
    else:
        repo, remote_url = await resolve_github_remote(root, binding, remote)

    ref = f"refs/heads/{branch}"
    fresh = await _git(root, "ls-remote", "--heads", remote, ref, timeout=12.0, output_limit=2048)
    rows = [line.split("\t", 1) for line in fresh.stdout.decode("utf-8", "replace").splitlines() if line]
    rows = [row for row in rows if len(row) == 2 and row[1] == ref]
    if len(rows) != 1 or not HEX40_RE.fullmatch(rows[0][0].lower()):
        raise ProbeError("github", "fresh remote branch response did not contain exactly one commit sha")
    sha = rows[0][0].lower()

    tracking_ref = f"refs/remotes/{remote}/{branch}"
    tracking = await _git(root, "rev-parse", "--verify", tracking_ref, ok_codes=(0, 128), output_limit=256)
    local_tracking = tracking.stdout.decode().strip() if tracking.exit_code == 0 else None
    return {
        "remote": remote,
        "branch": branch,
        "repository": repo,
        "remote_url_verified": True,
        "fresh_remote_sha": sha,
        "local_tracking_ref": tracking_ref,
        "local_tracking_sha": local_tracking,
        "same_as_local_tracking": (local_tracking == sha) if local_tracking else None,
    }


async def _git_log(root: Path, check: dict[str, Any]) -> dict[str, Any]:
    sha = await _resolve_commit(root, check["ref"])
    limit = min(int(check.get("limit", 10)), 100)
    result = await _git(root, "log", f"-{limit}", "--format=%H%x09%ct%x09%s", sha, "--", output_limit=24 * 1024)
    commits = []
    redacted = False
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        subject, did = _redact_text(parts[2])
        redacted |= did
        commits.append({"sha": parts[0], "committed_unix": int(parts[1]), "subject": subject})
    return {"resolved_ref": sha, "commits": commits, "complete": not result.truncated, "redacted": redacted}


async def _tree(root: Path, check: dict[str, Any]) -> dict[str, Any]:
    sha = await _resolve_commit(root, check["ref"])
    path_raw = check.get("path", "")
    if path_raw:
        rel = _validate_rel_path(path_raw)
        treeish = f"{sha}:{rel.as_posix()}"
    else:
        treeish = sha
    limit = min(int(check.get("limit", 100)), MAX_TREE_ENTRIES)
    result = await _git(root, "ls-tree", "-z", treeish, output_limit=48 * 1024)
    entries = []
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        try:
            meta, path_b = item.split(b"\t", 1)
            mode_b, type_b, object_b = meta.split(b" ", 2)
            name = path_b.decode("utf-8", "strict")
            _assert_nonsecret_parts(tuple(PurePosixPath(name).parts))
        except (ValueError, UnicodeDecodeError, ProbeError):
            continue
        entries.append({"name": name, "mode": mode_b.decode(), "type": type_b.decode(), "object": object_b.decode()})
        if len(entries) >= limit:
            break
    return {"resolved_ref": sha, "path": path_raw or ".", "entries": entries,
            "complete": (not result.truncated and len(entries) < limit), "truncated": result.truncated or len(entries) >= limit}


async def _worktree_list(root: Path) -> dict[str, Any]:
    result = await _git(root, "worktree", "list", "--porcelain", "-z", output_limit=16 * 1024)
    chunks = [x for x in result.stdout.decode("utf-8", "replace").split("\0\0") if x.strip()]
    worktrees = []
    root_real = root.resolve(strict=True)
    for chunk in chunks[:30]:
        row: dict[str, Any] = {}
        for line in chunk.split("\0"):
            if not line:
                continue
            key, _, value = line.partition(" ")
            if key == "worktree": row["path"] = value
            elif key == "HEAD": row["head"] = value
            elif key == "branch": row["branch"] = value.removeprefix("refs/heads/")
            elif key in {"detached", "bare", "locked", "prunable"}: row[key] = value or True
        path = row.get("path")
        if isinstance(path, str):
            try:
                row["current"] = Path(path).resolve(strict=True) == root_real
            except OSError:
                row["current"] = False
            project = addressable_project_name(path)
            if project is not None:
                row["project"] = project
                row["tool_addressable"] = True
        if row:
            worktrees.append(row)
    return {"worktrees": worktrees, "complete": not result.truncated and len(chunks) <= 30}


def _diff_endpoint(root: Path, raw: str) -> tuple[list[str], str]:
    if raw == "WORKTREE":
        return [], "WORKTREE"
    if raw == "INDEX":
        return ["--cached"], "INDEX"
    return [_validate_ref(raw)], raw


async def _diff(root: Path, check: dict[str, Any]) -> dict[str, Any]:
    base_raw, target_raw = check["base"], check["target"]
    path_raw = check.get("path", "")
    if base_raw == "WORKTREE" or target_raw == "INDEX" and base_raw == "WORKTREE":
        raise ProbeError("invalid_diff", "unsupported diff direction")
    path_args: list[str] = []
    if path_raw:
        rel = _validate_rel_path(path_raw)
        path_args = ["--", rel.as_posix()]
    # Supported bounded states: HEAD/commit -> INDEX, INDEX -> WORKTREE, commit -> commit, HEAD/commit -> WORKTREE.
    if target_raw == "INDEX":
        if base_raw in {"INDEX", "WORKTREE"}:
            raise ProbeError("invalid_diff", "INDEX target requires a commit/ref base")
        base_sha = await _resolve_commit(root, base_raw)
        cmd = ["diff", "--cached", "--name-only", "-z", "--no-ext-diff", "--no-textconv", base_sha, *path_args]
        text_cmd = ["diff", "--cached", "--no-ext-diff", "--no-textconv", f"--unified={int(check.get('context', 3))}", base_sha, *path_args]
        resolved = {"base": base_sha, "target": "INDEX"}
    elif base_raw == "INDEX" and target_raw == "WORKTREE":
        cmd = ["diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", *path_args]
        text_cmd = ["diff", "--no-ext-diff", "--no-textconv", f"--unified={int(check.get('context', 3))}", *path_args]
        resolved = {"base": "INDEX", "target": "WORKTREE"}
    elif target_raw == "WORKTREE":
        if base_raw in {"INDEX", "WORKTREE"}:
            raise ProbeError("invalid_diff", "WORKTREE target needs ref base or INDEX")
        base_sha = await _resolve_commit(root, base_raw)
        cmd = ["diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", base_sha, *path_args]
        text_cmd = ["diff", "--no-ext-diff", "--no-textconv", f"--unified={int(check.get('context', 3))}", base_sha, *path_args]
        resolved = {"base": base_sha, "target": "WORKTREE"}
    else:
        base_sha = await _resolve_commit(root, base_raw)
        target_sha = await _resolve_commit(root, target_raw)
        cmd = ["diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", base_sha, target_sha, *path_args]
        text_cmd = ["diff", "--no-ext-diff", "--no-textconv", f"--unified={int(check.get('context', 3))}", base_sha, target_sha, *path_args]
        resolved = {"base": base_sha, "target": target_sha}
    names_res = await _git(root, *cmd, output_limit=16 * 1024)
    if names_res.truncated:
        raise ProbeError("incomplete_scan", "diff path scan exceeded limit", status="partial")
    paths = [p for p in names_res.stdout.decode("utf-8", "strict").split("\0") if p]
    for p in paths:
        _validate_rel_path(p)
    text_res = await _git(root, *text_cmd, output_limit=MAX_DIFF_BYTES)
    text = text_res.stdout.decode("utf-8", "replace")
    text, redacted = _redact_text(text)
    return {**resolved, "paths": paths, "text": text, "complete": not text_res.truncated,
            "truncated": text_res.truncated, "redacted": redacted}


async def _git_blob(root: Path, commit: str, raw_path: str) -> tuple[bytes, str, str]:
    rel = _validate_rel_path(raw_path)
    sha = await _resolve_commit(root, commit)
    spec = f"{sha}:{rel.as_posix()}"
    object_res = await _git(root, "rev-parse", "--verify", spec, output_limit=256)
    object_id = object_res.stdout.decode().strip()
    type_res = await _git(root, "cat-file", "-t", object_id, output_limit=64)
    if type_res.stdout.decode().strip() != "blob":
        raise ProbeError("file_type", "git object is not a file blob", status="denied")
    size_res = await _git(root, "cat-file", "-s", object_id, output_limit=64)
    size = int(size_res.stdout.decode().strip())
    if size > MAX_FILE_BYTES:
        raise ProbeError("file_too_large", "git blob exceeds text read limit", status="denied")
    content = await _git(root, "cat-file", "blob", object_id, output_limit=MAX_FILE_BYTES + 1)
    if content.truncated:
        raise ProbeError("file_too_large", "git blob exceeds text read limit", status="denied")
    return content.stdout, sha, object_id


async def _remote_blob(binding: dict[str, Any], remote: str, ref: str, raw_path: str) -> tuple[bytes, str, str | None]:
    remote = _validate_alias(remote)
    rel = _validate_rel_path(raw_path)
    repo = (binding.get("remotes") or {}).get(remote)
    if not repo:
        raise ProbeError("remote_denied", "remote is not bound to GitHub", status="denied")
    ref = _validate_ref(ref)
    resolved = await _gh(binding, ["api", "--method", "GET", f"repos/{repo}/commits/{quote(ref, safe='')}", "--jq", ".sha"], limit=512)
    sha = resolved.stdout.decode().strip().lower()
    if not HEX40_RE.fullmatch(sha):
        raise ProbeError("github", "remote ref did not resolve to exact commit")
    response = await _gh(binding, ["api", "--method", "GET", f"repos/{repo}/contents/{quote(rel.as_posix(), safe='/')}?ref={sha}"], limit=MAX_FILE_BYTES * 2 + 8192)
    try:
        payload = json.loads(response.stdout.decode("utf-8"))
        if payload.get("type") != "file":
            raise ProbeError("file_type", "remote object is not a file", status="denied")
        size = int(payload.get("size", 0))
        if size > MAX_FILE_BYTES:
            raise ProbeError("file_too_large", "remote file exceeds text read limit", status="denied")
        data = base64.b64decode(str(payload.get("content", "")).replace("\n", ""), validate=True)
        blob_sha = payload.get("sha")
    except (ValueError, KeyError, json.JSONDecodeError, base64.binascii.Error) as exc:
        if isinstance(exc, ProbeError):
            raise
        raise ProbeError("github", "remote file response could not be decoded") from exc
    return data, sha, blob_sha


def _line_page(text: str, start: int, end: int | None) -> tuple[str, int, int, bool]:
    lines = text.splitlines()
    total = len(lines)
    start = max(1, start)
    end = min(total, end if end is not None else start + 199)
    if end < start:
        raise ProbeError("invalid_input", "end_line must be >= start_line")
    if end - start + 1 > 200:
        end = start + 199
    selected = "\n".join(lines[start - 1:end])
    encoded = selected.encode("utf-8")
    truncated = False
    if len(encoded) > MAX_TEXT_BYTES:
        selected = encoded[:MAX_TEXT_BYTES].decode("utf-8", "ignore")
        truncated = True
    return selected, start, end, truncated


async def _read_text(root: Path, binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    source = check.get("source", "worktree")
    raw_path = check["path"]
    start = int(check.get("start_line", 1))
    end = check.get("end_line")
    if source == "worktree":
        before = await _head_marker(root)
        path = _safe_worktree_path(root, raw_path, require_file=True)
        st_before = path.stat()
        if st_before.st_size > MAX_FILE_BYTES:
            raise ProbeError("file_too_large", "file exceeds text read limit", status="denied")
        data = path.read_bytes()
        st_after = path.stat()
        version: dict[str, Any] = {"source": "worktree", "head": before["head"], "sha256": hashlib.sha256(data).hexdigest(),
                                   "mtime_ns": st_before.st_mtime_ns, "size": len(data)}
        changed = (st_before.st_mtime_ns, st_before.st_size) != (st_after.st_mtime_ns, st_after.st_size) or before != await _head_marker(root)
        blob_sha = None
    elif source == "local_commit":
        if "ref" not in check:
            raise ProbeError("invalid_input", "local_commit read requires ref")
        data, sha, blob_sha = await _git_blob(root, check["ref"], raw_path)
        version = {"source": "local_commit", "commit": sha, "blob": blob_sha}
        changed = False
    elif source == "remote_commit":
        if "ref" not in check or "remote" not in check:
            raise ProbeError("invalid_input", "remote_commit read requires ref and remote")
        data, sha, blob_sha = await _remote_blob(binding, check["remote"], check["ref"], raw_path)
        version = {"source": "remote_commit", "commit": sha, "blob": blob_sha, "remote": check["remote"]}
        changed = False
    else:
        raise ProbeError("invalid_input", "unknown source")
    if _is_binary(data):
        raise ProbeError("binary_denied", "binary/non-UTF8 content is not returned", status="denied")
    text = data.decode("utf-8")
    page, actual_start, actual_end, truncated = _line_page(text, start, int(end) if end is not None else None)
    page, redacted = _redact_text(page)
    return {"path": raw_path, "version": version, "start_line": actual_start, "end_line": actual_end,
            "text": page, "complete": not truncated, "truncated": truncated, "redacted": redacted,
            "changed_during_read": changed}
