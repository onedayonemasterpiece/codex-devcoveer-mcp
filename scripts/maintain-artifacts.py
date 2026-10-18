#!/usr/bin/env python3
"""Bounded, direct (no LLM agent) maintenance of completed development artifacts.

Never touches service state, Git worktrees, Docker, credentials, or runtime data.
Deletes only a named, old, unreferenced top-level test run in a trusted artifact root.
Child-only scratch_cleanup additionally supports pytest-tmp with internal current
symlinks unlinked, never followed. Existing 72-hour retention/operator gates apply.
Dry-run is not authorization: apply requires the exact manifest and logical bytes.
Requires a quiescent trusted run: flock only coordinates this operator, not pytest.
POSIX stat/unlink is not atomic against a hostile same-UID writer. Detected races
stop cleanup with a partial-deletion receipt; inspect again, never blindly retry.
"""
from __future__ import annotations

import argparse
import hashlib
import fcntl
import stat
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


def _trusted_host_transport(proc: Path, uid: int) -> bool:
    """The user's privileged session infrastructure is not an artifact worker.

    Linux ptrace restrictions hide /proc/PID/cwd and fd for systemd --user,
    sd-pam, and sshd-session even when they share our UID. Their child shells
    and actual workers remain fully subject to open-path checks.
    """
    try:
        name = (proc / "comm").read_text(encoding="utf-8").strip()
        cgroup = (proc / "cgroup").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    prefix = rf"0::/user\.slice/user-{uid}\.slice/"
    if name in {"systemd", "(sd-pam)"}:
        return re.fullmatch(prefix + rf"user@{uid}\.service/init\.scope", cgroup) is not None
    if name == "sshd-session":
        return re.fullmatch(prefix + r"session-[0-9]+\.scope", cgroup) is not None
    return False


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
        if _trusted_host_transport(proc, uid):
            continue
        for link in (proc / "cwd", proc / "root"):
            try:
                location = os.readlink(link)
                if location == str(path) or location.startswith(prefix):
                    return True
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise UnsafeArtifact(f"process directory scan incomplete: pid={entry.name} {link.name} errno={exc.errno}") from exc
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


def _check_tree(candidate: Path, *, latest_out: list[float] | None = None) -> int:
    entries = 0
    root_info = candidate.stat()
    filesystem = root_info.st_dev
    newest = root_info.st_mtime

    def on_walk_error(error: OSError) -> None:
        raise UnsafeArtifact("cannot inspect all artifact subdirectories") from error

    for base, directories, files in os.walk(
        candidate, followlinks=False, onerror=on_walk_error
    ):
        entries += len(directories) + len(files)
        if entries > MAX_ENTRIES:
            raise UnsafeArtifact("run too large to inspect safely")
        for name in directories + files:
            child = Path(base) / name
            if child.is_symlink():
                raise UnsafeArtifact("run contains symlink")
            info = child.stat()
            if info.st_dev != filesystem:
                raise UnsafeArtifact("run crosses filesystem")
            newest = max(newest, info.st_mtime)
            name_lower = name.lower()
            if (name_lower in {".git", ".ssh", ".env", "credentials", "secrets"}
                    or name_lower.startswith(".env.")
                    or name_lower.endswith(".env")):
                raise UnsafeArtifact("run contains protected data")
    if latest_out is not None:
        latest_out.append(newest)
    return entries


def inspect(root: Path, repo_root: Path, project: str, name: str,
            *, now: float | None = None) -> dict:
    candidate = _run_dir(root, project, name)
    timestamp = time() if now is None else now
    age = timestamp - candidate.stat().st_mtime
    reasons = []
    if age < MIN_AGE_SECONDS:
        reasons.append("recent_run")
    if _is_referenced(repo_root, project, name):
        reasons.append("referenced_by_source_or_tests")
    try:
        if _in_use(candidate):
            reasons.append("open_by_process")
    except UnsafeArtifact as exc:
        # Inspection can still report the run; deletion remains fail-closed.
        reasons.append(f"process_check_incomplete: {exc}")
    latest_update: list[float] = []
    entries = _check_tree(candidate, latest_out=latest_update)
    latest_age = timestamp - latest_update[0]
    if latest_age < MIN_AGE_SECONDS:
        reasons.append("recent_nested_output")
    return {"project": project, "run": name, "size_kib": _size_kib(candidate),
            "entries": entries, "age_hours": round(age / 3600, 1),
            "newest_entry_age_hours": round(latest_age / 3600, 1),
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


# Child-only cleanup is intentionally NOT an override of retention/operator gates.
SCRATCH_CHILD = "pytest-tmp"
MAX_SCRATCH_BYTES = 4 * 1024**3


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_directory(path):
    """Anchor every component without resolving or following any symlink."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.absolute().parts[1:]:
            newer = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd)
            os.close(fd)
            fd = newer
        return fd
    except BaseException:
        os.close(fd)
        raise


def _mount_id(fd):
    # st_dev alone misses same-filesystem bind mounts. Linux operator only.
    try:
        for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
            if line.startswith("mnt_id:"):
                return int(line.split()[1])
    except (OSError, ValueError) as exc:
        raise UnsafeArtifact("mount identity unavailable") from exc
    raise UnsafeArtifact("mount identity unavailable")


def _scratch_snapshot(fd, path):
    records = {}
    size = 0
    root_info = os.fstat(fd)
    root_mount = _mount_id(fd)

    def walk(directory, relative, depth):
        nonlocal size
        if depth > 64:
            raise UnsafeArtifact("scratch depth limit exceeded")
        info = os.fstat(directory)
        records[relative] = _identity(info)
        for name in sorted(os.listdir(directory)):
            rel = relative + "/" + name if relative else name
            if len(records) >= MAX_ENTRIES:
                raise UnsafeArtifact("scratch entry limit exceeded")
            st = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if st.st_dev != root_info.st_dev or st.st_uid != os.getuid():
                raise UnsafeArtifact("scratch ownership/filesystem mismatch")
            lower = name.lower()
            if (lower in {".git", ".ssh", ".env", "credentials", "secrets"}
                    or lower.startswith(".env.") or lower.endswith(".env")):
                raise UnsafeArtifact("scratch contains protected data")
            records[rel] = _identity(st)
            if stat.S_ISDIR(st.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
                try:
                    if _mount_id(child) != root_mount:
                        raise UnsafeArtifact("scratch crosses mount boundary")
                    if _identity(os.fstat(child)) != _identity(st):
                        raise UnsafeArtifact("scratch directory changed")
                    walk(child, rel, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISLNK(st.st_mode):
                target = os.readlink(name, dir_fd=directory)
                absolute = os.path.normpath(os.path.join(str(path / relative), target))
                if (not name.endswith("current") or
                        os.path.commonpath([str(path), absolute]) != str(path)):
                    raise UnsafeArtifact("only internal pytest current links are allowed")
                # Unlink the link itself. Never inspect/open its target.
            elif stat.S_ISREG(st.st_mode):
                if st.st_nlink != 1:
                    raise UnsafeArtifact("scratch contains hardlinked file")
                size += st.st_size
                if size > MAX_SCRATCH_BYTES:
                    raise UnsafeArtifact("scratch byte limit exceeded")
            else:
                raise UnsafeArtifact("scratch contains special file")
    if root_info.st_uid != os.getuid():
        raise UnsafeArtifact("scratch ownership mismatch")
    walk(fd, "", 0)
    digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    return records, size, digest


def scratch_cleanup(root, repo_root, project, name, child, *, apply=False,
                    expected_manifest=None, expected_bytes=None, now=None):
    """Exact pytest child, dry-run default. No caller-supplied arbitrary path."""
    if child != SCRATCH_CHILD:
        raise UnsafeArtifact("only child pytest-tmp is allowed")
    run = _run_dir(root, project, name)
    candidate = run / child
    run_fd = _open_directory(run)
    fd = None
    try:
        # Serialize cooperating cleanup calls; not a lock on arbitrary pytest writers.
        try:
            fcntl.flock(run_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UnsafeArtifact("another scratch cleanup holds the run lock") from exc
        fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=run_fd)
        if _mount_id(fd) != _mount_id(run_fd):
            raise UnsafeArtifact("scratch root crosses mount boundary")
        records, size, digest = _scratch_snapshot(fd, candidate)
        os.close(fd)
        fd = None
        blockers = []
        timestamp = time() if now is None else now
        if timestamp - max(r[6] / 1e9 for r in records.values()) < MIN_AGE_SECONDS:
            blockers.append("recent_nested_output")
        if _is_referenced(repo_root, project, name):
            blockers.append("referenced_by_source_or_tests")
        try:
            if _in_use(candidate):
                blockers.append("open_by_process")
        except UnsafeArtifact as exc:
            blockers.append(f"process_check_incomplete: {exc}")
        plan = {"project": project, "run": name, "child": child,
                "bytes": size, "entries": len(records), "manifest_sha256": digest,
                "safe_to_delete": not blockers, "blockers": blockers,
                "requires_quiescent_run": True, "concurrent_writer_safe": False}
        if not apply:
            return {**plan, "status": "dry_run"}
        if blockers:
            raise UnsafeArtifact("scratch cleanup blocked: " + ",".join(blockers))
        if (not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool)
                or expected_bytes != size or expected_manifest != digest):
            raise UnsafeArtifact("scratch expected manifest/bytes mismatch")
        if _in_use(candidate):
            raise UnsafeArtifact("scratch became active")
        fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=run_fd)
        if _mount_id(fd) != _mount_id(run_fd):
            raise UnsafeArtifact("scratch root crosses mount boundary")
        # Re-enumerate before the first unlink, comparing inode and metadata too.
        if _scratch_snapshot(fd, candidate) != (records, size, digest):
            raise UnsafeArtifact("scratch changed since preview")

        children = {}
        for rel in records:
            if rel:
                parent, _, leaf = rel.rpartition("/")
                children.setdefault(parent, []).append(leaf)
        for names in children.values():
            names.sort()
        removed = {"files_or_links": 0, "directories": 0}

        def check_anchor(directory, relative):
            live = _open_directory(candidate / relative)
            try:
                a, b = os.fstat(live), os.fstat(directory)
                if (_mount_id(live) != _mount_id(directory) or
                        (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino)):
                    raise UnsafeArtifact("scratch path anchor changed")
            finally:
                os.close(live)

        def erase(directory, relative):
            check_anchor(directory, relative)
            names = sorted(os.listdir(directory))
            expected_names = children.get(relative, [])
            if names != expected_names:
                raise UnsafeArtifact("scratch entries changed during cleanup")
            for leaf in names:
                check_anchor(directory, relative)
                rel = relative + "/" + leaf if relative else leaf
                st = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                if _identity(st) != records[rel]:
                    raise UnsafeArtifact("scratch inode changed during cleanup")
                if stat.S_ISDIR(st.st_mode):
                    nested = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=directory)
                    try:
                        if _identity(os.fstat(nested)) != records[rel]:
                            raise UnsafeArtifact("scratch directory replaced")
                        erase(nested, rel)
                        current = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) != records[rel][:2]:
                            raise UnsafeArtifact("scratch directory moved")
                        check_anchor(directory, relative)
                        os.rmdir(leaf, dir_fd=directory)
                        removed["directories"] += 1
                    finally:
                        os.close(nested)
                else:
                    check_anchor(directory, relative)
                    os.unlink(leaf, dir_fd=directory)
                    removed["files_or_links"] += 1
        # Ensure the selected child is still the anchored directory.
        if _identity(os.stat(child, dir_fd=run_fd, follow_symlinks=False)) != records[""]:
            raise UnsafeArtifact("scratch root replaced")
        free_before = shutil.disk_usage(root).free
        try:
            erase(fd, "")
            current = os.stat(child, dir_fd=run_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != records[""][:2]:
                raise UnsafeArtifact("scratch root moved")
            os.rmdir(child, dir_fd=run_fd)
            removed["directories"] += 1
        except (UnsafeArtifact, OSError) as exc:
            return {**plan, "status": "blocked", "reason": str(exc),
                    "partial_cleanup": any(removed.values()), "removed": removed,
                    "free_before_bytes": free_before,
                    "free_after_bytes": shutil.disk_usage(root).free}
        return {**plan, "status": "removed", "removed": removed,
                "free_before_bytes": free_before,
                "free_after_bytes": shutil.disk_usage(root).free}
    finally:
        if fd is not None:
            os.close(fd)
        os.close(run_fd)


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--project", choices=sorted(ALLOWED_PROJECTS), required=True)
    cli.add_argument("--list", action="store_true")
    cli.add_argument("--run")
    cli.add_argument("--delete", action="store_true")
    cli.add_argument("--expected-kib", type=int)
    cli.add_argument("--scratch-child", choices=[SCRATCH_CHILD])
    cli.add_argument("--expected-manifest")
    cli.add_argument("--expected-bytes", type=int)
    args = cli.parse_args()
    if args.scratch_child:
        if not args.run or args.list or args.expected_kib is not None:
            cli.error("scratch cleanup requires run and no whole-run arguments")
        if args.delete and (args.expected_manifest is None or args.expected_bytes is None):
            cli.error("scratch apply requires expected manifest and bytes")
    elif args.expected_manifest is not None or args.expected_bytes is not None:
        cli.error("scratch expectations require scratch-child")
    if args.delete and not args.scratch_child and (not args.run or args.expected_kib is None):
        cli.error("--delete requires --run and --expected-kib")
    if args.list and (args.run or args.delete):
        cli.error("use --list or --run, not both")
    try:
        if args.scratch_child:
            result = scratch_cleanup(ARTIFACT_ROOT, REPO_ROOT, args.project, args.run,
                                     args.scratch_child, apply=args.delete,
                                     expected_manifest=args.expected_manifest,
                                     expected_bytes=args.expected_bytes)
        elif args.list:
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
        return 2 if result.get("status") == "blocked" else 0
    except (UnsafeArtifact, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

