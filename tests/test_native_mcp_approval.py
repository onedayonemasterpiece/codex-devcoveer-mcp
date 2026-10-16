"""Codex DevCoveer write-task MCP approval regression tests.

No task dispatch, model inference, secrets or interactive approvals.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from native_mcp_approval import DEVCOVEER_APP_ID, native_devcoveer_app_config


def test_only_explicit_write_access_trusts_own_app():
    assert native_devcoveer_app_config("read") == {}
    assert native_devcoveer_app_config(None) == {}
    assert native_devcoveer_app_config("unknown") == {}
    assert native_devcoveer_app_config("write") == {
        "apps": {
            DEVCOVEER_APP_ID: {
                "default_tools_approval_mode": "approve",
            }
        }
    }
    assert DEVCOVEER_APP_ID.startswith("asdk_app_")
    assert "apps._default" not in json.dumps(native_devcoveer_app_config("write"))
    assert "danger-full-access" not in json.dumps(native_devcoveer_app_config("write"))


def _build(tmp_path: Path):
    out = tmp_path / "runtime"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build-clean-host-runtime.py"), str(out)],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr
    return out


def test_canonical_build_wires_both_native_start_and_resume(tmp_path: Path):
    out = _build(tmp_path)
    bridge = (out / "codex-chatgpt-bridge.py").read_text(encoding="utf-8")
    assert (out / "native_mcp_approval.py").exists()
    assert "native_devcoveer_app_config as _native_devcoveer_app_config" in bridge
    assert 'fixed_config: dict[str, Any] = _native_devcoveer_app_config(access)' in bridge
    assert 'fixed_config: dict[str, Any] = _native_devcoveer_app_config(metadata.get("access"))' in bridge
    # Preserve the task sandbox and no approval requests for unrelated native tools.
    assert '"approvalPolicy": "never"' in bridge
    assert "WRITE_PERMISSION_PROFILE" in bridge
    assert '"default_tools_approval_mode": "approve"' not in bridge
    assert 'destructive_hint=True' in bridge


def test_resume_params_keep_original_policy_and_scope_app_trust(tmp_path: Path, monkeypatch):
    out = _build(tmp_path)
    # Import the complete generated bridge with isolated host paths. Do not
    # require or create production directories on the CI runner.
    projects = tmp_path / "projects"
    projects.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.syspath_prepend(str(out))
    # These constructors bind production paths as defaults at import time.
    # Keep their real implementations, but isolate their no-argument stores.
    import opencode_backend
    import model_health
    monkeypatch.setattr(opencode_backend.TaskRegistry.__init__, "__defaults__",
                        (tmp_path / "tasks",))
    monkeypatch.setattr(model_health.ModelHealthStore.__init__, "__defaults__",
                        (tmp_path / "model-health",))
    original_resolve = Path.resolve

    def resolve_test_project_root(path, *args, **kwargs):
        if path == Path("/home/dev/projects"):
            return original_resolve(projects, *args, **kwargs)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_test_project_root)
    spec = importlib.util.spec_from_file_location(
        "native_mcp_policy_canary_bridge", out / "codex-chatgpt-bridge.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        metadata = {
            "cwd": "/home/dev/projects/street-story",
            "access": "write", "sandbox": "workspace-write",
            "permissions": "devcoveer-write",
            "model": "gpt-6.1-sol",
            "reasoningEffort": "high",
        }
        payload = module.NativeHistoryClient._resume_params(
            "01a13f72-3423-7170-95c5-bee77e6a870d", metadata
        )
        assert payload["config"]["apps"] == native_devcoveer_app_config("write")["apps"]
        assert payload["config"]["default_permissions"] == "devcoveer-write"
        assert payload["config"]["model_reasoning_effort"] == "high"
        assert payload["approvalPolicy"] == "never"
        assert payload["cwd"] == metadata["cwd"]
        assert payload["threadId"] == "01a13f72-3423-7170-95c5-bee77e6a870d"
        read = module.NativeHistoryClient._resume_params(
            "01a13f72-3423-7170-95c5-bee77e6a870d",
            {**metadata, "access": "read", "sandbox": "read-only", "permissions": None},
        )
        assert "apps" not in read.get("config", {})
        assert read["sandbox"] == "read-only"
    finally:
        sys.modules.pop(spec.name, None)
