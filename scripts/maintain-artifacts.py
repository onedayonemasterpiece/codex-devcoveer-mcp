#!/usr/bin/env python3
"""Bounded, direct (no LLM agent) maintenance of completed development artifacts.

Never touches service state, Git worktrees, Docker, credentials, or runtime data.
Deletes only a named, old, unreferenced top-level test run in a trusted artifact root.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from time import time

ARTIFACT_ROOT = Path("/home/dev/artifacts")
REPO_ROOT = Path("/home/dev/projects")
ALLOWED_PROJECTS = frozenset({
    "street-story", "events-bot", "events-bot-new", "regional-knowledge-base",
    "wonderful-lections", "vibepublish", "codex-devcoveer-mcp",
})
RUN_NAME = re.compile(r"^[0-9]{8}(?:T[0-9]{6}Z)?[-_][a-zA-Z0-9._-]{1,120}$")
MIN_AGE_SECONDS = 72 * 3600
MAX_ENTRIES = 200000


class UnsafeArtifact(ValueError):
    pass


def _project_dir(root: Path, project: str) -> Path:
    if project not in ALLOWED_PROJECTS:
        raise UnsafeArtifact("project is not allowlisted")
    if root.is_symlink() or not root.is_dir():
        raise UnsafeArtifact("artifact root missing or a symlink")
    directory = root / project
    if directory.is_symlink() or not directory.is_dir():
        raise UnsafeArtifact("artifact project missing or a symlink")
    return directory


def _run_dir(root: Path, project: str, name: str) -> Path:
    directory = _project_dir(root, project)
    if RUN_NAME.fullmatch(name) is None:
        raise UnsafeArtifact("run must be one exact dated top-level directory name")
    candidate = directory / name
    if candidate.is_symlink() or not candidate.is_dir():
        raise UnsafeArtifact("run missing, not a directory, or a symlink")
    if candidate.resolve() != candidate.absolute():
        raise UnsafeArtifact("run resolves outside its exact path")
    return candidate


def _size_kib(path: Path) -> int:
    output = subprocess.run(
        ["/usr/bin/du", "-sk", "--", str(path)],
        text=True, capture_output=True, check=True, timeout=35,
    ).stdout
    return int(output.split()[0])


def _is_referenced(repo_root: Path, project: str, name: str) -> bool:
    source = repo_root / project
    if not source.is_dir() or source.is_symlink():
        raise UnsafeArtifact("source repo unavailable: cannot verify saved evidence references")
    targets = [p for p in (source / "docs", source / "backend" / "tests", source / "tests")
               if p.exists() and not p.is_symlink()]
    if not targets:
        raise UnsafeArtifact("no reference directories available")
    try:
        result = subprocess.run(
            ["/usr/bin/rg", "--quiet", "--fixed-strings", "--", name,
             *map(str, targets)],
            timeout=20, check=False, capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnsafeArtifact("reference scan unavailable") from exc
    if result.returncode not in (0, 1):
        raise UnsafeArtifact("reference scan failed")
    return result.returncode == 0


def _in_use(path: Path) -> bool:
    prefix = str(path) + "/"
    uid = os.getuid()
    try:
        processes = list(os.scandir("/proc"))
    except OSError as exc:
        raise UnsafeArtifact("cannot inspect processes") from exc
    for entry in processes:
        if not entry.name.isdigit():
            continue
        proc = Path(entry.path)
        try:
            if proc.stat().st_uid != uid:
                continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UnsafeArtifact("process ownership scan incomplete") from exc
        for link in (proc / "cwd", proc / "root"):
            try:
                location = os.readlink(link)
                if location == str(path) or location.startswith(prefix):
                    return True
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise UnsafeArtifact(f"process directory scan incomplete: {link.name} errno={exc.errno}") from exc
        try:
            with os.scandir(proc / "fd") as fds:
                for fd in fds:
                    try:
                        location = os.readlink(fd.path)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise UnsafeArtifact("file descriptor scan incomplete") from exc
                    if location == str(path) or location.startswith(prefix):
                        return True
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UnsafeArtifact("file descriptor scan unavailable") from exc
    return False


def _check_tree(candidate: Path) -> int:
    entries = 0
    filesystem = candidate.stat().st_dev
    for base, directories, files in os.walk(candidate, followlinks=False):
        entries += len(directories) + len(files)
        if entries > MAX_ENTRIES:
            raise UnsafeArtifact("run too large to inspect safely")
        for name in directories + files:
            child = Path(base) / name
            if child.is_symlink() or child.stat().st_dev != filesystem:
                raise UnsafeArtifact("run contains symlink or other filesystem")
            if name in (".git", ".env", "credentials", "secrets", ".ssh"):
                raise UnsafeArtifact("run contains protected data")
    return entries


def inspect(root: Path, repo_root: Path, project: str, name: str,
            *, now: float | None = None) -> dict:
    candidate = _run_dir(root, project, name)
    age = (time() if now is None else now) - candidate.stat().st_mtime
    reasons = []
    if age < MIN_AGE_SECONDS:
        reasons.append("recent_run")
    if _is_referenced(repo_root, project, name):
        reasons.append("referenced_by_source_or_tests")
    if _in_use(candidate):
        reasons.append("open_by_process")
    entries = _check_tree(candidate)
    return {"project": project, "run": name, "size_kib": _size_kib(candidate),
            "entries": entries, "age_hours": round(age / 3600, 1),
            "safe_to_delete": not reasons, "blockers": reasons}


def remove(root: Path, repo_root: Path, project: str, name: str,
           expected_kib: int, *, now: float | None = None) -> dict:
    if expected_kib <= 0:
        raise UnsafeArtifact("positive expected size is required")
    plan = inspect(root, repo_root, project, name, now=now)
    if not plan["safe_to_delete"]:
        raise UnsafeArtifact("deletion blocked: " + ",".join(plan["blockers"]))
    if plan["size_kib"] != expected_kib:
        raise UnsafeArtifact("size changed since preview: refusing deletion")
    candidate = _run_dir(root, project, name)
    free_before = shutil.disk_usage(root).free
    shutil.rmtree(candidate)
    return {**plan, "status": "removed", "free_before_bytes": free_before,
            "free_after_bytes": shutil.disk_usage(root).free}


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--project", choices=sorted(ALLOWED_PROJECTS), required=True)
    cli.add_argument("--list", action="store_true")
    cli.add_argument("--run")
    cli.add_argument("--delete", action="store_true")
    cli.add_argument("--expected-kib", type=int)
    args = cli.parse_args()
    if args.delete and (not args.run or args.expected_kib is None):
        cli.error("--delete requires --run and --expected-kib")
    if args.list and (args.run or args.delete):
        cli.error("use --list or --run, not both")
    try:
        if args.list:
            directory = _project_dir(ARTIFACT_ROOT, args.project)
            names = sorted((p.name for p in directory.iterdir()
                            if p.is_dir() and not p.is_symlink()
                            and RUN_NAME.fullmatch(p.name)), reverse=True)
            result = {"project": args.project, "runs": names[:120],
                      "more": len(names) > 120}
        elif args.run:
            result = (remove(ARTIFACT_ROOT, REPO_ROOT, args.project, args.run,
                             args.expected_kib) if args.delete
                      else inspect(ARTIFACT_ROOT, REPO_ROOT, args.project, args.run))
        else:
            cli.error("specify --list or --run")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (UnsafeArtifact, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
