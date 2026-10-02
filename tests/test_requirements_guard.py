from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from runtime.requirements_guard import (
    MANIFEST_PATH,
    ProbeError,
    consume_requirements_authorization,
    guard_requirement_mutation,
    requirements_commit_guard,
    requirements_context,
    requirements_prompt,
    update_requirements,
)


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
    path.mkdir()
    subprocess.check_call(["/usr/bin/git", "init", "-q", "-b", "main", str(path)])
    git(path, "config", "user.name", "T")
    git(path, "config", "user.email", "t@example.invalid")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "base")
    return path


def manifest(statement: str = "Keep automatic object identification.") -> str:
    return json.dumps({
        "schema_version": 1,
        "project": "demo",
        "requirements": [{
            "id": "CORE-001",
            "statement": statement,
            "critical": True,
            "acceptance": ["The user is not required to identify the object manually."],
        }],
    }, ensure_ascii=False)


def test_unconfigured_contract_is_zero_ceremony(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = requirements_context(repo)
    assert context["configured"] is False
    assert context["manifestPath"] == MANIFEST_PATH
    assert requirements_prompt(repo) == ""


def test_explicit_update_writes_canonical_contract_and_prompt(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path / "repo")
    monkeypatch.setattr("runtime.requirements_guard.STATE_ROOT", tmp_path / "state")
    result = update_requirements(repo, {
        "kind": "requirements_update",
        "content": manifest(),
        "reason": "Owner defined the critical product contour.",
        "authorization_ref": "voice:voice-20261002-122522-dc517f1b",
    })
    assert result["requirementCount"] == 1
    assert len(result["newDigest"]) == 64

    context = requirements_context(repo)
    assert context["configured"] is True
    assert context["digest"] == result["newDigest"]
    assert context["criticalRequirementIds"] == ["CORE-001"]
    prompt = requirements_prompt(repo)
    assert "PROJECT REQUIREMENTS CONTRACT" in prompt
    assert "CORE-001" in prompt
    assert "automatic object identification" in prompt


def test_generic_mutations_cannot_change_contract(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    for action in (
        {"kind": "write_text", "path": MANIFEST_PATH},
        {"kind": "delete_file", "path": MANIFEST_PATH},
        {"kind": "move_path", "source": MANIFEST_PATH, "target": "requirements.old"},
        {"kind": "chmod_executable", "path": MANIFEST_PATH},
        {
            "kind": "apply_source_patch",
            "patch": (
                "diff --git a/.devcoveer/requirements.json b/.devcoveer/requirements.json\n"
                "--- a/.devcoveer/requirements.json\n"
                "+++ b/.devcoveer/requirements.json\n"
            ),
        },
    ):
        with pytest.raises(ProbeError) as exc:
            guard_requirement_mutation(repo, action)
        assert exc.value.category == "requirements_change_required"


def test_commit_guard_binds_authorization_to_staged_blob(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path / "repo")
    monkeypatch.setattr("runtime.requirements_guard.STATE_ROOT", tmp_path / "state")
    result = update_requirements(repo, {
        "kind": "requirements_update",
        "content": manifest(),
        "reason": "Explicit owner requirement.",
        "authorization_ref": "task:requirements-1",
    })
    git(repo, "add", MANIFEST_PATH)

    approved = requirements_commit_guard(repo)
    assert approved is not None
    assert approved["requirementsDigest"] == result["newDigest"]
    assert approved["authorizationRef"] == "task:requirements-1"

    # An unstaged working-tree edit does not change what the commit guard evaluates.
    path = repo / MANIFEST_PATH
    path.write_text(manifest("Different unstaged text."), encoding="utf-8")
    still_approved = requirements_commit_guard(repo)
    assert still_approved["requirementsDigest"] == result["newDigest"]

    # Once that different content is staged, the old authorization no longer applies.
    git(repo, "add", MANIFEST_PATH)
    with pytest.raises(ProbeError) as exc:
        requirements_commit_guard(repo)
    assert exc.value.category == "requirements_change_required"

    consume_requirements_authorization(repo)


def test_staged_contract_without_explicit_update_is_denied(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path / "repo")
    monkeypatch.setattr("runtime.requirements_guard.STATE_ROOT", tmp_path / "state")
    path = repo / MANIFEST_PATH
    path.parent.mkdir()
    path.write_text(manifest(), encoding="utf-8")
    git(repo, "add", MANIFEST_PATH)
    with pytest.raises(ProbeError) as exc:
        requirements_commit_guard(repo)
    assert exc.value.category == "requirements_change_required"


def test_duplicate_requirement_ids_fail_closed(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path / "repo")
    monkeypatch.setattr("runtime.requirements_guard.STATE_ROOT", tmp_path / "state")
    bad = json.dumps({
        "schema_version": 1,
        "project": "demo",
        "requirements": [
            {"id": "CORE-001", "statement": "one"},
            {"id": "CORE-001", "statement": "two"},
        ],
    })
    with pytest.raises(ProbeError) as exc:
        update_requirements(repo, {
            "kind": "requirements_update",
            "content": bad,
            "reason": "test",
            "authorization_ref": "task:test",
        })
    assert exc.value.category == "requirements_invalid"