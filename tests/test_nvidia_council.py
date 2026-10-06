from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "runtime" / "nvidia_council.py"
spec = importlib.util.spec_from_file_location("nvidia_council", MODULE)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def _env(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_two_distinct_nvidia_credentials_are_detected_without_exposure(tmp_path: Path) -> None:
    path = _env(
        tmp_path / "providers.env",
        "NVIDIA_API_KEY_ALPHA=alpha-secret-value\n"
        "NVIDIA_API_KEY_BETA=beta-secret-value\n"
        "OTHER_KEY=must-ignore\n",
    )
    meta = mod.slot_metadata(path)
    assert meta == {
        "configured": True,
        "slotCount": 2,
        "requiredSlots": 2,
        "sourceNames": [
            "providers.env:NVIDIA_API_KEY_ALPHA",
            "providers.env:NVIDIA_API_KEY_BETA",
        ],
        "configuredSourceFiles": ["providers.env"],
        "credentialValuesExposed": False,
    }
    assert "secret" not in repr(meta)


def test_two_project_env_files_create_two_distinct_slots(tmp_path: Path) -> None:
    first = _env(
        tmp_path / "devcoveer-providers.env",
        "NVIDIA_API_KEY=alpha-secret-value\n",
    )
    second = _env(
        tmp_path / "regional-service.env",
        "NVIDIA_API_KEY=beta-secret-value\n",
    )
    meta = mod.slot_metadata((first, second))
    assert meta["configured"] is True
    assert meta["slotCount"] == 2
    assert meta["sourceNames"] == [
        "devcoveer-providers.env:NVIDIA_API_KEY",
        "regional-service.env:NVIDIA_API_KEY",
    ]
    assert meta["configuredSourceFiles"] == [
        "devcoveer-providers.env",
        "regional-service.env",
    ]


def test_missing_optional_source_is_fail_closed_not_exception(tmp_path: Path) -> None:
    first = _env(
        tmp_path / "devcoveer-providers.env",
        "NVIDIA_API_KEY=alpha-secret-value\n",
    )
    missing = tmp_path / "missing-regional.env"
    meta = mod.slot_metadata((first, missing))
    assert meta["configured"] is False
    assert meta["slotCount"] == 1
    assert meta["configuredSourceFiles"] == ["devcoveer-providers.env"]


def test_duplicate_nvidia_key_does_not_create_second_project_slot(tmp_path: Path) -> None:
    first = _env(
        tmp_path / "devcoveer-providers.env",
        "NVIDIA_API_KEY=same-secret\n",
    )
    second = _env(
        tmp_path / "regional-service.env",
        "NVIDIA_API_KEY=same-secret\n",
    )
    meta = mod.slot_metadata((first, second))
    assert meta["configured"] is False
    assert meta["slotCount"] == 1


def test_provider_env_must_be_private_regular_file(tmp_path: Path) -> None:
    path = _env(tmp_path / "providers.env", "NVIDIA_API_KEY_ALPHA=a\n")
    os.chmod(path, 0o644)
    with pytest.raises(mod.NvidiaCouncilError) as exc:
        mod.slot_metadata(path)
    assert exc.value.category == "configuration"


def test_two_slot_lock_admits_two_and_rejects_third_until_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "SLOT_ROOT", tmp_path / "locks")
    keys = [("NVIDIA_KEY_A", "a"), ("NVIDIA_KEY_B", "b")]
    first = mod._try_acquire_slot(keys)
    second = mod._try_acquire_slot(keys)
    third = mod._try_acquire_slot(keys)
    assert first is not None and second is not None
    assert {first[0], second[0]} == {0, 1}
    assert third is None
    mod._release_slot(first[0], first[1])
    again = mod._try_acquire_slot(keys)
    assert again is not None
    mod._release_slot(again[0], again[1])
    mod._release_slot(second[0], second[1])


def test_direct_council_models_are_exact_product_pair() -> None:
    assert set(mod.MODEL_PROFILES) == {
        "moonshotai/kimi-k3",
        "deepseek-ai/deepseek-v4.1-flash",
    }
    assert mod.MAX_SLOTS == 2
    assert mod.NVIDIA_ENDPOINT == "https://integrate.api.nvidia.com/v1/chat/completions"
