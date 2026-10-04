"""Native Codex status and explicit model profiles; no inference or credential I/O.

The caller supplies the existing authenticated app-server client. This module
never starts a task, buys credits, switches accounts or substitutes an API key.
Quota eligibility is a snapshot, not a permission to dispatch or a spend cap.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

DEFAULT_PROFILE = "gpt-6.1-medium"
# User-facing shorthand is not a claim that this account has either native ID.
MODEL_FAMILY_IDS = ("gpt-6.1", "gpt-6.1-sol")
RESERVE_PERCENT = 10.0
STATUS_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def resolve_profile(model: str | None, effort: str | None,
                    catalog: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Expand only the explicit owner shorthand; leave all other models alone."""
    if model != DEFAULT_PROFILE:
        return model, effort
    if effort not in (None, "medium"):
        raise ValueError("gpt-6.1-medium conflicts with the explicit reasoning effort")
    candidates = []
    for row in catalog:
        if not isinstance(row, dict):
            continue
        names = {row.get("id"), row.get("model")}
        if any(name in names for name in MODEL_FAMILY_IDS):
            candidates.append(row)
    if len(candidates) != 1:
        raise ValueError("Requested GPT-6.1 profile is unavailable or ambiguous in the native catalog")
    candidate = candidates[0]
    supported = {option.get("reasoningEffort") for option in
                 candidate.get("supportedReasoningEfforts", []) if isinstance(option, dict)}
    if "medium" not in supported:
        raise ValueError("Native GPT-6.1 catalog does not support medium reasoning")
    canonical = candidate.get("model") or candidate.get("id")
    if not isinstance(canonical, str) or not canonical:
        raise ValueError("Invalid native GPT-6.1 model ID")
    return canonical, "medium"


def _percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def normalize_limits(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Preserve independent windows; invalid/missing quota is never availability."""
    now = now or datetime.now(timezone.utc)
    observed_at = now.isoformat()
    result: dict[str, Any] = {
        "status": "unknown", "source_method": "account/rateLimits/read",
        "observed_at": observed_at, "limits": [],
        "admission": {"eligible": False, "reason": "quota_unavailable",
                      "reserve_percent": RESERVE_PERCENT,
                      "effective_remaining_percent": None,
                      "scope": "codex_account_snapshot_only",
                      "separate_owner_approval_required": True},
    }
    if not isinstance(payload, dict):
        return result
    buckets = payload.get("rateLimitsByLimitId")
    if isinstance(buckets, dict) and buckets:
        selected = buckets.get("codex")
        entries = list(buckets.items())
    else:
        selected = payload.get("rateLimits")
        entries = [("codex", selected)]
    selected_row = None
    for key, bucket in entries[:32]:
        if not isinstance(key, str) or not isinstance(bucket, dict):
            continue
        ident = bucket.get("limitId") or key
        if not isinstance(ident, str) or len(ident) > 100:
            continue
        windows, invalid, expired = [], False, False
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if window is None:
                continue
            if not isinstance(window, dict):
                invalid = True
                continue
            used = _percent(window.get("usedPercent"))
            if used is None:
                invalid = True
                continue
            duration = window.get("windowDurationMins")
            reset = window.get("resetsAt")
            if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
                duration = None
            if not isinstance(reset, (int, float)) or isinstance(reset, bool) or not math.isfinite(reset):
                reset = None
            elif reset <= now.timestamp():
                expired = True
            windows.append({"window": name, "used_percent": used,
                            "remaining_percent": 100.0 - used,
                            "duration_minutes": duration, "resets_at_unix": reset})
        reached = bucket.get("rateLimitReachedType") is not None
        row = {"limit_id": ident, "windows": windows,
               "valid": bool(windows) and not invalid and not expired,
               "limit_reached": reached}
        result["limits"].append(row)
        if bucket is selected and ident == "codex":
            selected_row = row
    admission = result["admission"]
    if selected_row is None:
        admission["reason"] = "codex_quota_bucket_unknown"
        return result
    if not selected_row["valid"]:
        admission["reason"] = "quota_invalid_or_reset_pending"
        return result
    remaining = min(window["remaining_percent"] for window in selected_row["windows"])
    admission["effective_remaining_percent"] = remaining
    admission["eligible"] = not selected_row["limit_reached"] and remaining > RESERVE_PERCENT
    admission["reason"] = ("quota_available" if admission["eligible"] else "reserve_or_limit_reached")
    result["status"] = "available"
    return result


async def read_status(history: Any) -> dict[str, Any]:
    """Machine equivalent of quota in /status, using the existing app-server."""
    try:
        raw = await history.request("account/rateLimits/read", {}, timeout=15)
        result = normalize_limits(raw)
    except Exception as exc:
        result = normalize_limits(None)
        result["error_type"] = type(exc).__name__
    result["inference_performed"] = False
    result["default_profile"] = {"requested": DEFAULT_PROFILE,
                                 "model": None, "reasoning_effort": "medium",
                                 "catalog_available": False,
                                 "inference_verified": False}
    try:
        catalog: list[dict[str, Any]] = []
        cursor, seen = None, set()
        for _ in range(5):
            page = await history.request("model/list", {
                "cursor": cursor, "limit": 100, "includeHidden": True,
            }, timeout=15)
            rows = page.get("data")
            if not isinstance(rows, list):
                raise ValueError("Invalid native catalog")
            catalog.extend(row for row in rows if isinstance(row, dict))
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError("Invalid native catalog cursor")
            seen.add(cursor)
        else:
            raise ValueError("Native catalog page limit exceeded")
        model, effort = resolve_profile(DEFAULT_PROFILE, None, catalog)
        result["default_profile"].update(model=model, reasoning_effort=effort, catalog_available=True)
    except Exception as exc:
        result["default_profile"]["error_type"] = type(exc).__name__
    result["admission"]["model_catalog_available"] = result["default_profile"]["catalog_available"]
    # Never turn this read-only quota observation into dispatch authority.
    return result



def normalize_turn_token_usage(payload: Any) -> dict[str, int] | None:
    """Project only the current native turn breakdown; cumulative thread totals are excluded."""
    if not isinstance(payload, dict):
        return None
    last = payload.get("last")
    if not isinstance(last, dict):
        return None
    keys = (
        "inputTokens",
        "cachedInputTokens",
        "outputTokens",
        "reasoningOutputTokens",
        "totalTokens",
    )
    result: dict[str, int] = {}
    for key in keys:
        value = last.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        result[key] = value
    return result


def progress_from_turn(turn: Any) -> dict[str, Any]:
    """Return a safe coarse phase from native execution items without exposing commands."""
    if not isinstance(turn, dict):
        return {"progressPhase": "preparing"}

    status = str(turn.get("status") or "")
    if status == "completed":
        return {"progressPhase": "ready"}
    if status == "failed":
        return {"progressPhase": "failed"}
    if status in {"interrupted", "cancelled", "canceled"}:
        return {"progressPhase": "cancelled"}

    items = turn.get("items")
    if not isinstance(items, list):
        return {"progressPhase": "planning"}

    phase = "planning"
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "commandExecution":
            continue
        command = str(item.get("command") or "").casefold()
        phase = "implementing"
        if any(token in command for token in (
            "pytest", "gradle test", "gradlew test", "npm test", "pnpm test",
            "yarn test", "vitest", "jest", "go test", "cargo test", "mvn test",
        )):
            phase = "testing"
        elif any(token in command for token in (
            "gh run", "gh pr checks", "workflow_dispatch", "github actions",
        )):
            phase = "ci"
        elif any(token in command for token in (
            "gh release", "android-release", "release.yml", "release.yaml",
        )):
            phase = "releasing"
        elif any(token in command for token in (
            "devcoveer_install", "deploy", "systemctl", "kubectl", "fly deploy",
        )):
            phase = "deploying"
        elif any(token in command for token in ("git push", "gh pr create")):
            phase = "publishing"
    return {"progressPhase": phase}


def patch_bridge(source: str) -> str:
    """Called by the canonical builder, never applied to live processes in place."""
    def replace(old: str, new: str, label: str) -> None:
        nonlocal source
        if source.count(old) != 1:
            raise ValueError("Codex account bridge anchor mismatch: " + label)
        source = source.replace(old, new, 1)

    replace('\nBRIDGE_VERSION = ',
            '\nfrom codex_account import STATUS_SCHEMA, normalize_turn_token_usage as _codex_normalize_turn_token_usage, progress_from_turn as _codex_progress_from_turn, read_status as _codex_read_status, resolve_profile as _codex_resolve_profile\n\nBRIDGE_VERSION = ',
            'import')
    replace('TOOLS = [\n', '''TOOLS = [
    types.Tool(name="codex_status", title="Read native Codex quota and owner model profile",
        description=("Read current account quota via native account/rateLimits/read, the machine equivalent of /status. "
                     "No inference, task launch, credit purchase or credential export. Shows independent quota windows, "
                     "a strictly-greater-than-10-percent snapshot gate, and whether gpt-6.1-medium resolves in the native "
                     "model catalog. Availability is not inference verification or owner authorization."),
        input_schema=STATUS_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=True)),
''', 'tool')
    replace('VALIDATORS = {\n',
            'VALIDATORS = {\n    "codex_status": Draft202012Validator(STATUS_SCHEMA),\n', 'validator')
    replace('        if name == "list_models":\n', '''        if name == "codex_status":
            return _tool_result(await _codex_read_status(history))

        if name == "list_models":
''', 'handler')
    replace('        self._turn_status: dict[str, dict[str, Any]] = {}\n',
            '        self._turn_status: dict[str, dict[str, Any]] = {}\n        self._turn_token_usage: dict[tuple[str, str], dict[str, int]] = {}\n',
            'native turn token usage state')
    replace('        thread_id = params.get("threadId")\n        if method == "thread/status/changed" and isinstance(thread_id, str):\n',
            '''        thread_id = params.get("threadId")
        if method == "thread/tokenUsage/updated" and isinstance(thread_id, str):
            turn_id = params.get("turnId")
            usage = _codex_normalize_turn_token_usage(params.get("tokenUsage"))
            if isinstance(turn_id, str) and usage is not None:
                key = (thread_id, turn_id)
                if key not in self._turn_token_usage and len(self._turn_token_usage) >= 2048:
                    self._turn_token_usage.pop(next(iter(self._turn_token_usage)))
                self._turn_token_usage[key] = usage
        elif method == "thread/status/changed" and isinstance(thread_id, str):
''',
            'native token usage notification')
    replace('    def _fail_pending(self, reason: str) -> None:\n',
            '''    def turn_token_usage(self, thread_id: str, turn_id: str) -> dict[str, int] | None:
        value = self._turn_token_usage.get((thread_id, turn_id))
        return dict(value) if value is not None else None

    def _fail_pending(self, reason: str) -> None:
''',
            'native token usage accessor')
    replace('            record = _task_record(native_thread, entry, record["taskReference"])\n            summary = _turn_summary(_last_turn(native_thread))\n            turn_status = summary.get("status") if isinstance(summary, dict) else None\n',
            '''            record = _task_record(native_thread, entry, record["taskReference"])
            last_turn = _last_turn(native_thread)
            summary = _turn_summary(last_turn)
            if isinstance(summary, dict) and isinstance(last_turn, dict):
                last_turn_id = last_turn.get("id")
                if isinstance(last_turn_id, str):
                    usage = history.turn_token_usage(thread_id, last_turn_id)
                    if usage is not None:
                        summary["tokenUsage"] = usage
            turn_status = summary.get("status") if isinstance(summary, dict) else None
''',
            'read_task native token usage')
    replace('    if final_response is not None:\n        summary["finalResponse"] = final_response\n    return summary\n',
            '    summary.update(_codex_progress_from_turn(turn))\n    if final_response is not None:\n        summary["finalResponse"] = final_response\n    return summary\n',
            'native task progress phase')
    # Resolve the shorthand only after the native catalog was obtained. Hidden
    # catalog models remain opt-in; ordinary discovery is unchanged.
    start = source.index('async def _resolve_model_selection(')
    end = source.index('\n\ndef _tool_result(', start)
    original = source[start:end]
    changed = original.replace('"includeHidden": False,', '"includeHidden": True,', 1)
    anchor = '    selected: list[dict[str, Any]]\n'
    if changed.count(anchor) != 1:
        raise ValueError('Codex profile selection anchor mismatch')
    changed = changed.replace(anchor, '''    try:
        requested_model, requested_effort = _codex_resolve_profile(
            requested_model, requested_effort, models,
        )
    except ValueError as exc:
        raise _invalid_params("model", str(exc)) from None

''' + anchor)
    source = source[:start] + changed + source[end:]
    return source
