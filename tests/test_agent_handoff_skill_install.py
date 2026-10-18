"""Tests for the durable, bounded, non-duplicating agent handoff procedure."""
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load():
    path = ROOT / "scripts/install-agent-handoff-skill.py"
    spec = importlib.util.spec_from_file_location("devcoveer_agent_handoff_install", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_handoff_rule_preserves_unrelated_global_instructions(tmp_path):
    module = load()
    original = "# Global\nKeep this.\n\n<!-- DEVCOVEER_INTERSERVICE_POLICY_START -->\nold shared rule\n<!-- DEVCOVEER_INTERSERVICE_POLICY_END -->\n"
    applied = module.transformed(original)
    assert applied.count(module.START) == 1
    assert applied.count(module.END) == 1
    assert original.strip() in applied
    assert module.transformed(applied) == applied
    module.atomic(tmp_path / "AGENTS.md", applied)
    assert (tmp_path / "AGENTS.md").read_text() == applied
    assert (tmp_path / "AGENTS.md").stat().st_mode & 0o077 == 0o044


def test_skill_requires_exact_thread_and_bounded_followup():
    text = (ROOT / "skills/devserver-agent-handoff/SKILL.md").read_text(encoding="utf-8")
    assert "message_ts" in text
    assert "original thread" in text
    assert "15–30 seconds" in text
    assert "sent_unconfirmed" in text
    assert "acknowledged" in text
    assert "verified_complete" in text
    assert "no reply" in text.lower()
    assert "No secrets" in text


def test_canonical_install_reapplies_handoff_policy():
    source = (ROOT / "scripts/install-current-canonical-runtime.sh").read_text()
    assert source.count('"$SRC/scripts/install-agent-handoff-skill.py"') == 2
    assert '"$SRC/scripts/install-agent-handoff-skill.py" --apply' in source
