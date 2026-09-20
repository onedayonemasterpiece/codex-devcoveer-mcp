from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from runtime.source_handoff import stage_source_handoff
from runtime.street_story_operator import (
    CommandResult,
    OperatorError,
    StreetStoryOperator,
    TARGET_BRANCH,
    TARGET_HOSTNAME,
    TARGET_PORT,
    TARGET_REPOSITORY,
    TARGET_SERVICE,
)


def _run(argv: list[str], cwd: Path | None = None) -> str:
    return subprocess.check_output(argv, cwd=cwd, text=True).strip()


def _fixture_handoff(tmp_path: Path) -> tuple[Path, Path, str, str]:
    repo = tmp_path / "street-story"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Street Story Test"], cwd=repo, check=True)
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    base = _run(["git", "rev-parse", "HEAD"], repo)

    (repo / "app.txt").write_text("base\ncurrent\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
    patch = subprocess.check_output(
        ["git", "diff", "--cached", "--binary", base],
        cwd=repo,
    ).decode()
    patch_sha = hashlib.sha256(patch.encode()).hexdigest()
    subprocess.run(["git", "reset", "--hard", base], cwd=repo, check=True, stdout=subprocess.DEVNULL)

    handoff_root = tmp_path / "handoffs"
    staged = stage_source_handoff(
        repo,
        "street-story",
        base,
        patch,
        patch_sha,
        root=handoff_root,
    )
    workspace = handoff_root / staged["handoffId"] / "worktree"
    tree = _run(["git", "write-tree"], workspace)
    return repo, handoff_root, staged["handoffId"], tree


def _operator(
    tmp_path: Path,
    repo: Path,
    handoff_root: Path,
    remote_head: str,
    remote_tree: str,
) -> StreetStoryOperator:
    def runner(argv: list[str], cwd: Path | None, env, timeout: int) -> CommandResult:
        del env, timeout
        if argv[:4] == ["git", "-C", str(repo), "fetch"]:
            return CommandResult(0, "")
        if argv[:4] == ["git", "-C", str(repo), "rev-parse"]:
            value = argv[4]
            if value == f"refs/remotes/origin/{TARGET_BRANCH}":
                return CommandResult(0, remote_head + "\n")
            if value == f"{remote_head}^{{tree}}":
                return CommandResult(0, remote_tree + "\n")
        completed = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if completed.returncode:
            raise OperatorError(completed.stdout)
        return CommandResult(completed.returncode, completed.stdout)

    return StreetStoryOperator(
        state_root=tmp_path / "operator-state",
        project_root=repo,
        releases_root=tmp_path / "releases",
        current_link=tmp_path / "current",
        app_state_root=tmp_path / "app-state",
        vpn_root=tmp_path / "vpn",
        user_unit_root=tmp_path / "units",
        certbot_root=tmp_path / "certbot",
        vibe_source=tmp_path / "vibe",
        vibe_py=tmp_path / "vibe-python",
        vibe_db=tmp_path / "vibe.sqlite3",
        vibe_owner_token_file=tmp_path / "owner-token",
        host_env=tmp_path / "host.env",
        handoff_root=handoff_root,
        runner=runner,
    )


def test_operator_targets_are_compile_time_allowlisted() -> None:
    assert TARGET_REPOSITORY == "onedayonemasterpiece/street-story"
    assert TARGET_BRANCH == "work/street-story-mvp-20260908"
    assert TARGET_HOSTNAME == "street-story.kenigevents.ru"
    assert TARGET_PORT == 8080
    assert TARGET_SERVICE == "street-story.service"


def test_source_gate_accepts_only_matching_current_head_tree(tmp_path: Path) -> None:
    repo, handoff_root, handoff_id, tree = _fixture_handoff(tmp_path)
    current_sha = "a" * 40
    operator = _operator(tmp_path, repo, handoff_root, current_sha, tree)

    workspace, observed_tree = operator._verify_approved_source(current_sha, handoff_id)

    assert workspace.name == "worktree"
    assert observed_tree == tree


def test_source_gate_rejects_non_current_sha(tmp_path: Path) -> None:
    repo, handoff_root, handoff_id, tree = _fixture_handoff(tmp_path)
    operator = _operator(tmp_path, repo, handoff_root, "a" * 40, tree)

    with pytest.raises(OperatorError, match="not the current Street Story PR branch HEAD"):
        operator._verify_approved_source("b" * 40, handoff_id)


def test_source_gate_rejects_handoff_tree_mismatch(tmp_path: Path) -> None:
    repo, handoff_root, handoff_id, _tree = _fixture_handoff(tmp_path)
    current_sha = "a" * 40
    operator = _operator(tmp_path, repo, handoff_root, current_sha, "b" * 40)

    with pytest.raises(OperatorError, match="handoff tree does not match current remote HEAD"):
        operator._verify_approved_source(current_sha, handoff_id)


def test_edge_target_rejects_arbitrary_hostname_before_host_access(tmp_path: Path) -> None:
    operator = StreetStoryOperator(state_root=tmp_path / "operator")
    with pytest.raises(OperatorError, match="edge target is not allowlisted"):
        operator.publish_edge_route(
            hostname="example.invalid",
            loopback_port=TARGET_PORT,
            idempotency_key="street-story-edge-test-001",
        )
