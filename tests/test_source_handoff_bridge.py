"""Integration tests for Source Handoff in the Codex DevCoveer MCP bridge.

Tests verify:
- stage_source_handoff and cleanup_source_handoff MCP method wiring
- handoff:<id> project resolution
- Existing MCP method table preservation
- Error cases (wrong SHA256, invalid base_sha, malformed patch)
- Idempotent handoff
- Canonical checkout unchanged
- No handoff .venv
- No transport artifacts (.patch/.zip/.tar/.gz)
"""
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


class TestStageSourceHandoff:
    """Tests for stage_source_handoff MCP method behavior."""

    def test_materializes_patch_in_isolated_worktree(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        patch = build_patch(repo, base)
        root = tmp_path / "handoffs"

        result = stage(repo, base, patch, root)
        workspace = resolve_source_handoff(result["handoffId"], root=root)

        # Canonical checkout unchanged
        assert (repo / "change.txt").read_text() == "old\n"
        assert not (repo / "added.txt").exists()
        assert (repo / "delete.txt").exists()

        # Worktree has the patch applied
        assert (workspace / "change.txt").read_text() == "new\n"
        assert (workspace / "added.txt").read_text() == "added\n"
        assert not (workspace / "delete.txt").exists()
        assert sorted(result["changedFiles"]) == ["added.txt", "change.txt", "delete.txt"]

        # Staged changes exist in worktree
        quiet = subprocess.run(["git", "-C", str(workspace), "diff", "--cached", "--quiet", base])
        assert quiet.returncode == 1

    def test_returns_handoff_id_and_project(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        patch = build_patch(repo, base)
        root = tmp_path / "handoffs"

        result = stage(repo, base, patch, root)

        assert "handoffId" in result
        assert result["handoffId"].startswith("src_")
        assert result["project"] == f"handoff:{result['handoffId']}"
        assert result["baseSha"] == base
        assert result["patchSha256"] == hashlib.sha256(patch).hexdigest()
        assert result["reused"] is False

    def test_no_transport_artifacts(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        patch = build_patch(repo, base)
        root = tmp_path / "handoffs"

        result = stage(repo, base, patch, root)
        handoff_dir = root / result["handoffId"]

        # Only meta.json and worktree directory
        assert sorted(p.name for p in handoff_dir.iterdir()) == ["meta.json", "worktree"]
        # No transport artifacts
        assert not any(p.suffix in {".patch", ".zip", ".tar", ".gz"} for p in handoff_dir.rglob("*"))

    def test_no_handoff_venv(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        patch = build_patch(repo, base)
        root = tmp_path / "handoffs"

        result = stage(repo, base, patch, root)
        handoff_dir = root / result["handoffId"]

        assert not (handoff_dir / ".venv").exists()

    def test_meta_json_correct(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        patch = build_patch(repo, base)
        root = tmp_path / "handoffs"

        result = stage(repo, base, patch, root)
        handoff_dir = root / result["handoffId"]
        meta = json.loads((handoff_dir / "meta.json").read_text())

        assert meta["baseSha"] == base
        assert meta["patchSha256"] == hashlib.sha256(patch).hexdigest()
        assert meta["projectId"] == "example-project"
        assert meta["projectRoot"] == str(repo.resolve())


class TestCleanupSourceHandoff:
    """Tests for cleanup_source_handoff MCP method behavior."""

    def test_removes_worktree_and_metadata(self, tmp_path: Path) -> None:
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

    def test_idempotent_cleanup(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        patch = build_patch(repo, base)
        root = tmp_path / "handoffs"
        result = stage(repo, base, patch, root)

        cleanup_source_handoff(result["handoffId"], root=root)
        again = cleanup_source_handoff(result["handoffId"], root=root)
        assert again["removed"] is False

    def test_no_transport_artifacts_after_cleanup(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        patch = build_patch(repo, base)
        root = tmp_path / "handoffs"
        result = stage(repo, base, patch, root)

        cleanup_source_handoff(result["handoffId"], root=root)
        handoff_dir = root / result["handoffId"]
        assert not handoff_dir.exists()


class TestHandoffProjectResolution:
    """Tests for handoff:<id> project resolution."""

    def test_handoff_project_resolves_to_workspace(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        patch = build_patch(repo, base)
        root = tmp_path / "handoffs"
        result = stage(repo, base, patch, root)

        workspace = resolve_source_handoff(result["handoffId"], root=root)
        assert workspace.is_dir()

        # The workspace contains the patched files
        assert (workspace / "change.txt").read_text() == "new\n"
        assert (workspace / "added.txt").read_text() == "added\n"
        assert not (workspace / "delete.txt").exists()

    def test_invalid_handoff_id_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "handoffs"
        with pytest.raises(SourceHandoffError, match="invalid source handoff id"):
            resolve_source_handoff("../escape", root=root)
        with pytest.raises(SourceHandoffError, match="invalid source handoff id"):
            cleanup_source_handoff("/tmp/anything", root=root)


class TestErrorCases:
    """Tests for various error conditions."""

    def test_wrong_sha256_fails(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        patch = build_patch(repo, base)
        root = tmp_path / "handoffs"

        with pytest.raises(SourceHandoffError, match="sha256 mismatch"):
            stage_source_handoff(
                repo, "example-project", base, patch, "0" * 64, root=root
            )
        assert not root.exists() or list(root.iterdir()) == []

    def test_invalid_base_sha_fails(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        patch = build_patch(repo, base)
        root = tmp_path / "handoffs"

        with pytest.raises(SourceHandoffError, match="base commit"):
            stage_source_handoff(
                repo, "example-project", "f" * 40, patch,
                hashlib.sha256(patch).hexdigest(), root=root
            )

    def test_malformed_patch_fails_cleanly(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        root = tmp_path / "handoffs"
        patch = b"not a git patch\n"

        with pytest.raises(SourceHandoffError):
            stage(repo, base, patch, root)
        assert list(root.iterdir()) == []

    def test_empty_patch_fails(self, tmp_path: Path) -> None:
        repo, base = make_repo(tmp_path)
        root = tmp_path / "handoffs"

        with pytest.raises(SourceHandoffError, match="empty"):
            stage(repo, base, b"", root)


class TestIdempotentHandoff:
    """Tests for idempotent handoff behavior."""

    def test_same_input_returns_existing_workspace(self, tmp_path: Path) -> None:
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


class TestMethodTablePreservation:
    """Verify existing MCP methods are not lost."""

    EXISTING_METHODS = frozenset({
        "list_models", "list_tasks", "read_task", "start_task",
        "continue_task", "cancel_task", "council_run",
        "get_task_evidence", "record_task_review", "find_projects",
        "materialize_release", "deploy_user_service",
        "publish_existing_edge_route", "dataset_loop_service",
        "deployment_receipt", "prepare_dataset_loop_owner",
        "start_run", "get_run", "list_runs", "cancel_run",
        "resume_run", "list_artifacts", "get_artifact",
    })

    NEW_METHODS = frozenset({
        "stage_source_handoff", "cleanup_source_handoff",
    })

    def test_bridge_has_all_methods(self) -> None:
        """Parse the candidate bridge and verify method table."""
        candidate_path = Path("/home/dev/.local/libexec/openai-codex-mcp/codex-chatgpt-bridge.candidate")
        if not candidate_path.is_file():
            pytest.skip("candidate bridge not generated yet")

        content = candidate_path.read_text(encoding="utf-8")

        # Check all existing methods are present
        for method in self.EXISTING_METHODS:
            assert f'name="{method}"' in content, f"existing method {method} missing from bridge"

        # Check new methods are present
        for method in self.NEW_METHODS:
            assert f'name="{method}"' in content, f"new method {method} missing from bridge"

    def test_schemas_present(self) -> None:
        """Verify input schemas are defined."""
        candidate_path = Path("/home/dev/.local/libexec/openai-codex-mcp/codex-chatgpt-bridge.candidate")
        if not candidate_path.is_file():
            pytest.skip("candidate bridge not generated yet")

        content = candidate_path.read_text(encoding="utf-8")
        assert "STAGE_SOURCE_HANDOFF_INPUT_SCHEMA" in content
        assert "CLEANUP_SOURCE_HANDOFF_INPUT_SCHEMA" in content

    def test_validators_present(self) -> None:
        """Verify validators are registered."""
        candidate_path = Path("/home/dev/.local/libexec/openai-codex-mcp/codex-chatgpt-bridge.candidate")
        if not candidate_path.is_file():
            pytest.skip("candidate bridge not generated yet")

        content = candidate_path.read_text(encoding="utf-8")
        assert '"stage_source_handoff": Draft202012Validator(' in content
        assert '"cleanup_source_handoff": Draft202012Validator(' in content

    def test_project_resolver_handles_handoff(self) -> None:
        """Verify project resolver has handoff: prefix handling."""
        candidate_path = Path("/home/dev/.local/libexec/openai-codex-mcp/codex-chatgpt-bridge.candidate")
        if not candidate_path.is_file():
            pytest.skip("candidate bridge not generated yet")

        content = candidate_path.read_text(encoding="utf-8")
        assert 'raw_hint.startswith("handoff:")' in content
        assert "_resolve_source_handoff" in content
