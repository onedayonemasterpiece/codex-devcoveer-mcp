"""Short-horizon, credential-free runtime health memory for DevCoveer models.

The store deliberately remembers only operational facts (latency/outcome/category),
never prompts, responses, provider payloads, credentials, or raw error bodies.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MODEL_HEALTH_DIR = Path("/home/dev/.local/share/openai-codex-mcp/model-health")
MODEL_HEALTH_SCHEMA = "devcoveer-model-health.v1"
EVENT_RETENTION_MS = 5 * 60 * 60 * 1000
WINDOWS_MS = {"1h": 60 * 60 * 1000, "3h": 3 * 60 * 60 * 1000, "5h": 5 * 60 * 60 * 1000}
SELECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:/-]{0,190}$")
PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_ALLOWED_OUTCOMES = {"success", "failure", "retry", "timeout", "cancelled"}
_ALLOWED_CATEGORIES = {
    "ok", "quota_exhausted", "rate_limited", "timeout", "eol",
    "provider_unavailable", "model_error", "upstream_error", "cancelled", "unknown",
}

# These are router probe cooldowns, not provider reset times. resetAt/retryAfter
# are exposed separately only when the provider actually supplies them.
_POLICY_COOLDOWN_MS = {
    "quota_exhausted": 30 * 60 * 1000,
    "rate_limited": 5 * 60 * 1000,
    "timeout": 2 * 60 * 1000,
    "provider_unavailable": 5 * 60 * 1000,
    "model_error": 5 * 60 * 1000,
    "upstream_error": 5 * 60 * 1000,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * p
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[lo]
    return int(round(ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)))


def resource_class(provider: str) -> str:
    if provider == "opencode":
        return "renewable_free_burst"
    if provider.startswith("groq"):
        return "renewable_key_pool"
    if provider == "nvidia":
        return "elastic_nonscarce"
    if provider.startswith("giga"):
        return "metered_reserve"
    return "metered_or_unknown"


def resource_priority(provider: str) -> int:
    cls = resource_class(provider)
    return {
        "renewable_free_burst": 10,
        "renewable_key_pool": 15,
        "elastic_nonscarce": 20,
        "metered_reserve": 30,
        "metered_or_unknown": 40,
    }[cls]


class ModelHealthStore:
    _thread_lock = threading.RLock()

    def __init__(self, root: Path = MODEL_HEALTH_DIR) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.events_path = self.root / "events.json"
        self.lock_path = self.root / ".lock"
        self.lock_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.lock_path, 0o600)

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            with self.lock_path.open("r+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_locked(self) -> list[dict[str, Any]]:
        if not self.events_path.is_file() or self.events_path.is_symlink():
            return []
        if self.events_path.stat().st_size > 2_000_000:
            return []
        try:
            value = json.loads(self.events_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError):
            return []
        if not isinstance(value, dict) or value.get("schema") != MODEL_HEALTH_SCHEMA:
            return []
        events = value.get("events")
        return [x for x in events if isinstance(x, dict)] if isinstance(events, list) else []

    def _write_locked(self, events: list[dict[str, Any]]) -> None:
        payload = {"schema": MODEL_HEALTH_SCHEMA, "events": events[-5000:]}
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) + "\n").encode()
        fd, temp_name = tempfile.mkstemp(prefix=".health-", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.events_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _prune(events: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
        cutoff = now_ms - EVENT_RETENTION_MS
        return [e for e in events if isinstance(e.get("observedAt"), int) and e["observedAt"] >= cutoff]

    def record(
        self,
        *,
        provider: str,
        selection: str,
        outcome: str,
        category: str,
        observed_at: int | None = None,
        latency_ms: int | None = None,
        retryable: bool | None = None,
        status_code: int | None = None,
        event_key: str | None = None,
    ) -> bool:
        if PROVIDER_RE.fullmatch(provider) is None or SELECTION_RE.fullmatch(selection) is None:
            return False
        if outcome not in _ALLOWED_OUTCOMES:
            outcome = "failure"
        if category not in _ALLOWED_CATEGORIES:
            category = "unknown"
        now = observed_at if isinstance(observed_at, int) and observed_at > 0 else _now_ms()
        event: dict[str, Any] = {
            "provider": provider,
            "selection": selection,
            "outcome": outcome,
            "category": category,
            "observedAt": now,
        }
        if isinstance(latency_ms, int) and 0 <= latency_ms <= 24 * 60 * 60 * 1000:
            event["latencyMs"] = latency_ms
        if isinstance(retryable, bool):
            event["retryable"] = retryable
        if isinstance(status_code, int) and 100 <= status_code <= 599:
            event["statusCode"] = status_code
        if isinstance(event_key, str) and 1 <= len(event_key) <= 200:
            event["eventKey"] = event_key

        with self._locked():
            events = self._prune(self._read_locked(), now)
            if event_key and any(e.get("eventKey") == event_key for e in events):
                return False
            events.append(event)
            try:
                self._write_locked(events)
            except OSError:
                # Optional health telemetry must not disable task readback on ENOSPC.
                return False
        return True

    def _events_for(self, selection: str, *, now_ms: int) -> list[dict[str, Any]]:
        with self._locked():
            events = self._prune(self._read_locked(), now_ms)
        return [e for e in events if e.get("selection") == selection]

    @staticmethod
    def _summarize(selection: str, events: list[dict[str, Any]], now: int) -> dict[str, Any]:
        provider = selection.split("/", 1)[0] if "/" in selection else ""
        windows: dict[str, Any] = {}
        health_events = [e for e in events if e.get("outcome") != "cancelled"]
        for label, span in WINDOWS_MS.items():
            subset = [e for e in health_events if e["observedAt"] >= now - span]
            latencies = [e["latencyMs"] for e in subset if isinstance(e.get("latencyMs"), int)]
            successes = sum(e.get("outcome") == "success" for e in subset)
            attempts = len(subset)
            windows[label] = {
                "attempts": attempts,
                "successes": successes,
                "successRate": round(successes / attempts, 3) if attempts else None,
                "p50LatencyMs": _percentile(latencies, 0.50),
                "p95LatencyMs": _percentile(latencies, 0.95),
                "rateLimitCount": sum(e.get("category") == "rate_limited" for e in subset),
                "quotaExhaustedCount": sum(e.get("category") == "quota_exhausted" for e in subset),
                "timeoutCount": sum(e.get("category") == "timeout" for e in subset),
                "hardFailureCount": sum(e.get("category") == "eol" for e in subset),
            }

        latest = max(health_events, key=lambda e: e["observedAt"]) if health_events else None
        last_success = max((e["observedAt"] for e in health_events if e.get("outcome") == "success"), default=None)
        last_failure = max((e["observedAt"] for e in health_events if e.get("outcome") != "success"), default=None)
        runtime_status = "unknown"
        cooldown_until = None
        retryable = None
        last_category = None
        if latest:
            last_category = latest.get("category")
            retryable = latest.get("retryable")
            if latest.get("category") == "eol":
                runtime_status = "eol"
            elif latest.get("outcome") == "success":
                runtime_status = "available_recent_success"
            else:
                cooldown = _POLICY_COOLDOWN_MS.get(str(latest.get("category")))
                if cooldown:
                    cooldown_until = int(latest["observedAt"]) + cooldown
                    runtime_status = "temporarily_unavailable" if cooldown_until > now else "probe_due"
                else:
                    runtime_status = "degraded"

        p95 = windows["1h"]["p95LatencyMs"] or windows["3h"]["p95LatencyMs"]
        baseline = 180_000 if provider == "opencode" else 300_000 if provider == "nvidia" else 120_000
        stall_threshold = max(baseline, int(p95 * 2) if isinstance(p95, int) else 0)
        return {
            "resourceClass": resource_class(provider),
            "resourcePriority": resource_priority(provider),
            "currentRuntimeStatus": runtime_status,
            "lastCategory": last_category,
            "retryable": retryable,
            "cooldownUntil": cooldown_until,
            "lastSuccessAt": last_success,
            "lastFailureAt": last_failure,
            "stallThresholdMs": stall_threshold,
            "windows": windows,
        }

    def summary(self, selection: str, *, now_ms: int | None = None) -> dict[str, Any]:
        now = now_ms if isinstance(now_ms, int) else _now_ms()
        events = self._events_for(selection, now_ms=now)
        return self._summarize(selection, events, now)

    def provider_summary(self, provider: str, *, now_ms: int | None = None) -> dict[str, Any]:
        """Provider-level health only for genuinely provider-wide conditions.

        Zen quota exhaustion is treated as account/provider-wide until a later Zen
        success or the policy cooldown expires. NVIDIA 429/latency remains model-local
        because load is known to vary strongly by endpoint.
        """
        now = now_ms if isinstance(now_ms, int) else _now_ms()
        with self._locked():
            events = self._prune(self._read_locked(), now)
        subset = [
            e for e in events
            if e.get("provider") == provider and e.get("outcome") != "cancelled"
        ]
        latest = max(subset, key=lambda e: e["observedAt"]) if subset else None
        status = "unknown"
        category = None
        cooldown_until = None
        if latest:
            category = latest.get("category")
            if latest.get("outcome") == "success":
                status = "available_recent_success"
            elif provider == "opencode" and category == "quota_exhausted":
                cooldown_until = int(latest["observedAt"]) + _POLICY_COOLDOWN_MS["quota_exhausted"]
                status = "temporarily_unavailable" if cooldown_until > now else "probe_due"
            elif category == "eol":
                # EOL is endpoint-specific and never makes the whole provider unavailable.
                status = "degraded_model_only"
            else:
                status = "degraded_observation"
        return {
            "provider": provider,
            "currentRuntimeStatus": status,
            "lastCategory": category,
            "cooldownUntil": cooldown_until,
            "lastObservedAt": latest.get("observedAt") if latest else None,
        }

    def all_summaries(self, selections: list[str], *, now_ms: int | None = None) -> dict[str, dict[str, Any]]:
        now = now_ms if isinstance(now_ms, int) else _now_ms()
        with self._locked():
            events = self._prune(self._read_locked(), now)
        by_selection: dict[str, list[dict[str, Any]]] = {selection: [] for selection in selections}
        for event in events:
            selection = event.get("selection")
            if selection in by_selection:
                by_selection[selection].append(event)
        return {selection: self._summarize(selection, by_selection[selection], now) for selection in selections}
