from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-default-opencode-policy.py"

def _load():
    spec = importlib.util.spec_from_file_location("install_default_opencode_policy", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _fixture(module) -> str:
    return (
        "# fixture\n"
        + module.OLD_START_TOOL
        + "\n\n"
        + module.OLD_HANDOFF_TOOL
        + "\n\n"
        + module.OLD_ROUTING
        + "\n# end\n"
    )

def test_transform_enforces_explicit_codex_and_adaptive_opencode():
    module = _load()
    transformed = module.transform_bridge(_fixture(module))
    assert '"reason": "explicit_codex_provider"' in transformed
    assert "default_codex" not in transformed
    assert "codex_model_match" not in transformed
    assert "adaptive_opencode_default" in transformed
    assert "default_opencode_fallback" in transformed
    assert "_model_discovery_payload(history, None, False)" in transformed
    assert 'provider=None if adaptive_model is not None else "opencode"' in transformed
    assert 'explicit provider="codex"' in transformed
    assert "Never silently fall back from OpenCode to native Codex" in transformed

def test_transform_advertises_source_handoff_fallback():
    module = _load()
    transformed = module.transform_bridge(_fixture(module))
    assert "ChatGPT-side" in transformed
    assert "GitHub connector" in transformed
    assert "handoff:<id>" in transformed
    assert "DevCoveer git/gh" in transformed
    assert "cleanup_source_handoff" in transformed
    assert "separate venv" in transformed
    assert "persistent patch" in transformed

@pytest.mark.parametrize("label", ["routing", "start", "handoff"])
def test_transform_fails_closed_when_expected_snippet_missing(label):
    module = _load()
    fixture = _fixture(module)
    if label == "routing":
        fixture = fixture.replace(module.OLD_ROUTING, "", 1)
    elif label == "start":
        fixture = fixture.replace(module.OLD_START_TOOL, "", 1)
    else:
        fixture = fixture.replace(module.OLD_HANDOFF_TOOL, "", 1)
    with pytest.raises(SystemExit):
        module.transform_bridge(fixture)

def test_transform_fails_closed_when_expected_snippet_duplicated():
    module = _load()
    fixture = _fixture(module) + "\n" + module.OLD_ROUTING
    with pytest.raises(SystemExit):
        module.transform_bridge(fixture)
