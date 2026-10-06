from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time

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


def test_cancel_event_stops_capacity_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "SLOT_ROOT", tmp_path / "locks")
    keys = [("NVIDIA_KEY_A", "a"), ("NVIDIA_KEY_B", "b")]
    first = mod._try_acquire_slot(keys)
    second = mod._try_acquire_slot(keys)
    assert first is not None and second is not None
    cancelled = threading.Event()
    cancelled.set()
    started = time.monotonic()
    with pytest.raises(mod.NvidiaCouncilError) as exc:
        mod._acquire_slot(keys, timeout_seconds=10.0, cancel_event=cancelled)
    assert exc.value.category == "cancelled"
    assert time.monotonic() - started < 0.5
    mod._release_slot(first[0], first[1])
    mod._release_slot(second[0], second[1])


def test_provider_deadline_bounds_transient_retry_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "SLOT_ROOT", tmp_path / "locks")
    monkeypatch.setattr(
        mod,
        "_candidate_keys",
        lambda *_args, **_kwargs: [
            ("NVIDIA_KEY_A", "a"),
            ("NVIDIA_KEY_B", "b"),
        ],
    )

    calls = {"count": 0}

    def fail_fast(**_kwargs):
        calls["count"] += 1
        raise mod.NvidiaCouncilError("provider_unavailable", "temporary")

    monkeypatch.setattr(mod, "_invoke_once", fail_fast)
    started = time.monotonic()
    with pytest.raises(mod.NvidiaCouncilError) as exc:
        mod._invoke_blocking(
            model="moonshotai/kimi-k3",
            prompt="bounded retry",
            capacity_timeout_seconds=1.0,
            provider_deadline_seconds=0.05,
            cancel_event=None,
        )
    assert exc.value.category == "timeout"
    assert calls["count"] == 1
    assert time.monotonic() - started < 1.0


def test_task_cancel_registry_is_idempotent_and_releasable() -> None:
    task_id = "dvt_" + "c" * 32
    mod.release_task(task_id)
    event = mod.task_cancel_event(task_id)
    assert event.is_set() is False
    assert mod.cancel_task(task_id) is True
    assert event.is_set() is True
    assert mod.cancel_task(task_id) is False
    mod.release_task(task_id)
    replacement = mod.task_cancel_event(task_id)
    assert replacement is not event
    assert replacement.is_set() is False
    mod.release_task(task_id)


def test_provider_deadline_defaults_are_bounded() -> None:
    assert mod.DEFAULT_PROVIDER_DEADLINE_SECONDS == 360.0
    assert mod.DEFAULT_CAPACITY_TIMEOUT_SECONDS == 900.0
    assert mod.SOCKET_STALL_TIMEOUT_SECONDS == 180.0


def test_deepseek_uses_bounded_non_stream_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": "deepseek ok"}}
                    ],
                    "usage": {"completion_tokens": 7},
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["accept"] = request.get_header("Accept")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    text, usage = mod._invoke_once(
        key="secret",
        model="deepseek-ai/deepseek-v4.1-flash",
        prompt="bounded council stage",
        deadline_monotonic=time.monotonic() + 30.0,
    )
    assert text == "deepseek ok"
    assert usage == {"completion_tokens": 7}
    assert captured["body"]["stream"] is False
    assert captured["body"]["max_tokens"] == 4096
    assert captured["body"]["temperature"] == 1.0
    assert captured["body"]["top_p"] == 0.95
    assert captured["accept"] == "application/json"
    assert 0 < captured["timeout"] <= mod.SOCKET_STALL_TIMEOUT_SECONDS


def test_kimi_keeps_streaming_profile() -> None:
    profile = mod.MODEL_PROFILES["moonshotai/kimi-k3"]
    assert profile["max_tokens"] == 16384
    assert profile.get("stream", True) is True
    assert profile["reasoning_effort"] == "max"
