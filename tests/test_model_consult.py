from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
BUILDER = ROOT / "scripts" / "build-clean-host-runtime.py"
INSTALLER = ROOT / "scripts" / "install-current-canonical-runtime.sh"

sys.path.insert(0, str(RUNTIME))
from model_consult import (  # noqa: E402
    CONSULT_MODEL_INPUT_SCHEMA,
    consultation_metadata,
    consultation_prompt,
    model_spec,
    persist_consultation,
)


def test_consult_aliases_are_exact_nvidia_models() -> None:
    assert model_spec("kimi_k3")["selection"] == "nvidia/moonshotai/kimi-k3"
    assert model_spec("deepseek")["selection"] == "nvidia/deepseek-ai/deepseek-v4.1-flash"
    assert CONSULT_MODEL_INPUT_SCHEMA["properties"]["model"]["enum"] == ["deepseek", "kimi_k3"]


def test_consult_prompt_is_read_only_advisory() -> None:
    text = consultation_prompt(purpose="edge_cases", question="Find the missing edge cases.")
    assert "READ ONLY" in text
    assert "advisory" in text
    assert "do not edit files" in text
    assert "calling ChatGPT remains the decision owner" in text
    assert "Edge cases" in text


def test_consult_markdown_is_terminal_idempotent_and_redacted(tmp_path: Path) -> None:
    question = "Review the release edge cases"
    task = {
        "taskId": "dvt_" + "a" * 32,
        "selection": "nvidia/moonshotai/kimi-k3",
        "createdAt": 1000,
        "finishedAt": 4200,
        "consultation": consultation_metadata(alias="kimi_k3", purpose="code_review", question=question),
    }
    first = persist_consultation(
        task, "completed", "Useful result. Authorization: Bearer super-secret-value",
        root=tmp_path,
    )
    second = persist_consultation(
        task, "completed", "Useful result. Authorization: Bearer super-secret-value",
        root=tmp_path,
    )
    assert first["status"] == "ok"
    assert first["format"] == "markdown"
    assert second["reused"] is True
    body = Path(first["path"]).read_text(encoding="utf-8")
    assert "# Model consultation" in body
    assert "Latency ms: 3200" in body
    assert "Useful result." in body
    assert "super-secret-value" not in body
    assert "[REDACTED]" in body


def test_builder_materializes_consult_model_surface() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "runtime"
        completed = subprocess.run(
            [sys.executable, str(BUILDER), str(out)],
            text=True, capture_output=True, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        bridge = (out / "codex-chatgpt-bridge.py").read_text(encoding="utf-8")
        assert 'name="consult_model"' in bridge
        assert '"consult_model": Draft202012Validator(CONSULT_MODEL_INPUT_SCHEMA)' in bridge
        assert 'if name == "consult_model":' in bridge
        assert 'access="read"' in bridge
        assert "model_consultation" in bridge
        assert "persist_consultation as _persist_consultation" in bridge
        assert 'BRIDGE_VERSION = "3.32.0-model-consult"' in bridge
        assert (out / "model_consult.py").is_file()


def test_installer_requires_consult_model_in_candidate_and_wrapper() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.count('"consult_model"') >= 2
