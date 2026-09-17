from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from runtime.source_handoff import (
    SourceHandoffError,
    cleanup_source_handoff,
    resolve_source_handoff,
    stage_source_handoff,
)


def git(cwd: Path, *args: str, input_bytes: bytes | None = None, check: bool = True) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
    return completed.stdout


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "tests@example.invalid")
    git(repo, "config", "user.name", "Source Handoff Tests")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    (repo / "change.txt").write_text("old\n", encoding="utf-8")
    (repo / "delete.txt").write_text("remove me\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    return repo, base


def build_patch(repo: Path, base: str) -> bytes:
    (repo / "change.txt").write_text("new\n", encoding="utf-8")
    (repo / "added.txt").write_text("added\n", encoding="utf-8")
    (repo / "delete.txt").unlink()
    git(repo, "add", "-A")
    patch = git(repo, "diff", "--cached", "--binary", base)
    git(repo, "reset", "--hard", base)
    return patch


def stage(repo: Path, base: str, patch: bytes, root: Path):
    return stage_source_handoff(
        repo,
        "example-project",
        base,
        patch,
        hashlib.sha256(patch).hexdigest(),
        root=root,
    )


def test_stage_materializes_patch_without_touching_canonical_or_persisting_patch(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    patch = build_patch(repo, base)
    root = tmp_path / "handoffs"

    result = stage(repo, base, patch, root)
    workspace = resolve_source_handoff(result["handoffId"], root=root)

    assert (repo / "change.txt").read_text() == "old\n"
    assert not (repo / "added.txt").exists()
    assert (repo / "delete.txt").exists()

    assert (workspace / "change.txt").read_text() == "new\n"
    assert (workspace / "added.txt").read_text() == "added\n"
    assert not (workspace / "delete.txt").exists()
    assert sorted(result["changedFiles"]) == ["added.txt", "change.txt", "delete.txt"]
    quiet = subprocess.run(["git", "-C", str(workspace), "diff", "--cached", "--quiet", base])
    assert quiet.returncode == 1

    handoff_dir = root / result["handoffId"]
    assert not (handoff_dir / ".venv").exists()
    assert sorted(p.name for p in handoff_dir.iterdir()) == ["meta.json", "worktree"]
    assert not any(p.suffix in {".patch", ".zip", ".tar", ".gz"} for p in handoff_dir.rglob("*"))

    meta = json.loads((handoff_dir / "meta.json").read_text())
    assert meta["baseSha"] == base
    assert meta["patchSha256"] == hashlib.sha256(patch).hexdigest()


def test_checksum_mismatch_fails_without_leak(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    patch = build_patch(repo, base)
    root = tmp_path / "handoffs"
    with pytest.raises(SourceHandoffError, match="sha256 mismatch"):
        stage_source_handoff(repo, "example-project", base, patch, "0" * 64, root=root)
    assert not root.exists() or list(root.iterdir()) == []
    assert len(git(repo, "worktree", "list", "--porcelain").split(b"worktree ")) == 2


def test_unknown_base_fails(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    patch = build_patch(repo, base)
    root = tmp_path / "handoffs"
    with pytest.raises(SourceHandoffError, match="base commit"):
        stage_source_handoff(
            repo,
            "example-project",
            "f" * 40,
            patch,
            hashlib.sha256(patch).hexdigest(),
            root=root,
        )


def test_malformed_patch_is_cleaned_up(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    root = tmp_path / "handoffs"
    patch = b"not a git patch\n"
    with pytest.raises(SourceHandoffError):
        stage(repo, base, patch, root)
    assert list(root.iterdir()) == []
    listing = git(repo, "worktree", "list", "--porcelain").decode()
    assert str(root) not in listing


def test_same_input_is_idempotent_and_keeps_existing_workspace(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    patch = build_patch(repo, base)
    root = tmp_path / "handoffs"
    first = stage(repo, base, patch, root)
    workspace = resolve_source_handoff(first["handoffId"], root=root)
    (workspace / "opencode-note.txt").write_text("keep this\n", encoding="utf-8")

    second = stage(repo, base, patch, root)
    assert second["handoffId"] == first["handoffId"]
    assert second["reused"] is True
    assert (workspace / "opencode-note.txt").read_text() == "keep this\n"


def test_cleanup_removes_worktree_and_metadata(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    patch = build_patch(repo, base)
    root = tmp_path / "handoffs"
    result = stage(repo, base, patch, root)
    workspace = resolve_source_handoff(result["handoffId"], root=root)

    removed = cleanup_source_handoff(result["handoffId"], root=root)
    assert removed == {"handoffId": result["handoffId"], "removed": True}
    assert not workspace.exists()
    assert not (root / result["handoffId"]).exists()
    assert str(workspace) not in git(repo, "worktree", "list", "--porcelain").decode()

    again = cleanup_source_handoff(result["handoffId"], root=root)
    assert again["removed"] is False


def test_invalid_handoff_id_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "handoffs"
    with pytest.raises(SourceHandoffError, match="invalid source handoff id"):
        resolve_source_handoff("../escape", root=root)
    with pytest.raises(SourceHandoffError, match="invalid source handoff id"):
        cleanup_source_handoff("/tmp/anything", root=root)
