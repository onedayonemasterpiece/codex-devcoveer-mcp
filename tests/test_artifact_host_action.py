from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
import direct_actions as actions
from direct_probe_core import ProbeError


def run(value):
    return asyncio.run(value)


def test_direct_host_schema_advertises_bounded_artifact_operator():
    variants = actions.HOST_ACTION_INPUT_SCHEMA["properties"]["action"]["oneOf"]
    by_kind = {x["properties"]["kind"]["const"]: x for x in variants}
    assert "artifact_maintenance" in by_kind
    item = by_kind["artifact_maintenance"]
    assert set(item["properties"]["mode"]["enum"]) == {"list", "inspect", "delete", "scratch_cleanup"}
    assert "project" not in item["properties"]
    assert "run" in item["properties"]
    assert "expected_kib" in item["properties"]


def test_host_operator_never_accepts_arbitrary_project_or_missing_size(tmp_path, monkeypatch):
    with pytest.raises(ProbeError, match="requires the registered"):
        run(actions._host_artifact_maintenance(
            "street-story", {"artifact_project": "street-story", "mode": "list"}))
    with pytest.raises(ProbeError, match="positive expected_kib"):
        run(actions._host_artifact_maintenance(
            "codex-devcoveer-mcp", {
                "artifact_project": "street-story", "mode": "delete", "run": "a"
            }))
    with pytest.raises(ProbeError, match="accepts no run"):
        run(actions._host_artifact_maintenance(
            "codex-devcoveer-mcp", {
                "artifact_project": "street-story", "mode": "list", "run": "a"
            }))


def test_host_operator_invokes_only_bundled_script_and_exact_argv(tmp_path, monkeypatch):
    script = tmp_path / "maintain-artifacts.py"
    script.write_text("print('fixture')\n")
    module_path = tmp_path / "direct_actions.py"
    module_path.write_text("# temporary test location\\n")
    monkeypatch.setattr(actions, "__file__", str(module_path))
    calls = []

    async def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            exit_code=0, stdout=json.dumps({"safe_to_delete": False}).encode(),
            stderr=b"", duration_ms=1, truncated=False,
        )

    monkeypatch.setattr(actions, "_run_process", fake_run)
    result = run(actions._host_artifact_maintenance(
        "codex-devcoveer-mcp",
        {"artifact_project": "street-story", "mode": "inspect",
         "run": "20261001T080000Z-test"},
    ))
    assert result["status"] == "ok"
    argv, params = calls.pop()
    assert argv == [
        "/usr/bin/python3", str(script), "--project", "street-story",
        "--run", "20261001T080000Z-test",
    ]
    assert params["cwd"] == tmp_path

    run(actions._host_artifact_maintenance(
        "codex-devcoveer-mcp",
        {"artifact_project": "street-story", "mode": "delete",
         "run": "20261001T080000Z-test", "expected_kib": 123},
    ))
    argv, _ = calls.pop()
    assert argv[-3:] == ["--delete", "--expected-kib", "123"]


def test_canonical_builder_bundles_host_maintenance_script(tmp_path):
    built = tmp_path / "runtime"
    builder = ROOT / "scripts" / "build-clean-host-runtime.py"
    result = subprocess.run(
        [sys.executable, str(builder), str(built)],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr
    bundled = built / "maintain-artifacts.py"
    assert bundled.read_bytes() == (ROOT / "scripts" / "maintain-artifacts.py").read_bytes()
    direct = (built / "direct_actions.py").read_text()
    assert 'kind == "artifact_maintenance"' in direct
    assert '"artifact_maintenance"' in direct

