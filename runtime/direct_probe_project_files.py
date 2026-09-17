from __future__ import annotations

import asyncio
import hashlib
import os
import stat as statmod
from pathlib import Path, PurePosixPath
from typing import Any

from direct_probe_core import (
    MAX_FILE_BYTES, MAX_LIST_ENTRIES, MAX_SEARCH_BYTES, MAX_SEARCH_FILES, MAX_SEARCH_MATCHES,
    ProbeError, SKIP_SEARCH_SEGMENTS, _assert_nonsecret_parts, _git, _is_binary, _redact_text,
    _safe_worktree_path, _sha256_path,
)
from direct_probe_project_git import _head_marker

async def _tracked_ignored(root: Path, rel: str) -> tuple[bool, bool]:
    tracked_res, ignored_res = await asyncio.gather(
        _git(root, "ls-files", "--error-unmatch", "--", rel, ok_codes=(0, 1), output_limit=256),
        _git(root, "check-ignore", "-q", "--", rel, ok_codes=(0, 1), output_limit=64),
    )
    return tracked_res.exit_code == 0, ignored_res.exit_code == 0

async def _file_stat(root: Path, raw: str) -> dict[str, Any]:
    path = _safe_worktree_path(root, raw)
    st = path.stat()
    rel = path.relative_to(root.resolve()).as_posix()
    tracked, ignored = await _tracked_ignored(root, rel)
    return {"path": rel, "type": "file" if path.is_file() else "directory", "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "mode": statmod.S_IMODE(st.st_mode), "tracked": tracked, "ignored": ignored}

async def _file_sha(root: Path, raw: str) -> dict[str, Any]:
    path = _safe_worktree_path(root, raw, require_file=True)
    st_before = path.stat()
    if st_before.st_size > 128 * 1024 * 1024:
        raise ProbeError("file_too_large", "file exceeds hash size limit", status="denied")
    digest = await asyncio.to_thread(_sha256_path, path, 128 * 1024 * 1024)
    st_after = path.stat()
    return {"path": path.relative_to(root.resolve()).as_posix(), "sha256": digest, "size": st_before.st_size,
            "mtime_ns": st_before.st_mtime_ns, "changed_during_read": (st_before.st_size, st_before.st_mtime_ns) != (st_after.st_size, st_after.st_mtime_ns)}

def _skip_search_path(rel: PurePosixPath) -> bool:
    return any(part.lower() in SKIP_SEARCH_SEGMENTS for part in rel.parts)

async def _search(root: Path, check: dict[str, Any]) -> dict[str, Any]:
    query = check["query"]
    mode = check.get("mode", "content")
    limit = min(int(check.get("limit", 40)), MAX_SEARCH_MATCHES)
    roots = check.get("paths") or [""]
    base_root = root.resolve()
    starting: list[Path] = []
    for raw in roots:
        starting.append(base_root if raw == "" else _safe_worktree_path(root, raw))
    before = await _head_marker(root)
    matches: list[dict[str, Any]] = []
    scanned_files = 0
    scanned_bytes = 0
    skipped = 0
    complete = True

    def candidates(start: Path):
        nonlocal skipped
        if start.is_file():
            yield start
            return
        for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
            d = Path(dirpath)
            kept: list[str] = []
            for name in dirnames:
                child = d / name
                try:
                    rel = PurePosixPath(child.relative_to(base_root).as_posix())
                    if _skip_search_path(rel):
                        skipped += 1
                        continue
                    _assert_nonsecret_parts(rel.parts)
                    if statmod.S_ISLNK(child.lstat().st_mode):
                        skipped += 1
                        continue
                except (OSError, ValueError, ProbeError):
                    skipped += 1
                    continue
                kept.append(name)
            dirnames[:] = kept
            for name in filenames:
                yield d / name

    for start in starting:
        for path in candidates(start):
            try:
                rel = PurePosixPath(path.relative_to(base_root).as_posix())
                if _skip_search_path(rel):
                    skipped += 1
                    continue
                _assert_nonsecret_parts(rel.parts)
                lst = path.lstat()
                if statmod.S_ISLNK(lst.st_mode) or not statmod.S_ISREG(lst.st_mode):
                    continue
            except (OSError, ProbeError, ValueError):
                skipped += 1
                continue
            scanned_files += 1
            if scanned_files > MAX_SEARCH_FILES:
                complete = False
                break
            size = lst.st_size
            scanned_bytes += size
            if scanned_bytes > MAX_SEARCH_BYTES:
                complete = False
                break
            if mode == "name":
                if query in rel.name:
                    matches.append({"path": rel.as_posix(), "line": None, "text": None})
            else:
                if size > MAX_FILE_BYTES:
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    skipped += 1
                    continue
                if _is_binary(data):
                    continue
                for lineno, line in enumerate(data.decode("utf-8").splitlines(), 1):
                    if query in line:
                        snippet, did = _redact_text(line[:1000])
                        matches.append({"path": rel.as_posix(), "line": lineno, "text": snippet, "redacted": did})
                        if len(matches) >= limit:
                            break
            if len(matches) >= limit:
                complete = False
                break
        if not complete or len(matches) >= limit:
            break
    cache: dict[str, str] = {}
    for item in matches:
        pth = base_root / item["path"]
        if item["path"] not in cache:
            try:
                cache[item["path"]] = hashlib.sha256(pth.read_bytes()).hexdigest()
            except OSError:
                cache[item["path"]] = "unavailable"
        item["sha256"] = cache[item["path"]]
    after = await _head_marker(root)
    return {"query": query, "mode": mode, "matches": matches, "confirmed_no_matches": complete and not matches,
            "scan_complete": complete, "truncated": not complete, "scanned_files": min(scanned_files, MAX_SEARCH_FILES),
            "scanned_bytes": min(scanned_bytes, MAX_SEARCH_BYTES), "skipped": skipped, "changed_during_read": before != after}

async def _file_list(root: Path, check: dict[str, Any]) -> dict[str, Any]:
    raw = check.get("path", "")
    base = root.resolve() if raw == "" else _safe_worktree_path(root, raw)
    depth = min(int(check.get("depth", 1)), 4)
    limit = min(int(check.get("limit", 100)), MAX_LIST_ENTRIES)
    base_depth = len(base.parts)
    entries = []
    complete = True
    for path in base.rglob("*") if base.is_dir() else [base]:
        rel_depth = len(path.parts) - base_depth
        if rel_depth > depth:
            continue
        try:
            rel = PurePosixPath(path.relative_to(root.resolve()).as_posix())
            if _skip_search_path(rel):
                continue
            _assert_nonsecret_parts(rel.parts)
            lst = path.lstat()
            if statmod.S_ISLNK(lst.st_mode):
                ftype = "symlink"
            elif statmod.S_ISREG(lst.st_mode):
                ftype = "file"
            elif statmod.S_ISDIR(lst.st_mode):
                ftype = "directory"
            else:
                continue
        except (OSError, ValueError, ProbeError):
            continue
        entries.append({"path": rel.as_posix(), "type": ftype, "size": lst.st_size, "mtime_ns": lst.st_mtime_ns})
        if len(entries) >= limit:
            complete = False
            break
    return {"base": raw or ".", "depth": depth, "entries": entries, "complete": complete, "truncated": not complete}
