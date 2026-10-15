"""Managed parallel native worktrees: real local Git integration/retirement tests."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
import direct_actions as actions
import native_managed_worktrees as manager

REF_1 = "01a13f72-3423-7170-95c5-bee77e6a870d"
REF_2 = "01a13f73-3423-7170-95c5-bee77e6a870d"


def git(root: Path, *args: str, check: bool = True) -> str:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    })
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch):
    worktrees = tmp_path / "managed-worktrees"
    metadata = tmp_path / "managed-records"
    monkeypatch.setattr(manager, "WORKTREE_ROOT", worktrees)
    monkeypatch.setattr(manager, "RECORD_ROOT", metadata)
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "a.txt").write_text("initial\n")
    git(root, "add", "a.txt")
    git(root, "commit", "-m", "Initial")
    return root


def perform(root: Path, mode: str, ref: str = REF_1, **opts):
    return manager.managed_worktree(root, root.name,
                                    {"kind": "managed_worktree", "mode": mode,
                                     "task_ref": ref, **opts})


def commit_change(worktree: Path, file: str, content: str) -> str:
    (worktree / file).write_text(content)
    git(worktree, "add", file)
    git(worktree, "commit", "-m", f"Test {file}")
    return git(worktree, "rev-parse", "HEAD")


def test_create_merge_and_cleanup_same_owned_checkout(repo):
    created = perform(repo, "create")
    assert created["status"] == "ok"
    worktree = Path(created["worktree"])
    assert worktree.is_dir()
    assert created["target_branch"] == "main"
    assert git(repo, "rev-parse", "HEAD") == created["base_sha"]
    assert perform(repo, "create")["reused"] is True
    head = commit_change(worktree, "added.txt", "new functionality\n")
    assert perform(repo, "status")["head"] == head
    done = perform(repo, "finish", expected_head=head)
    assert done["status"] == "ok"
    assert done["merged"] and done["cleaned"]
    assert (repo / "added.txt").read_text() == "new functionality\n"
    assert not worktree.exists()
    assert git(repo, "branch", "--list", created["branch"]) == ""
    assert perform(repo, "finish", expected_head=head)["already_finished"]


def test_uncommitted_and_untracked_work_is_never_discarded(repo):
    created = perform(repo, "create")
    worktree = Path(created["worktree"])
    head = git(worktree, "rev-parse", "HEAD")
    (worktree / "unfinished.txt").write_text("keep me")
    result = perform(repo, "finish", expected_head=head)
    assert result["status"] == "blocked"
    assert result["blocker"] == "uncommitted_or_untracked_work"
    assert worktree.joinpath("unfinished.txt").read_text() == "keep me"


def test_dirty_canonical_target_is_never_overwritten(repo):
    created = perform(repo, "create")
    worktree = Path(created["worktree"])
    head = commit_change(worktree, "feature.txt", "parallel\n")
    (repo / "a.txt").write_text("current worker edits\n")
    result = perform(repo, "finish", expected_head=head)
    assert result["blocker"] == "target_checkout_dirty"
    assert worktree.exists()
    assert (repo / "a.txt").read_text() == "current worker edits\n"
    assert not (repo / "feature.txt").exists()


def test_diverged_nonconflicting_branches_merge_both_then_cleanup(repo):
    first = perform(repo, "create", REF_1)
    second = perform(repo, "create", REF_2)
    first_path, second_path = Path(first["worktree"]), Path(second["worktree"])
    head1 = commit_change(first_path, "first.txt", "first\n")
    head2 = commit_change(second_path, "second.txt", "second\n")
    assert perform(repo, "finish", REF_1, expected_head=head1)["cleaned"]
    second_done = perform(repo, "finish", REF_2, expected_head=head2)
    assert second_done["cleaned"] and second_done["merged"]
    assert (repo / "first.txt").is_file()
    assert (repo / "second.txt").is_file()
    assert not first_path.exists() and not second_path.exists()


def test_conflicting_merge_aborts_without_discarding_branch(repo):
    created = perform(repo, "create")
    worktree = Path(created["worktree"])
    head = commit_change(worktree, "a.txt", "feature version\n")
    (repo / "a.txt").write_text("other author's version\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-m", "Parallel canonical update")
    old_target = git(repo, "rev-parse", "HEAD")
    result = perform(repo, "finish", expected_head=head)
    assert result["blocker"] == "merge_conflict_or_failure"
    assert worktree.exists()
    assert git(repo, "rev-parse", "HEAD") == old_target
    assert (repo / "a.txt").read_text() == "other author's version\n"
    assert git(repo, "status", "--porcelain") == ""
    assert git(worktree, "rev-parse", "HEAD") == head


def test_busy_worktree_retained_after_integration_until_safe(repo, monkeypatch):
    created = perform(repo, "create")
    worktree = Path(created["worktree"])
    head = commit_change(worktree, "busy.txt", "owned\n")
    monkeypatch.setattr(manager, "_in_use", lambda _path: True)
    result = perform(repo, "finish", expected_head=head)
    assert result["blocker"] == "worktree_in_use"
    assert result["integrated"] is True
    assert worktree.exists()
    assert (repo / "busy.txt").exists()
    monkeypatch.setattr(manager, "_in_use", lambda _path: False)
    again = perform(repo, "finish", expected_head=head)
    assert again["cleaned"]
    assert not worktree.exists()


def test_head_drift_and_target_switch_protected(repo):
    created = perform(repo, "create")
    worktree = Path(created["worktree"])
    head = commit_change(worktree, "drift.txt", "x\n")
    assert perform(repo, "finish", expected_head=created["base_sha"])["blocker"] == "expected_head_mismatch"
    git(repo, "checkout", "-b", "unrelated")
    assert perform(repo, "finish", expected_head=head)["blocker"] == "target_branch_changed"
    assert worktree.exists()


def test_unowned_worktree_remains_untouched(repo):
    outside = repo.parent / "another-existing-worktree"
    git(repo, "worktree", "add", "-b", "another", str(outside), "HEAD")
    created = perform(repo, "create")
    head = commit_change(Path(created["worktree"]), "done.txt", "ready\n")
    assert perform(repo, "finish", expected_head=head)["cleaned"]
    assert outside.is_dir()
    assert git(outside, "branch", "--show-current") == "another"


def test_task_id_validation_and_readonly_policy(repo):
    for ref in ("../other", "bad", "x" * 36):
        with pytest.raises(actions.ProbeError):
            perform(repo, "create", ref)
    assert manager.policy_prompt("read", REF_1) == ""
    instructions = manager.policy_prompt("write", REF_1)
    assert REF_1 in instructions
    assert "mode=finish" in instructions
    assert "No force deletion" in instructions


def test_direct_project_action_schema_includes_typed_managed_operation():
    schema = Draft202012Validator(actions.PROJECT_ACTION_INPUT_SCHEMA)
    good = {"project": "street-story",
            "action": {"kind": "managed_worktree", "mode": "create", "task_ref": REF_1}}
    assert not list(schema.iter_errors(good))
    assert list(schema.iter_errors({"project": "street-story",
        "action": {**good["action"], "path": "/home/dev/projects/other"}}))
    assert list(schema.iter_errors({"project": "street-story",
        "action": {**good["action"], "task_ref": "../etc"}}))


def test_canonical_builder_emits_policy_and_runtime(tmp_path: Path):
    builder = ROOT / "scripts" / "build-clean-host-runtime.py"
    target = tmp_path / "bundle"
    result = subprocess.run([sys.executable, str(builder), str(target)],
                            capture_output=True, text=True, timeout=100)
    assert result.returncode == 0, result.stderr
    bridge = (target / "codex-chatgpt-bridge.py").read_text()
    assert (target / "native_managed_worktrees.py").is_file()
    assert "_managed_worktree_policy_prompt(access, thread_id)" in bridge
    assert "_managed_worktree_policy_prompt(resume_metadata.get" in bridge
    assert "do not create a clone, checkout copy" not in bridge
    assert "task-managed worktree" in bridge
    assert (target / "direct_actions.py").read_text().count("managed_worktree") >= 2


def test_ignored_untracked_database_blocks_removal(repo):
    (repo / ".gitignore").write_text("*.sqlite3\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "Preserve ignored databases")
    created = perform(repo, "create")
    worktree = Path(created["worktree"])
    head = commit_change(worktree, "feature.txt", "ready\n")
    (worktree / "important.sqlite3").write_bytes(b"not reproducible")
    result = perform(repo, "finish", expected_head=head)
    assert result["blocker"] == "uncommitted_or_untracked_work"
    assert worktree.joinpath("important.sqlite3").read_bytes() == b"not reproducible"
    assert not (repo / "feature.txt").exists()


def test_integrated_cleanup_is_resumable_after_original_checkout_changes(repo, monkeypatch):
    created = perform(repo, "create")
    worktree = Path(created["worktree"])
    head = commit_change(worktree, "landed.txt", "merged\n")
    monkeypatch.setattr(manager, "_in_use", lambda _path: True)
    first = perform(repo, "finish", expected_head=head)
    assert first["status"] == "blocked" and first["integrated"]
    (repo / "ongoing.txt").write_text("unrelated post-merge worker edit\n")
    monkeypatch.setattr(manager, "_in_use", lambda _path: False)
    finished = perform(repo, "finish", expected_head=head)
    assert finished["cleaned"]
    assert not worktree.exists()
    assert (repo / "ongoing.txt").read_text() == "unrelated post-merge worker edit\n"


def test_interrupted_after_worktree_removal_finishes_branch_retirement(repo, monkeypatch):
    created = perform(repo, "create")
    worktree = Path(created["worktree"])
    head = commit_change(worktree, "landed.txt", "merged\n")
    monkeypatch.setattr(manager, "_in_use", lambda _path: True)
    assert perform(repo, "finish", expected_head=head)["blocker"] == "worktree_in_use"
    git(repo, "worktree", "remove", str(worktree))
    assert not worktree.exists()
    monkeypatch.setattr(manager, "_in_use", lambda _path: False)
    finished = perform(repo, "finish", expected_head=head)
    assert finished["cleaned"] and finished["merged"]
    assert git(repo, "branch", "--list", created["branch"]) == ""


def test_creating_receipt_retries_same_existing_worktree(repo):
    created = perform(repo, "create")
    marker = next(manager.RECORD_ROOT.glob("*.json"))
    import json
    data = json.loads(marker.read_text())
    data["state"] = "creating"
    marker.write_text(json.dumps(data))
    reused = perform(repo, "create")
    assert reused["reused"] is True and reused["state"] == "active"
    assert len(list(manager.WORKTREE_ROOT.iterdir())) == 1


def test_secondary_agent_checkout_cannot_spawn_nested_owned_worktree(repo):
    other = repo.parent / "existing-agent"
    git(repo, "worktree", "add", "-b", "existing-agent-branch", str(other), "HEAD")
    assert other.is_dir()
    with pytest.raises(actions.ProbeError, match="secondary worktree"):
        manager.managed_worktree(other, other.name, {
            "kind": "managed_worktree", "mode": "create", "task_ref": REF_1,
        })
    assert other.is_dir()
    assert not manager.WORKTREE_ROOT.exists() or not list(manager.WORKTREE_ROOT.glob("*"))


@pytest.fixture
def preexisting_copy(repo, monkeypatch):
    """Simulate a Dot task that created its isolated checkout before binding."""
    parent = repo.parent / "projects"
    parent.mkdir()
    monkeypatch.setattr(manager, "ADOPT_ROOTS", (parent,))
    worktree = parent / "project-orphan-recovery-20261015"
    branch = "chatgpt/research-orphan-recovery-20261015"
    git(repo, "worktree", "add", "-b", branch, str(worktree), "HEAD")
    head = git(worktree, "rev-parse", "HEAD")
    return worktree, branch, head


def test_adopt_existing_clean_worktree_without_second_copy(repo, preexisting_copy):
    worktree, branch, head = preexisting_copy
    before = git(repo, "rev-parse", "HEAD")
    adopted = perform(repo, "adopt", worktree=str(worktree),
                      expected_branch=branch, expected_head=head)
    assert adopted["status"] == "ok" and adopted["adopted"] is True
    assert adopted["worktree"] == str(worktree)
    assert adopted["branch"] == branch
    assert adopted["head"] == head
    assert git(repo, "rev-parse", "HEAD") == before
    assert perform(repo, "status")["head"] == head
    assert perform(repo, "adopt", worktree=str(worktree),
                   expected_branch=branch, expected_head=head)["reused"]
    assert not any(manager.WORKTREE_ROOT.iterdir())
    assert git(worktree, "status", "--porcelain") == ""


def test_adopt_existing_dirty_worktree_preserves_data_until_commit(repo, preexisting_copy):
    worktree, branch, head = preexisting_copy
    (worktree / "unsaved-draft.txt").write_text("preserve changes")
    adopted = perform(repo, "adopt", worktree=str(worktree),
                      expected_branch=branch, expected_head=head)
    assert adopted["adopted"]
    fail = perform(repo, "finish", expected_head=head)
    assert fail["blocker"] == "uncommitted_or_untracked_work"
    assert worktree.joinpath("unsaved-draft.txt").read_text() == "preserve changes"
    committed_head = git(worktree, "rev-parse", "HEAD")
    assert committed_head == head


def test_adopted_worktree_integrates_and_retires_original_without_force(repo, preexisting_copy):
    worktree, branch, head = preexisting_copy
    perform(repo, "adopt", worktree=str(worktree),
            expected_branch=branch, expected_head=head)
    updated = commit_change(worktree, "recovered.txt", "recovered product source\\n")
    before = git(repo, "rev-parse", "HEAD")
    assert before == head
    result = perform(repo, "finish", expected_head=updated)
    assert result["status"] == "ok" and result["cleaned"]
    assert (repo / "recovered.txt").read_text() == "recovered product source\\n"
    assert not worktree.exists()
    assert git(repo, "branch", "--list", branch) == ""
    assert perform(repo, "finish", expected_head=updated)["already_finished"]


def test_adopt_does_not_merge_into_dirty_original(repo, preexisting_copy):
    worktree, branch, head = preexisting_copy
    (repo / "a.txt").write_text("other agent is editing\\n")
    result = perform(repo, "adopt", worktree=str(worktree),
                     expected_branch=branch, expected_head=head)
    assert result["adopted"] and result["target_dirty"]
    commit = commit_change(worktree, "feature.txt", "feature\\n")
    blocked = perform(repo, "finish", expected_head=commit)
    assert blocked["blocker"] == "target_checkout_dirty"
    assert (repo / "a.txt").read_text() == "other agent is editing\\n"
    assert worktree.exists() and not (repo / "feature.txt").exists()


def test_adopt_rejects_wrong_branch_head_or_unregistered_path(repo, preexisting_copy):
    worktree, branch, head = preexisting_copy
    for args in (
        {"worktree": str(worktree), "expected_branch": branch, "expected_head": "a"*40},
        {"worktree": str(worktree), "expected_branch": "chatgpt/other", "expected_head": head},
        {"worktree": str(repo), "expected_branch": branch, "expected_head": head},
        {"worktree": str(repo.parent / "another"), "expected_branch": branch, "expected_head": head},
        {"worktree": "/tmp/nope", "expected_branch": branch, "expected_head": head},
    ):
        with pytest.raises(actions.ProbeError):
            perform(repo, "adopt", **args)
    assert not any(manager.RECORD_ROOT.glob("*.json"))
    assert worktree.is_dir()


def test_adopt_is_bound_to_one_exact_native_task(repo, preexisting_copy):
    worktree, branch, head = preexisting_copy
    perform(repo, "adopt", REF_1, worktree=str(worktree),
            expected_branch=branch, expected_head=head)
    with pytest.raises(actions.ProbeError, match="another task"):
        perform(repo, "adopt", REF_2, worktree=str(worktree),
                expected_branch=branch, expected_head=head)
    assert worktree.exists()
    assert perform(repo, "status", REF_1)["worktree"] == str(worktree)


def test_adopted_worktree_waits_for_running_process_before_merge(repo, preexisting_copy, monkeypatch):
    worktree, branch, head = preexisting_copy
    perform(repo, "adopt", worktree=str(worktree),
            expected_branch=branch, expected_head=head)
    commit = commit_change(worktree, "feature.txt", "ready\\n")
    monkeypatch.setattr(manager, "_in_use", lambda _path: True)
    blocked = perform(repo, "finish", expected_head=commit)
    assert blocked["blocker"] == "worktree_in_use"
    assert not (repo / "feature.txt").exists()
    assert worktree.exists()
    monkeypatch.setattr(manager, "_in_use", lambda _path: False)
    assert perform(repo, "finish", expected_head=commit)["cleaned"]


def test_schema_advertises_adopt_without_broad_shell_surface():
    validator = Draft202012Validator(actions.PROJECT_ACTION_INPUT_SCHEMA)
    valid = {"project": "street-story", "action": {
        "kind": "managed_worktree", "mode": "adopt", "task_ref": REF_1,
        "worktree": "/home/dev/projects/street-story-research-orphan-recovery-20261015",
        "expected_branch": "chatgpt/research-orphan-recovery-20261015",
        "expected_head": "a"*40,
    }}
    assert not list(validator.iter_errors(valid))
    assert list(validator.iter_errors({"project": "street-story", "action": {
        **valid["action"], "command": "rm -rf /"}}))
