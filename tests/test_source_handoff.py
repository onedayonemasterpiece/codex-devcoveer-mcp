from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from runtime.source_handoff import (
    SourceHandoffError,
    cleanup_source_handoff,
    gc_source_handoffs,
    list_source_handoffs,
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



def test_lifecycle_inventory_keeps_dirty_subsumed_as_review_only(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    root = tmp_path / "handoffs"
    patch = build_patch(repo, base)
    result = stage(repo, base, patch, root)
    workspace = resolve_source_handoff(result["handoffId"], root=root)

    before = list_source_handoffs(repo, root=root)
    row = next(item for item in before["handoffs"] if item["handoffId"] == result["handoffId"])
    assert row["cleanupSafe"] is False
    assert row["cleanupReason"] == "unique_changes"

    git(repo, "apply", "--index", "-", input_bytes=patch)
    git(repo, "commit", "-m", "accept patch")
    after = list_source_handoffs(repo, root=root)
    row = next(item for item in after["handoffs"] if item["handoffId"] == result["handoffId"])
    assert row["changesSubsumed"] is True
    assert row["cleanupSafe"] is False
    assert row["cleanupReason"] == "changes_subsumed_review"

    swept = gc_source_handoffs(repo, root=root, older_than_seconds=0, max_remove=8)
    assert result["handoffId"] not in swept["removed"]
    assert workspace.exists()


def test_lifecycle_clean_referenced_head_is_safe(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    root = tmp_path / "handoffs"
    patch = build_patch(repo, base)
    result = stage(repo, base, patch, root)
    workspace = resolve_source_handoff(result["handoffId"], root=root)
    git(workspace, "commit", "-m", "handoff commit")
    head = git(workspace, "rev-parse", "HEAD").decode().strip()
    git(repo, "branch", "saved-handoff", head)

    inventory = list_source_handoffs(repo, root=root)
    row = next(item for item in inventory["handoffs"] if item["handoffId"] == result["handoffId"])
    assert row["clean"] is True
    assert row["headReferenced"] is True
    assert row["cleanupSafe"] is True
    assert row["cleanupReason"] == "clean_head_referenced"


def test_cleanup_survives_original_staging_worktree_removal(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "--detach", str(linked), base)
    root = tmp_path / "handoffs"
    patch = build_patch(repo, base)
    digest = hashlib.sha256(patch).hexdigest()
    result = stage_source_handoff(
        linked, "proj", base, patch.decode("utf-8"), digest, root=root
    )
    workspace = resolve_source_handoff(result["handoffId"], root=root)
    git(repo, "worktree", "remove", "--force", str(linked))

    removed = cleanup_source_handoff(result["handoffId"], root=root)
    assert removed["removed"] is True
    assert not workspace.exists()


def test_stage_opportunistically_sweeps_only_safe_handoffs(tmp_path: Path, monkeypatch) -> None:
    import runtime.source_handoff as sh

    repo, base = make_repo(tmp_path)
    root = tmp_path / "handoffs"
    first_patch = build_patch(repo, base)
    first = stage(repo, base, first_patch, root)
    first_workspace = resolve_source_handoff(first["handoffId"], root=root)
    git(first_workspace, "commit", "-m", "accept first")
    first_head = git(first_workspace, "rev-parse", "HEAD").decode().strip()
    git(repo, "branch", "saved-first", first_head)

    monkeypatch.setattr(sh, "AUTO_GC_TRIGGER_COUNT", 1)
    monkeypatch.setattr(sh, "AUTO_GC_STALE_SECONDS", 0)

    current = base
    (repo / "change.txt").write_text("second\n", encoding="utf-8")
    second_patch = git(repo, "diff", "--binary", current)
    git(repo, "checkout", "--", "change.txt")
    second = stage(repo, current, second_patch, root)
    assert first["handoffId"] in second["autoCleanup"]["removed"]
    assert (root / second["handoffId"]).is_dir()



def test_requirements_contract_cannot_be_staged_through_source_handoff(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    contract = repo / ".devcoveer" / "requirements.json"
    contract.parent.mkdir()
    contract.write_text('{"schema_version":1,"project":"demo","requirements":[{"id":"CORE-001","statement":"preserve"}]}\n', encoding="utf-8")
    git(repo, "add", ".devcoveer/requirements.json")
    patch = git(repo, "diff", "--cached", "--binary", base)
    git(repo, "reset", "--hard", base)

    with pytest.raises(SourceHandoffError, match="requirements contract changes are not accepted"):
        stage(repo, base, patch, tmp_path / "handoffs")
