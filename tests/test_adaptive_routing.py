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


def test_controlled_cancel_is_neutral_for_model_health(tmp_path: Path):
    store = ModelHealthStore(tmp_path / "health")
    now = 25_000_000
    selection = "nvidia/moonshotai/kimi-k3"
    store.record(
        provider="nvidia", selection=selection, outcome="success", category="ok",
        observed_at=now - 2_000, latency_ms=9_000, event_key="success",
    )
    store.record(
        provider="nvidia", selection=selection, outcome="cancelled", category="cancelled",
        observed_at=now, event_key="cancelled",
    )
    summary = store.summary(selection, now_ms=now + 1)
    assert summary["currentRuntimeStatus"] == "available_recent_success"
    assert summary["windows"]["1h"]["attempts"] == 1
    assert summary["windows"]["1h"]["successRate"] == 1.0
    assert store.provider_summary("nvidia", now_ms=now + 1)["currentRuntimeStatus"] == "available_recent_success"


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


def test_resource_order_is_zen_then_groq_then_nvidia_then_metered():
    assert resource_class("opencode") == "renewable_free_burst"
    assert resource_class("groq") == "renewable_key_pool"
    assert resource_class("groq3") == "renewable_key_pool"
    assert resource_class("nvidia") == "elastic_nonscarce"
    assert resource_class("giga1") == "metered_reserve"
    assert resource_priority("opencode") < resource_priority("groq") < resource_priority("nvidia") < resource_priority("giga1")


def test_bridge_fragments_are_parseable_and_enforce_runtime_routing_contract():
    discovery = (RUNTIME / "bridge_fragments" / "model_discovery_adaptive.py").read_text(encoding="utf-8")
    read_task = (RUNTIME / "bridge_fragments" / "read_opencode_task_adaptive.py").read_text(encoding="utf-8")
    routing = (RUNTIME / "opencode_routing.py").read_text(encoding="utf-8")
    ast.parse(discovery)
    ast.parse(read_task)
    ast.parse(routing)

    for field in (
        "currentRuntimeStatus",
        "resourceClass",
        "resourcePriority",
        "activeTasks",
        "activeSessions",
        "routingRecommended",
        "agentEligible",
        "agentTaskReady",
    ):
        assert field in discovery
    assert 'current_runtime in {"eol", "temporarily_unavailable", "probe_due", "degraded"}' in discovery
    assert "0 if recommended else 1" in discovery
    assert 'item.get("routingRecommended") is True' in discovery
    assert '"genericAgentFilter": "toolcall_and_text_io"' in discovery
    assert 'provider_id.startswith("groq")' in discovery
    assert '{"groq": 0, "groq2": 1, "groq3": 2}' in discovery
    assert '"policy": "zen_then_groq_then_nvidia_then_metered"' in discovery
    assert '"automaticFallback": True' in discovery
    assert '"automaticFallbackScope": "ordered_providers_for_automatic_tasks"' in discovery
    assert '"groqKeyOrder": ["groq", "groq2", "groq3"]' in discovery
    assert "Do not ask the user to restate the task." in read_task

    for field in (
        "providerRuntimeStatus",
        "providerErrorCategory",
        "lastProgressAt",
        "lastToolCallAt",
        "stallForMs",
        "stallSuspected",
        "safeLiveSnapshot",
        "suggestedAction",
        "durableTaskStatus",
        "inspectionStatus",
    ):
        assert field in read_task
    assert "transient_inspection_errors" in read_task
    assert '"retry_read_before_cancel"' in read_task
    assert "The durable task state was not changed" in read_task
    assert "for msg in reversed(messages):" in read_task
    assert "latest assistant turn" in read_task
    assert "elif info.get('time', {}).get('completed'): result['status'] = 'completed'" in routing
    assert "info.get('time', {}).get('completed') and text" not in routing


def test_backend_patches_are_bounded_and_include_runtime_observability():
    patch1 = ROOT / "patches" / "opencode_backend_adaptive.patch.1"
    patch2 = ROOT / "patches" / "opencode_backend_adaptive.patch.2"
    cancel_patch = ROOT / "patches" / "opencode_backend_cancel_neutral.patch"
    combined = patch1.read_text(encoding="utf-8") + patch2.read_text(encoding="utf-8")
    assert _sha(patch1) == "e325dfa1e8575cd42f25a84d7b535332c2207da349b404db9bfffe3eb20e2829"
    assert _sha(cancel_patch) == "4b4f3ed187d084860613a0232afb1e33c6a01c60f706e46be73c7d31c4be07c5"
    # The Sep-10 live runtime already contains the WRITE-policy cleanup. The
    # rebased patch must not try to rewrite that unrelated hunk a second time.
    assert "@@ -343,8 +527,9 @@" not in patch2.read_text(encoding="utf-8")
    for needle in (
        "ModelHealthStore",
        "providerRuntimeStatus",
        "quota_exhausted",
        "rate_limited",
        "safeLiveSnapshot",
        "stallSuspected",
    ):
        assert needle in combined
    cancel_text = cancel_patch.read_text(encoding="utf-8")
    assert 'category, retryable, runtime_status, suggested = "cancelled"' in cancel_text
    assert 'event_outcome = "cancelled"' in cancel_text


def test_installer_bootstrap_is_fail_closed_and_does_not_restart_services():
    path = ROOT / "scripts" / "install-adaptive-routing-v3.10.py"
    source = path.read_text(encoding="utf-8")
    ast.parse(source)
    assert 'EXPECTED_BACKEND_SHA256 = "0b769ac4aae681345d6857b0322c8dddb76a499fbb86986bddc39b7ace68e902"' in source
    assert 'EXPECTED_BRIDGE_SOURCE_SHA256 = "2006a60b5bdbbed9b6892ce9c0308512a5fc31dceb9fa197726d74157aac57ea"' in source
    assert 'EXPECTED_READ_TASK_SHA256 = "ccc473d2c50c519ce979c1cee7a3235f4a7ca59890cb6e67abdfa69566f00275"' in source
    assert 'EXPECTED_PATCH2_SHA256: str | None = "69c782babbc234caa6aff16fcd8982e5e195c2b2d03a51c94d4fe7a717eb3918"' in source
    assert 'EXPECTED_ROUTING_BACKEND_SHA256: str | None = "7313917544890dac03934fb0da53047012d300faafbfd5a8445b3122bfe112a2"' in source
    assert 'EXPECTED_NEW_BACKEND_SHA256: str | None = "479f6707669011f99a008a9432de83b82d0bda70e6b0cb23473662cae6892f03"' in source
    assert 'EXPECTED_NEW_BRIDGE_SHA256: str | None = "8bfbbb7003e916bffd6cf4b37b1705295ecf8fd428c0ada3523ed2a19d17c12c"' in source
    assert "candidate hashes are not pinned" in source
    assert "live or unverified DevCoveer tasks exist; inspect them before installation/restart" in source
    assert "staleRegistryTasks" in source
    assert "maintenanceTaskExcluded" in source
    assert "session_status_unavailable" in source
    assert "systemctl" not in source
    assert "restartRequired" in source

    spec = importlib.util.spec_from_file_location("adaptive_installer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.NEW_VERSION == 'BRIDGE_VERSION = "3.10.0"'
