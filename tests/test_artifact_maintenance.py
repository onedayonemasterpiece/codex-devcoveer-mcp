from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "artifact_maintenance", ROOT / "scripts" / "maintain-artifacts.py"
)
assert SPEC is not None and SPEC.loader is not None
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)

sys.path.insert(0, str(ROOT / "runtime"))
from model_health import ModelHealthStore


def fixture(tmp_path: Path):
    root = tmp_path / "artifacts"
    directory = root / "street-story"
    directory.mkdir(parents=True)
    repo = tmp_path / "projects"
    (repo / "street-story" / "docs").mkdir(parents=True)
    name = "20261001T080000Z-finished-test"
    run = directory / name
    run.mkdir()
    (run / "result.png").write_bytes(b"x" * 7000)
    old = time.time() - maintenance.MIN_AGE_SECONDS - 3600
    os.utime(run, (old, old))
    return root, repo, run, name


def test_direct_artifact_cleanup_is_exact_and_bounded(tmp_path, monkeypatch):
    root, repo, run, name = fixture(tmp_path)
    monkeypatch.setattr(maintenance, "_is_referenced", lambda *a: False)
    monkeypatch.setattr(maintenance, "_in_use", lambda *a: False)
    plan = maintenance.inspect(root, repo, "street-story", name)
    assert plan["safe_to_delete"]
    assert run.exists()
    with pytest.raises(maintenance.UnsafeArtifact, match="size changed"):
        maintenance.remove(root, repo, "street-story", name, plan["size_kib"] + 1)
    assert run.is_dir()
    result = maintenance.remove(root, repo, "street-story", name, plan["size_kib"])
    assert result["status"] == "removed"
    assert not run.exists()


def test_direct_artifact_cleanup_denies_traversal_and_symlinks(tmp_path):
    root, repo, run, name = fixture(tmp_path)
    with pytest.raises(maintenance.UnsafeArtifact):
        maintenance._run_dir(root, "street-story", "../street-story")
    with pytest.raises(maintenance.UnsafeArtifact):
        maintenance._run_dir(root, "unknown-project", name)
    (run / "shortcut").symlink_to(tmp_path / "outside")
    with pytest.raises(maintenance.UnsafeArtifact, match="symlink"):
        maintenance._check_tree(run)


def test_direct_artifact_cleanup_protects_recent_referenced_and_active(tmp_path, monkeypatch):
    root, repo, run, name = fixture(tmp_path)
    monkeypatch.setattr(maintenance, "_is_referenced", lambda *a: True)
    monkeypatch.setattr(maintenance, "_in_use", lambda *a: False)
    plan = maintenance.inspect(root, repo, "street-story", name)
    assert "referenced_by_source_or_tests" in plan["blockers"]
    with pytest.raises(maintenance.UnsafeArtifact, match="deletion blocked"):
        maintenance.remove(root, repo, "street-story", name, plan["size_kib"])
    monkeypatch.setattr(maintenance, "_is_referenced", lambda *a: False)
    monkeypatch.setattr(maintenance, "_in_use", lambda *a: True)
    assert "open_by_process" in maintenance.inspect(root, repo, "street-story", name)["blockers"]
    monkeypatch.setattr(maintenance, "_in_use", lambda *a: False)
    os.utime(run, None)
    assert "recent_run" in maintenance.inspect(root, repo, "street-story", name)["blockers"]


def test_reference_scan_protects_named_retained_evidence(tmp_path):
    root, repo, run, name = fixture(tmp_path)
    if not Path("/usr/bin/rg").exists():
        pytest.skip("rg not installed on this test host")
    (repo / "street-story" / "docs" / "report.md").write_text(name)
    assert maintenance._is_referenced(repo, "street-story", name)


def test_model_health_write_failure_does_not_break_readback(tmp_path, monkeypatch):
    store = ModelHealthStore(root=tmp_path / "model-health")

    def fail(_events):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_write_locked", fail)
    assert store.record(
        provider="opencode", selection="opencode/test",
        outcome="success", category="ok",
    ) is False


def test_inaccessible_proc_keeps_preview_but_blocks_delete(tmp_path, monkeypatch):
    root, repo, run, name = fixture(tmp_path)
    monkeypatch.setattr(maintenance, "_is_referenced", lambda *a: False)
    def denied(_candidate):
        raise maintenance.UnsafeArtifact("proc access denied")
    monkeypatch.setattr(maintenance, "_in_use", denied)
    plan = maintenance.inspect(root, repo, "street-story", name)
    assert plan["size_kib"] > 0
    assert not plan["safe_to_delete"]
    assert any("process_check_incomplete" in b for b in plan["blockers"])
    with pytest.raises(maintenance.UnsafeArtifact, match="deletion blocked"):
        maintenance.remove(root, repo, "street-story", name, plan["size_kib"])
    assert run.is_dir()
