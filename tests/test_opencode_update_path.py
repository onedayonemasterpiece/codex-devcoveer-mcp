from __future__ import annotations

import stat
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "update-opencode-runtime.sh"
BOOTSTRAP = ROOT / "scripts" / "bootstrap-devcoveer2-control-plane.sh"


def test_updater_is_repeatable_user_scoped_and_fail_safe() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    assert UPDATER.stat().st_mode & stat.S_IXUSR
    assert 'TARGET="${1:-latest}"' in source
    assert '"$BIN" upgrade --method curl' in source
    assert '"$BIN" upgrade "$TARGET" --method curl' in source
    assert "sudo " not in source
    assert "rollback()" in source
    assert 'systemctl --user restart "$UNIT"' in source
    assert "/global/health" in source


def test_recovery_bootstrap_does_not_pin_operational_opencode_version() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "OPENCODE_VERSION=" not in source
    assert "OpenCode health: PASS; version=" in source
