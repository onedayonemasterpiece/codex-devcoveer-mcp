from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))

import direct_probe_disk_forensics as forensic
import direct_probe_host_runtime as host_runtime


def git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    })
    return subprocess.check_output(
        ["/usr/bin/git", "-C", str(repo), *args], env=env, text=True
    ).strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.check_call(["/usr/bin/git", "init", "-q", "-b", "main", str(path)])
    git(path, "config", "user.name", "T")
    git(path, "config", "user.email", "t@example.invalid")
    (path / "a.txt").write_text("alpha\n", encoding="utf-8")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "init")
    return path


def test_sessions_profile_is_metadata_only_and_age_bounded(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    old = sessions / "old.bin"
    fresh = sessions / "fresh.bin"
    old.write_bytes(b"x" * 10)
    fresh.write_bytes(b"y" * 20)
    old_epoch = time.time() - 20 * 86400
    os.utime(old, (old_epoch, old_epoch))

    data = forensic.sessions_profile(sessions)

    assert data["profile"] == "codex_sessions"
    assert data["files"]["count"] == 2
    assert data["files"]["bytes"] == 30
    by_days = {row["older_than_days"]: row for row in data["age_buckets"]}
    assert by_days[14]["files"] == 1 and by_days[14]["bytes"] == 10
    assert by_days[30]["files"] == 0
    assert "old.bin" not in str(data) and "fresh.bin" not in str(data)


def test_worktrees_profile_only_auto_eligible_for_clean_linked_worktree(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(forensic, "_proc_targets", lambda _root: (set(), set(), 0, 0, True))
    parent = init_repo(tmp_path / "parent")
    git(parent, "branch", "safe")
    git(parent, "branch", "dirty")

    root = tmp_path / "worktrees"
    root.mkdir()
    clean = root / "clean-linked"
    dirty = root / "dirty-linked"
    subprocess.check_call(["/usr/bin/git", "-C", str(parent), "worktree", "add", "-q", str(clean), "safe"])
    subprocess.check_call(["/usr/bin/git", "-C", str(parent), "worktree", "add", "-q", str(dirty), "dirty"])
    (dirty / "untracked.txt").write_text("u", encoding="utf-8")
    init_repo(root / "standalone")

    data = asyncio.run(forensic.worktrees_profile(root))

    assert data["profile"] == "codex_worktrees"
    assert data["leaves"]["total"]["count"] == 3
    assert data["leaves"]["eligible_unique"]["count"] == 1
    assert data["leaves"]["auto_delete_safe_unique"]["count"] == 1
    assert data["leaves"]["dirty"]["count"] == 1
    eligible = data["top_eligible"]
    assert eligible and eligible[0]["relative_path"] == "clean-linked"
    assert eligible[0]["layout"] == "linked_worktree"
    assert eligible[0]["reference_safe"] is True
    assert eligible[0]["eligible"] is True
    assert eligible[0]["auto_delete_safe"] is True
    standalone_rows = [
        row for row in data["top_review"]
        if row["relative_path"] == "standalone"
    ]
    assert standalone_rows and standalone_rows[0]["layout"] == "standalone_clone"
    assert standalone_rows[0]["reference_safe"] is False
    assert "untracked.txt" not in str(data)


def test_incomplete_process_scan_preserves_candidate_but_blocks_auto_delete(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(forensic, "_proc_targets", lambda _root: (set(), set(), 0, 0, False))
    parent = init_repo(tmp_path / "parent")
    git(parent, "branch", "safe")
    root = tmp_path / "worktrees"
    root.mkdir()
    clean = root / "clean-linked"
    subprocess.check_call(["/usr/bin/git", "-C", str(parent), "worktree", "add", "-q", str(clean), "safe"])

    data = asyncio.run(forensic.worktrees_profile(root))

    assert data["process_refs"]["complete"] is False
    assert data["leaves"]["eligible_unique"]["count"] == 1
    assert data["leaves"]["auto_delete_safe_unique"]["count"] == 0
    assert data["top_eligible"][0]["eligible"] is True
    assert data["top_eligible"][0]["auto_delete_safe"] is False
    assert data["complete"] is False


def test_resources_accepts_fixed_profile_binding(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "one").write_bytes(b"x")
    binding = {
        "roots": {
            "sessions": {
                "path": str(sessions),
                "profile": "codex_sessions",
            }
        }
    }

    data = asyncio.run(host_runtime._resources(
        binding, {"id": "r", "kind": "resources", "root": "sessions", "largest": 0}
    ))

    assert data["root"]["alias"] == "sessions"
    assert data["profile_data"]["profile"] == "codex_sessions"
    assert data["profile_data"]["files"]["count"] == 1


def test_resources_reports_missing_optional_profile_root(tmp_path: Path):
    missing = tmp_path / "missing-worktrees"
    binding = {
        "roots": {
            "worktrees": {
                "path": str(missing),
                "profile": "codex_worktrees",
            }
        }
    }

    data = asyncio.run(host_runtime._resources(
        binding, {"id": "r", "kind": "resources", "root": "worktrees", "largest": 0}
    ))

    assert data["root"] == {"alias": "worktrees", "exists": False}
    assert data["profile_data"]["profile"] == "codex_worktrees"
    assert data["profile_data"]["rootExists"] is False
    assert data["profile_data"]["complete"] is True


def test_unknown_resource_profile_fails_closed(tmp_path: Path):
    with pytest.raises(forensic.ProbeError) as exc:
        forensic.resolve_resource_target({
            "path": str(tmp_path),
            "profile": "arbitrary_shell",
        })
    assert exc.value.category == "config"
