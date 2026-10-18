"""Idempotence of server-wide interservice instructions installer."""
import importlib.util
from pathlib import Path


def test_installer_is_idempotent_and_preserves_unrelated_instructions(tmp_path):
    source = Path(__file__).resolve().parents[1] / "scripts/install-interservice-skill.py"
    spec = importlib.util.spec_from_file_location("install_interservice_skill", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = "# Existing operator notes\n- Preserve this rule.\n"
    converted = module.transformed(base)
    assert base.strip() in converted
    assert converted.count(module.START) == 1
    assert converted.count(module.END) == 1
    assert module.transformed(converted) == converted
    updated = module.transformed(converted + "\n## New section\nkeep\n")
    assert updated.count(module.START) == 1
    assert "New section" in updated
    first = tmp_path / "AGENTS.md"
    module.atomic(first, updated)
    assert first.read_text() == updated
    assert first.stat().st_mode & 0o077 == 0o044


def test_skill_declares_same_host_without_bypassing_auth():
    skill = (Path(__file__).resolve().parents[1] /
             "skills/devserver-interservice/SKILL.md").read_text()
    assert "127.0.0.1" in skill
    assert "least-privileged" in skill
    assert "idempotency" in skill
    assert "SHA-256" in skill
    assert "No user bearer forwarding" in skill