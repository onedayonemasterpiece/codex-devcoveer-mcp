from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build-clean-host-runtime.py"


def test_consultation_uses_safe_build_agent_and_empty_output_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "runtime"
        completed = subprocess.run(
            [sys.executable, str(BUILDER), str(out)],
            text=True, capture_output=True, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        backend = (out / "opencode_backend.py").read_text(encoding="utf-8")
        bridge = (out / "codex-chatgpt-bridge.py").read_text(encoding="utf-8")
        start = backend.index("def consultation_directory")
        end = backend.index("async def cancel", start)
        consult = backend[start:end]
        assert "async def prompt_consultation" in consult
        assert '"agent": "plan" if provider == "opencode" else "build"' in consult
        assert 'if provider != "opencode":' in consult
        assert 'payload["tools"] = self.consultation_tools' in consult
        for tool in ("bash", "task", "edit", "write", "patch", "apply_patch", "todowrite", "todoread"):
            assert f'"{tool}": False' in consult
        for tool in ("read", "glob", "grep", "list", "lsp", "websearch", "webfetch"):
            assert f'"{tool}": False' in consult
        assert "model-consult-provided-only" in backend
        assert '"action": "deny"' in consult
        assert "dispatchUnknown" in consult
        assert "dispatch_unknown" in bridge
        assert "empty_consultation_output" in bridge
        assert '"empty_output" if empty_consultation_output' in bridge
        assert "Model consultation returned no visible output." in bridge
