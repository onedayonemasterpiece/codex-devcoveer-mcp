from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))

import direct_actions as actions
import direct_ops_gateway as gateway
import direct_probe_project_git as project_git
import workspace_selection as workspace


def run(coro):
    return asyncio.run(coro)


def process_result(stdout: bytes = b"", code: int = 0):
    return SimpleNamespace(
        exit_code=code,
        stdout=stdout,
        stderr=b"",
        truncated=False,
        duration_ms=1,
    )


def test_parse_worktrees_exposes_only_tool_addressable_projects(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    main = projects / "events-bot-new"
    ui = projects / "events-bot-new-islands-home"
    hidden_parent = tmp_path / "state"
    handoff = hidden_parent / "worktree"
    for path in (main, ui, handoff):
        path.mkdir(parents=True)
    raw = (
        f"worktree {main}\0HEAD {'1' * 40}\0branch refs/heads/main\0\0"
        f"worktree {ui}\0HEAD {'2' * 40}\0branch refs/heads/feature/ui\0\0"
        f"worktree {handoff}\0HEAD {'3' * 40}\0detached\0\0"
    )
    rows = workspace.parse_worktree_porcelain(
        raw,
        current_path=ui,
        projects_root=projects,
    )
    assert [row["project"] for row in rows] == [
        "events-bot-new-islands-home",
        "events-bot-new",
    ]
    assert rows[0]["current"] is True
    assert all(row["tool_addressable"] is True for row in rows)
    assert all(str(handoff) != row["path"] for row in rows)


def test_workspace_context_is_actionable_but_does_not_make_exact_resolution_ambiguous(monkeypatch):
    related = [
        {"project": "events-bot-new", "head": "1" * 40, "current": True},
        {"project": "events-bot-new-islands-home", "head": "2" * 40, "current": False},
    ]
    monkeypatch.setattr(workspace, "related_addressable_worktrees", lambda _path: related)
    context = workspace.workspace_context("/repo")
    assert context["addressableWorktreeCount"] == 2
    assert context["continuationCheckRequired"] is True
    assert context["relatedWorktrees"] == related
    assert "expected_head" in context["guidance"]
    assert "does not select" in context["guidance"]


def test_workspace_context_stays_quiet_for_single_checkout(monkeypatch):
    monkeypatch.setattr(
        workspace,
        "related_addressable_worktrees",
        lambda _path: [{"project": "only", "head": "1" * 40, "current": True}],
    )
    assert workspace.workspace_context("/repo") is None


def test_worktree_list_adds_project_hint_for_addressable_rows(tmp_path, monkeypatch):
    root = tmp_path / "events-bot-new"
    sibling = tmp_path / "events-bot-new-islands-home"
    root.mkdir()
    sibling.mkdir()
    raw = (
        f"worktree {root}\0HEAD {'1' * 40}\0branch refs/heads/main\0\0"
        f"worktree {sibling}\0HEAD {'2' * 40}\0branch refs/heads/feature/ui\0\0"
    ).encode()

    async def fake_git(*_args, **_kwargs):
        return process_result(raw)

    monkeypatch.setattr(project_git, "_git", fake_git)
    monkeypatch.setattr(
        project_git,
        "addressable_project_name",
        lambda path: Path(path).name,
    )
    data = run(project_git._worktree_list(root))
    assert data["worktrees"][0]["project"] == "events-bot-new"
    assert data["worktrees"][0]["current"] is True
    assert data["worktrees"][1]["project"] == "events-bot-new-islands-home"
    assert data["worktrees"][1]["tool_addressable"] is True


@pytest.mark.parametrize(
    "operation",
    ["sync_ff", "run_tool", "run_profile", "write_text", "git_stage", "git_commit_push", "job_start", "batch"],
)
def test_guarded_actions_expose_expected_head(operation):
    schema = gateway.describe_plane("project_action", operation)["payload_schema"]
    expected = schema["properties"]["expected_head"]
    assert expected["pattern"] == r"^[0-9a-f]{40}$"
    assert "checkpoint" in expected["description"]


def test_status_action_does_not_require_workspace_head_guard():
    schema = gateway.describe_plane("project_action", "job_status")["payload_schema"]
    assert "expected_head" not in schema["properties"]


def test_expected_head_guard_accepts_exact_checkpoint(tmp_path, monkeypatch):
    expected = "a" * 40

    async def fake_git(_root, *args, **_kwargs):
        assert args == ("rev-parse", "HEAD")
        return process_result((expected + "\n").encode())

    monkeypatch.setattr(actions, "_git", fake_git)
    assert run(actions._guard_expected_head(tmp_path, {"expected_head": expected})) == expected


def test_expected_head_guard_fails_before_wrong_workspace_execution(tmp_path, monkeypatch):
    expected = "a" * 40
    observed = "b" * 40

    async def fake_git(_root, *args, **_kwargs):
        return process_result((observed + "\n").encode())

    monkeypatch.setattr(actions, "_git", fake_git)
    with pytest.raises(actions.ProbeError) as failure:
        run(actions._guard_expected_head(tmp_path, {"expected_head": expected}))
    assert failure.value.category == "workspace_drift"
    assert failure.value.status == "denied"
    assert expected in str(failure.value)
    assert observed in str(failure.value)
    assert "find_projects" in str(failure.value)
