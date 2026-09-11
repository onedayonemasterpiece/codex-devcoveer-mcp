from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from model_health import ModelHealthStore, resource_class, resource_priority  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_model_health_windows_and_cooldown(tmp_path: Path):
    store = ModelHealthStore(tmp_path / "health")
    now = 10_000_000
    kimi = "nvidia/moonshotai/kimi-k3"
    sibling = "nvidia/deepseek-ai/deepseek-v4-flash"
    assert store.record(
        provider="nvidia",
        selection=kimi,
        outcome="retry",
        category="rate_limited",
        observed_at=now - 1_000,
        latency_ms=25_000,
        retryable=True,
        status_code=429,
        event_key="k1",
    )
    summary = store.summary(kimi, now_ms=now)
    assert summary["currentRuntimeStatus"] == "temporarily_unavailable"
    assert summary["windows"]["1h"]["rateLimitCount"] == 1
    assert summary["windows"]["1h"]["p50LatencyMs"] == 25_000
    assert store.summary(sibling, now_ms=now)["currentRuntimeStatus"] == "unknown"

    store.record(
        provider="nvidia",
        selection=kimi,
        outcome="success",
        category="ok",
        observed_at=now + 2_000,
        latency_ms=8_000,
        event_key="k2",
    )
    summary = store.summary(kimi, now_ms=now + 2_001)
    assert summary["currentRuntimeStatus"] == "available_recent_success"
    assert summary["windows"]["1h"]["successes"] == 1


def test_zen_quota_is_provider_wide_but_nvidia_failure_is_model_local(tmp_path: Path):
    store = ModelHealthStore(tmp_path / "health")
    now = 20_000_000
    store.record(
        provider="opencode",
        selection="opencode/mimo-v2.5-free",
        outcome="retry",
        category="quota_exhausted",
        observed_at=now,
        retryable=True,
        event_key="zen-q",
    )
    assert store.provider_summary("opencode", now_ms=now + 1)["currentRuntimeStatus"] == "temporarily_unavailable"

    bad = "nvidia/nvidia/nemotron-3-nano-30b-a3b"
    sibling = "nvidia/nvidia/nemotron-3-super-120b-a12b"
    store.record(
        provider="nvidia",
        selection=bad,
        outcome="failure",
        category="eol",
        observed_at=now,
        retryable=False,
        status_code=410,
        event_key="nv-eol",
    )
    assert store.summary(bad, now_ms=now + 1)["currentRuntimeStatus"] == "eol"
    assert store.summary(sibling, now_ms=now + 1)["currentRuntimeStatus"] == "unknown"
    assert store.provider_summary("nvidia", now_ms=now + 1)["currentRuntimeStatus"] == "degraded_model_only"


def test_health_store_is_content_free_and_bounded(tmp_path: Path):
    store = ModelHealthStore(tmp_path / "health")
    store.record(
        provider="opencode",
        selection="opencode/mimo-v2.5-free",
        outcome="retry",
        category="quota_exhausted",
        observed_at=30_000_000,
        event_key="safe-event",
    )
    raw = (tmp_path / "health" / "events.json").read_text(encoding="utf-8")
    assert "prompt" not in raw.lower()
    assert "response" not in raw.lower()
    payload = json.loads(raw)
    assert payload["schema"] == "devcoveer-model-health.v1"
    assert set(payload["events"][0]) <= {
        "provider",
        "selection",
        "outcome",
        "category",
        "observedAt",
        "latencyMs",
        "retryable",
        "statusCode",
        "eventKey",
    }


def test_resource_order_is_zen_then_nvidia_then_metered():
    assert resource_class("opencode") == "renewable_free_burst"
    assert resource_class("nvidia") == "elastic_nonscarce"
    assert resource_class("giga1") == "metered_reserve"
    assert resource_priority("opencode") < resource_priority("nvidia") < resource_priority("giga1")


def test_bridge_fragments_are_parseable_and_expose_live_diagnostics():
    discovery = (RUNTIME / "bridge_fragments" / "model_discovery_adaptive.py").read_text(encoding="utf-8")
    read_task = (RUNTIME / "bridge_fragments" / "read_opencode_task_adaptive.py").read_text(encoding="utf-8")
    ast.parse(discovery)
    ast.parse(read_task)

    for field in (
        "currentRuntimeStatus",
        "resourceClass",
        "resourcePriority",
        "activeTasks",
        "activeSessions",
        "routingRecommended",
    ):
        assert field in discovery
    for field in (
        "providerRuntimeStatus",
        "providerErrorCategory",
        "lastProgressAt",
        "lastToolCallAt",
        "stallForMs",
        "stallSuspected",
        "safeLiveSnapshot",
        "suggestedAction",
    ):
        assert field in read_task


def test_backend_patch_contains_runtime_observability_not_silent_fallback():
    patch1 = ROOT / "patches" / "opencode_backend_adaptive.patch.1"
    patch2 = ROOT / "patches" / "opencode_backend_adaptive.patch.2"
    combined = patch1.read_text(encoding="utf-8") + patch2.read_text(encoding="utf-8")
    assert _sha(patch1) == "e325dfa1e8575cd42f25a84d7b535332c2207da349b404db9bfffe3eb20e2829"
    assert _sha(patch2) == "80a79f06223c99be2c0b32087cf46037c0a384db2ba269fc3c9712bfe0a8908a"
    for needle in (
        "ModelHealthStore",
        "providerRuntimeStatus",
        "quota_exhausted",
        "rate_limited",
        "safeLiveSnapshot",
        "stallSuspected",
    ):
        assert needle in combined


def test_installer_is_fail_closed_and_does_not_restart_services():
    path = ROOT / "scripts" / "install-adaptive-routing-v3.10.py"
    source = path.read_text(encoding="utf-8")
    ast.parse(source)
    assert 'EXPECTED_BACKEND_SHA256 = "c4a152e5c6b44e0e6ca1899aec4ad221b1ec38b5f8578827d67d55f4dc007864"' in source
    assert 'EXPECTED_NEW_BACKEND_SHA256 = "2243d37d89605817db9cd8c19179ac8769851fdf688e2d201daca5359c10c63d"' in source
    assert "active DevCoveer tasks exist; inspect them before installation/restart" in source
    assert "systemctl" not in source
    assert "restartRequired" in source

    spec = importlib.util.spec_from_file_location("adaptive_installer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.NEW_VERSION == 'BRIDGE_VERSION = "3.10.0"'
