from __future__ import annotations

import json
from pathlib import Path

import pytest

from model_health import ModelHealthStore
from opencode_backend_adaptive import OpenCodeBackend


def test_model_health_windows_and_cooldown(tmp_path: Path):
    store = ModelHealthStore(tmp_path / "health")
    now = 10_000_000
    kimi = "nvidia/moonshotai/kimi-k3"
    deepseek = "nvidia/deepseek-ai/deepseek-v4-flash"
    assert store.record(provider="nvidia", selection=kimi, outcome="retry",
                        category="rate_limited", observed_at=now - 1_000,
                        latency_ms=25_000, retryable=True, status_code=429,
                        event_key="k1")
    summary = store.summary(kimi, now_ms=now)
    assert summary["currentRuntimeStatus"] == "temporarily_unavailable"
    assert summary["windows"]["1h"]["rateLimitCount"] == 1
    assert summary["windows"]["1h"]["p50LatencyMs"] == 25_000
    assert store.summary(deepseek, now_ms=now)["currentRuntimeStatus"] == "unknown"

    store.record(provider="nvidia", selection=kimi, outcome="success", category="ok",
                 observed_at=now + 2_000, latency_ms=8_000, event_key="k2")
    summary = store.summary(kimi, now_ms=now + 2_001)
    assert summary["currentRuntimeStatus"] == "available_recent_success"
    assert summary["windows"]["1h"]["successes"] == 1


def test_model_health_eol_is_exact_model_only(tmp_path: Path):
    store = ModelHealthStore(tmp_path / "health")
    now = 20_000_000
    bad = "nvidia/nvidia/nemotron-3-nano-30b-a3b"
    sibling = "nvidia/nvidia/nemotron-3-super-120b-a12b"
    store.record(provider="nvidia", selection=bad, outcome="failure", category="eol",
                 observed_at=now, retryable=False, status_code=410, event_key="e1")
    assert store.summary(bad, now_ms=now + 1)["currentRuntimeStatus"] == "eol"
    assert store.summary(sibling, now_ms=now + 1)["currentRuntimeStatus"] == "unknown"


def test_zen_quota_is_provider_wide_until_later_success(tmp_path: Path):
    store = ModelHealthStore(tmp_path / "health")
    now = 25_000_000
    store.record(provider="opencode", selection="opencode/mimo-v2.5-free", outcome="retry",
                 category="quota_exhausted", observed_at=now, retryable=True, event_key="q1")
    assert store.provider_summary("opencode", now_ms=now + 1)["currentRuntimeStatus"] == "temporarily_unavailable"
    store.record(provider="opencode", selection="opencode/big-pickle", outcome="success",
                 category="ok", observed_at=now + 2_000, event_key="q2")
    assert store.provider_summary("opencode", now_ms=now + 2_001)["currentRuntimeStatus"] == "available_recent_success"


def test_health_store_contains_no_content_payloads(tmp_path: Path):
    store = ModelHealthStore(tmp_path / "health")
    store.record(provider="opencode", selection="opencode/mimo-v2.5-free",
                 outcome="retry", category="quota_exhausted", observed_at=30_000_000,
                 event_key="safe-event")
    raw = (tmp_path / "health" / "events.json").read_text()
    assert "prompt" not in raw.lower()
    assert "response" not in raw.lower()
    payload = json.loads(raw)
    assert set(payload["events"][0]) <= {
        "provider", "selection", "outcome", "category", "observedAt", "latencyMs",
        "retryable", "statusCode", "eventKey",
    }


class FakeBackend(OpenCodeBackend):
    def __init__(self, responses, health_store):
        super().__init__(health_store=health_store)
        self.responses = responses

    async def request(self, method, path, *, directory=None, payload=None, timeout=30.0):
        key = (method, path)
        value = self.responses[key]
        return value() if callable(value) else value


@pytest.mark.asyncio
async def test_retry_quota_is_visible_before_cancel(tmp_path: Path):
    store = ModelHealthStore(tmp_path / "health")
    session_id = "ses_abcdef1234"
    nowish = 40_000_000
    responses = {
        ("GET", "/session/status"): {
            session_id: {"type": "retry", "attempt": 3,
                         "message": "Free usage exceeded token=abcdefghijklmnopqrstuv", "next": nowish + 60_000}
        },
        ("GET", f"/session/{session_id}"): {"time": {"created": nowish - 50_000, "updated": nowish - 5_000}},
        ("GET", f"/session/{session_id}/message?limit=100"): [
            {"info": {"id": "msg_a", "role": "assistant", "providerID": "opencode",
                      "modelID": "mimo-v2.5-free", "time": {"created": nowish - 40_000},
                      "error": {"name": "ProviderError", "message": "Free usage exceeded"}},
             "parts": []}
        ],
    }
    backend = FakeBackend(responses, store)
    result = await backend.inspect(
        session_id=session_id, directory="/tmp", include_messages=True,
        provider_hint="opencode", model_hint="mimo-v2.5-free",
        selection_hint="opencode/mimo-v2.5-free",
    )
    assert result["status"] == "running"
    assert result["sessionStatus"] == "retry"
    assert result["providerRuntimeStatus"] == "temporarily_unavailable"
    assert result["providerErrorCategory"] == "quota_exhausted"
    assert result["retryable"] is True
    assert result["suggestedAction"] == "switch_provider"
    assert result["attempt"] == 3
    assert result["next"] == nowish + 60_000
    assert "abcdefghijklmnopqrstuv" not in str(result["providerState"])
    assert isinstance(result["safeLiveSnapshot"], list)
    health = store.summary("opencode/mimo-v2.5-free")
    assert health["windows"]["5h"]["quotaExhaustedCount"] >= 1


@pytest.mark.asyncio
async def test_busy_no_progress_is_only_suspected_not_terminal(tmp_path: Path, monkeypatch):
    import opencode_backend_adaptive as mod
    store = ModelHealthStore(tmp_path / "health")
    session_id = "ses_abcdef9999"
    observed = 100_000_000
    monkeypatch.setattr(mod, "_now_ms", lambda: observed)
    responses = {
        ("GET", "/session/status"): {session_id: {"type": "busy"}},
        ("GET", f"/session/{session_id}"): {"time": {"created": observed - 900_000, "updated": observed - 700_000}},
        ("GET", f"/session/{session_id}/message?limit=100"): [
            {"info": {"id": "msg_b", "role": "assistant", "providerID": "nvidia",
                      "modelID": "moonshotai/kimi-k3", "time": {"created": observed - 700_000}},
             "parts": []}
        ],
    }
    result = await FakeBackend(responses, store).inspect(
        session_id=session_id, directory="/tmp", provider_hint="nvidia",
        model_hint="moonshotai/kimi-k3", selection_hint="nvidia/moonshotai/kimi-k3",
    )
    assert result["status"] == "running"
    assert result["providerRuntimeStatus"] == "active"
    assert result["stallSuspected"] is True
    assert result["stallForMs"] >= result["stallThresholdMs"]
    assert result["providerErrorCategory"] is None


@pytest.mark.asyncio
async def test_catalog_exposes_modalities_limits_and_runtime_health(tmp_path: Path):
    store = ModelHealthStore(tmp_path / "health")
    selection = "nvidia/moonshotai/kimi-k3"
    store.record(provider="nvidia", selection=selection, outcome="retry",
                 category="rate_limited", status_code=429, retryable=True,
                 event_key="rate")
    providers = {
        "providers": [{"id": "nvidia", "models": {
            "moonshotai/kimi-k3": {
                "name": "Kimi K3", "status": "active",
                "capabilities": {"reasoning": True, "toolcall": True, "attachment": True,
                                 "input": {"text": True, "image": True, "video": True, "audio": False},
                                 "output": {"text": True, "image": False}},
                "limit": {"context": 1048576, "output": 131072},
                "cost": {"input": 0, "output": 0}, "variants": {},
            }
        }}]
    }
    backend = FakeBackend({("GET", "/config/providers"): providers}, store)
    catalog = await backend.catalog()
    item = catalog["models"][0]
    assert item["capabilities"]["attachment"] is True
    assert item["capabilities"]["modalities"]["input"] == ["image", "text", "video"]
    assert item["limits"] == {"context": 1048576, "output": 131072}
    assert item["free"] is False
    assert item["resourceClass"] == "elastic_nonscarce"
    assert item["routingRecommended"] is False
