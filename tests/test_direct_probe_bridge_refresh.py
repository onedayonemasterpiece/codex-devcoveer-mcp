from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "refresh-direct-probe-bridge.py"


def load_refresh():
    spec = importlib.util.spec_from_file_location("refresh_direct_probe_bridge", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_installer(tmp_path: Path, baseline: bytes):
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def safe_bytes(path: Path, *, max_size: int = 3_000_000) -> bytes:
        assert path.is_file() and not path.is_symlink()
        data = path.read_bytes()
        assert len(data) <= max_size
        return data

    def transform(source: str) -> str:
        return source + "\n# desired direct-probe bridge refresh\n"

    return SimpleNamespace(
        INSTALL_DIR=tmp_path,
        EXPECTED_BRIDGE_SHA256=digest(baseline),
        safe_bytes=safe_bytes,
        sha256=digest,
        transform_bridge=transform,
    )


def test_refresh_builds_from_exact_trusted_baseline(monkeypatch, tmp_path):
    mod = load_refresh()
    baseline = b"trusted baseline bridge\n"
    deployed = b"known deployed bridge\n"
    installer = fake_installer(tmp_path, baseline)
    backup = tmp_path / "codex-chatgpt-bridge.py.bak.direct-probes-20260912T200000Z"
    backup.write_bytes(baseline)
    monkeypatch.setattr(mod, "EXPECTED_DEPLOYED_BRIDGE_SHA256", mod.sha256(deployed))

    candidate, used = mod.build_candidate(installer, deployed)

    assert used == backup
    assert candidate == installer.transform_bridge(baseline.decode("utf-8")).encode("utf-8")


def test_refresh_is_idempotent_for_exact_desired_candidate(monkeypatch, tmp_path):
    mod = load_refresh()
    baseline = b"trusted baseline bridge\n"
    installer = fake_installer(tmp_path, baseline)
    (tmp_path / "codex-chatgpt-bridge.py.bak.direct-probes-20260912T200000Z").write_bytes(baseline)
    desired = installer.transform_bridge(baseline.decode("utf-8")).encode("utf-8")
    monkeypatch.setattr(mod, "EXPECTED_DEPLOYED_BRIDGE_SHA256", "f" * 64)

    candidate, _used = mod.build_candidate(installer, desired)

    assert candidate == desired


def test_refresh_rejects_unknown_runtime_lineage(monkeypatch, tmp_path):
    mod = load_refresh()
    baseline = b"trusted baseline bridge\n"
    installer = fake_installer(tmp_path, baseline)
    (tmp_path / "codex-chatgpt-bridge.py.bak.direct-probes-20260912T200000Z").write_bytes(baseline)
    monkeypatch.setattr(mod, "EXPECTED_DEPLOYED_BRIDGE_SHA256", "e" * 64)

    try:
        mod.build_candidate(installer, b"unexpected installed bridge\n")
    except SystemExit as exc:
        assert "accepted deployed bridge" in str(exc)
    else:
        raise AssertionError("unknown runtime lineage must fail closed")


def test_refresh_requires_trusted_baseline_backup(monkeypatch, tmp_path):
    mod = load_refresh()
    baseline = b"trusted baseline bridge\n"
    deployed = b"known deployed bridge\n"
    installer = fake_installer(tmp_path, baseline)
    monkeypatch.setattr(mod, "EXPECTED_DEPLOYED_BRIDGE_SHA256", mod.sha256(deployed))

    try:
        mod.build_candidate(installer, deployed)
    except SystemExit as exc:
        assert "trusted pre-direct-probe bridge backup is unavailable" in str(exc)
    else:
        raise AssertionError("refresh without trusted baseline must fail closed")


def test_refresh_script_never_restarts_services():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "systemctl" not in text
    assert "opencode-devcoveer.service" not in text
    assert "--maintenance-task-id" in text
    assert "maintenance_gate" in text
    assert 'EXPECTED_DEPLOYED_BRIDGE_SHA256 = "4de99e63f1c089103a0bd0a4efea6396c1279690109d98fb1232cb218a137ab8"' in text
