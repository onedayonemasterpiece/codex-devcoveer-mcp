from __future__ import annotations

import hashlib
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "historical" / "3.6.1" / "source"
INSTALLER = ROOT / "scripts" / "install-current-canonical-runtime.sh"

EXPECTED = {
    "codex-chatgpt-bridge.py": "73756c81b852cb183943c880d5918faadeee25ea9b034504a80df9f5f05d2107",
    "opencode_backend.py": "56699a39b2d635cdc397774cc25f122b119e7794599b0d50776c7596e6c9af84",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_controlled_baseline_is_complete_and_pinned() -> None:
    for name, expected in EXPECTED.items():
        path = BASE / name
        assert path.is_file()
        assert sha256(path) == expected
    assert (BASE / "README.md").is_file()
    assert (BASE / "run-codex-mcp").is_file()


def test_clean_host_installer_does_not_use_bootstrap_as_source() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'SOURCE_REF="${DEVCOVEER_SOURCE_REF:-origin/main}"' in source
    assert 'git -C "$WORK_REPO" archive "$SOURCE_SHA"' in source
    assert 'cp -a "$BASE/." "$CAND"' in source
    assert 'cp -a "$BOOT" "$CAND"' not in source
    assert 'source_baseline_sha_gate=PASS' in source


def test_clean_host_installer_accepts_probes_before_tunnel_switch() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert '"project_probe", "host_probe"' in source
    assert "wrapper_required_tools=PASS" in source
    assert "systemctl --user restart codex-devserver-tunnel.service" in source


def test_clean_host_installer_runs_normally_as_dev_without_sudo() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'ERROR: run as root' not in source
    assert 'sudo ' not in source
    assert 'CURRENT_UID="$(id -u)"' in source
    assert 'if [ "$CURRENT_UID" -eq "$DEV_UID" ]; then' in source
    assert 'systemctl --user restart codex-devserver-tunnel.service' in source


def test_clean_host_installer_is_executable() -> None:
    mode = INSTALLER.stat().st_mode
    assert mode & stat.S_IXUSR
