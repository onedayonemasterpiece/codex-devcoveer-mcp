from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
BUILDER = ROOT / "scripts" / "build-clean-host-runtime.py"

spec = importlib.util.spec_from_file_location(
    "opencode_acp_isolated",
    RUNTIME / "opencode_acp_isolated.py",
)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def test_sandbox_environment_does_not_inherit_credentials(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "must-not-leak")
    env = mod._sandbox_environment(tmp_path)
    assert set(env) == {
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
        "PATH",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "TERM",
    }
    assert "must-not-leak" not in repr(env)
    assert env["HOME"].startswith(str(tmp_path))
    assert env["TMPDIR"].startswith(str(tmp_path))


def test_landlock_policy_handles_filesystem_but_never_executes_sandbox_files() -> None:
    handled = mod._handled_fs(3)
    assert handled & mod._READ_FILE
    assert handled & mod._WRITE_FILE
    assert handled & mod._EXECUTE
    sandbox_access = handled & ~mod._EXECUTE
    assert sandbox_access & mod._READ_FILE
    assert sandbox_access & mod._WRITE_FILE
    assert not sandbox_access & mod._EXECUTE


def test_runner_rejects_non_opencode_model_before_process_launch() -> None:
    with pytest.raises(mod.IsolatedAcpError, match="exact OpenCode"):
        asyncio.run(
            mod.run_isolated_acp_prompt(
                model="nvidia/moonshotai/kimi-k3",
                prompt="Synthetic prompt.",
            )
        )


def test_builder_materializes_isolated_acp_runner() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "runtime"
        completed = subprocess.run(
            [sys.executable, str(BUILDER), str(out)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        generated = out / "opencode_acp_isolated.py"
        assert generated.is_file()
        source = generated.read_text(encoding="utf-8")
        assert "_SYS_LANDLOCK_CREATE_RULESET = 444" in source
        assert "_SYS_LANDLOCK_RESTRICT_SELF = 446" in source
        assert "PR_SET_NO_NEW_PRIVS" in source
        assert '"HOME": str(home)' in source
        assert '"OPENAI_API_KEY"' not in source
        assert "session/request_permission" in source
        assert '"outcome": "cancelled"' in source
        assert "tool_attempted" in source
        assert 'os.execve(str(binary), [str(binary), "acp"], env)' in source


def test_acp_protocol_is_official_session_lifecycle() -> None:
    source = (RUNTIME / "opencode_acp_isolated.py").read_text(encoding="utf-8")
    for method in (
        '"initialize"',
        '"session/new"',
        '"session/set_config_option"',
        '"session/prompt"',
        '"session/cancel"',
    ):
        assert method in source
    assert '"mcpServers": []' in source
    assert '"configId": "model"' in source
