from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))

import direct_actions
import direct_probe_core


def _kinds(rows):
    return {
        row.get("properties", {}).get("kind", {}).get("const")
        for row in rows
    }


def test_devcoveer_has_no_android_device_control_surface() -> None:
    project_actions = _kinds(direct_actions._PROJECT_ACTION_SCHEMA["oneOf"])
    host_probes = _kinds(direct_probe_core.HOST_CHECK_SCHEMAS)

    assert "adb" not in project_actions
    assert not any(isinstance(kind, str) and kind.startswith("adb") for kind in host_probes)
    assert not (RUNTIME / "direct_ops2_android.py").exists()

    capabilities_source = (RUNTIME / "direct_ops2.py").read_text(encoding="utf-8")
    assert '"adb"' not in capabilities_source

    installer = (ROOT / "scripts" / "install-current-canonical-runtime.sh").read_text(encoding="utf-8")
    assert '"adb"' not in installer

    docs = (ROOT / "docs" / "direct-ops-2.md").read_text(encoding="utf-8")
    assert "adb_devices" not in docs
    assert "package logcat" in docs
    assert "local workstation" in docs
