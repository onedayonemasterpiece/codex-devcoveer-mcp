"""Read existing Codex Native session history by exact codex://threads UUID.

No inference, resume, writes, arbitrary paths, or secret/hidden-reasoning exports.
The native rollout file is append-only; numeric cursors refer to stable
redacted, chunked *visible* events, not raw JSONL line numbers.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from session_evidence import PATTERNS, SECRET_KEY, redact

# The historical secret-assignment expression in session_evidence has an
# unbounded greedy key prefix. On a long nonmatching string it is quadratic.
# Preserve the same detection on short strings; use a bounded, linear-time
# equivalent on long native tool-output/message strings.
_SAFE_ASSIGNMENT = re.compile(
    r"""(?im)(?<![\w.-])[\w.-]{0,128}(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|credential)[\w.-]{0,128}["']?\s{0,64}[:=]\s{0,64}(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)"""
)


def _redact_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SECRET_KEY.search(key) and key != "tokens"
                else _redact_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_safe(item) for item in value]
    if isinstance(value, str):
        if len(value) <= 2048:
            return redact(value)
        patterns = (*PATTERNS[:3], _SAFE_ASSIGNMENT, *PATTERNS[4:])
        for pattern in patterns:
            value = pattern.sub("[REDACTED]", value)
    return value

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LINK = re.compile(r"^codex://threads/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$", re.I)
_ROOTS = (
    Path("/home/dev/.local/share/devcoveer/auth/codex/sessions"),
    Path("/home/dev/.codex/sessions"),
    Path("/home/dev/.local/share/openai-codex-mcp/codex-home/sessions"),
)
_CHUNK_CHARS = 10000
_MAX_BYTES = 100000


class NativeThreadError(ValueError):
    def __init__(self, status: str, message: str) -> None:
        self.status = status
        super().__init__(message)


def normalize_thread_reference(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    match = _LINK.fullmatch(value.strip())
    if match:
        return match.group(1).lower()
    value = value.strip().lower()
    return value if _UUID.fullmatch(value) else None


def is_thread_link(value: str) -> bool:
    return isinstance(value, str) and _LINK.fullmatch(value.strip()) is not None


def _discover(thread_id: str, roots: tuple[Path, ...]) -> Path:
    # Roots are ordered by ownership: the actual DevCoveer Codex Native home
    # is authoritative; the bridge's own default Codex home is a fallback.
    # One session may be copied into both profiles, so do not reject a
    # canonical native session merely because a shadow copy also exists.
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        matches: list[Path] = []
        # Exact UUID suffix and fixed year/month/day hierarchy; no general
        # project, HOME, or repository search and no paths from the caller.
        for path in root.glob(f"*/*/*/rollout-*-{thread_id}.jsonl"):
            if path.is_symlink() or not path.is_file():
                continue
            if any(part.is_symlink() for part in (path.parent, path.parent.parent, path.parent.parent.parent)):
                continue
            if not path.resolve().is_relative_to(root.resolve()):
                continue
            matches.append(path)
        if len(matches) > 1:
            raise NativeThreadError("ambiguous", "The native thread has multiple rollouts in its authoritative session store.")
        if matches:
            return matches[0]
    raise NativeThreadError("not_found", "Codex Native session is not present in the authorized session stores.")


def _entry(row: dict[str, Any], include_tools: bool) -> tuple[str, str, str | None, Any] | None:
    if row.get("type") != "response_item":
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind == "message" and payload.get("role") in ("user", "assistant"):
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            return None
        texts = [
            str(part.get("text"))
            for part in blocks
            if isinstance(part, dict)
            and part.get("type") in ("input_text", "output_text", "text")
            and isinstance(part.get("text"), str)
        ]
        if not texts:
            return None
        return str(payload["role"]), "message", None, "\n".join(texts)
    if not include_tools:
        return None
    if kind in ("custom_tool_call", "function_call"):
        return "assistant", "tool_call", str(payload.get("name") or ""), {
            "arguments": payload.get("arguments", payload.get("input", "")),
            "callId": payload.get("call_id"),
        }
    if kind in ("custom_tool_call_output", "function_call_output"):
        return "tool", "tool_result", None, {
            "output": payload.get("output", ""),
            "callId": payload.get("call_id"),
        }
    # Never return model-private reasoning, developer/system messages,
    # world state, instructions, token usage or raw credential-bearing metadata.
    return None


def read_native_thread(
    reference: str,
    *,
    cursor: int = 0,
    limit: int = 40,
    detail: str = "full",
    include_tools: bool | None = None,
    roots: tuple[Path, ...] = _ROOTS,
) -> dict[str, Any]:
    thread_id = normalize_thread_reference(reference)
    if thread_id is None:
        raise NativeThreadError("invalid_reference", "Expected an exact UUID or codex://threads/<UUID> link.")
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        raise NativeThreadError("invalid_cursor", "Cursor must be a nonnegative integer.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise NativeThreadError("invalid_limit", "Limit must be between 1 and 100.")
    if detail not in ("summary", "full"):
        raise NativeThreadError("invalid_detail", "Detail must be summary or full.")
    if include_tools is None:
        include_tools = detail == "full"
    path = _discover(thread_id, roots)
    before = path.stat()
    collected: list[dict[str, Any]] = []
    last_visible: list[dict[str, Any]] = []
    raw_lines = 0
    visible_messages = 0
    visible_tools = 0
    chunks = 0
    verified_metadata = False
    meta: dict[str, Any] = {}
    used_bytes = 0
    page_exhausted = False

    with path.open("r", encoding="utf-8") as source:
        for line in source:
            raw_lines += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A live writer may have an incomplete final line. Do not call
                # the snapshot complete until the next read stabilizes.
                continue
            if not isinstance(row, dict):
                continue
            if row.get("type") == "session_meta":
                payload = row.get("payload")
                if isinstance(payload, dict) and payload.get("id") == thread_id:
                    verified_metadata = True
                    meta = {
                        "cwd": payload.get("cwd"),
                        "createdAt": payload.get("timestamp"),
                        "source": str(payload.get("source", ""))[:100],
                    }
                continue
            entry = _entry(row, include_tools)
            if entry is None:
                continue
            role, kind, tool, raw_value = entry
            if kind == "message":
                visible_messages += 1
            else:
                visible_tools += 1
            raw_text = (raw_value if isinstance(raw_value, str)
                        else json.dumps(raw_value, ensure_ascii=False, separators=(",", ":")))
            # Event chunk counts are based on the original text, not on
            # redacted length. This keeps cursor positions stable without
            # doing expensive secret redaction for thousands of skipped tools.
            number_of_chunks = max(1, (len(raw_text) + _CHUNK_CHARS - 1) // _CHUNK_CHARS)
            needs_content = (
                (detail == "summary" and kind == "message")
                or (detail == "full" and not page_exhausted
                    and chunks + number_of_chunks > cursor
                    and chunks < cursor + limit and len(collected) < limit)
            )
            safe_value = ""
            if needs_content:
                safe_value = _redact_safe(raw_value)
                if not isinstance(safe_value, str):
                    safe_value = json.dumps(safe_value, ensure_ascii=False, separators=(",", ":"))
            for part_number in range(number_of_chunks):
                index = chunks
                chunks += 1
                if not needs_content:
                    continue
                # Equal partitions reconstruct the whole redacted event even
                # when replacing a secret changes its total length.
                begin = len(safe_value) * part_number // number_of_chunks
                end = len(safe_value) * (part_number + 1) // number_of_chunks
                piece = {
                    "index": index, "timestamp": row.get("timestamp"),
                    "role": role, "kind": kind, "text": safe_value[begin:end],
                }
                if tool:
                    piece["tool"] = tool
                if number_of_chunks > 1:
                    piece["part"] = part_number + 1
                    piece["parts"] = number_of_chunks
                if detail == "summary":
                    last_visible.append(piece)
                    if len(last_visible) > 6:
                        last_visible.pop(0)
                elif index >= cursor and not page_exhausted and len(collected) < limit:
                    encoded = len(json.dumps(piece, ensure_ascii=False).encode("utf-8"))
                    if used_bytes + encoded <= _MAX_BYTES or not collected:
                        collected.append(piece)
                        used_bytes += encoded
                    else:
                        # Never skip the oversized next item and then return
                        # later smaller items: nextCursor must represent one
                        # contiguous prefix of the underlying event history.
                        page_exhausted = True

    if not verified_metadata:
        raise NativeThreadError("invalid_source", "Native session metadata does not match the requested UUID.")
    after = path.stat()
    stable = before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
    if detail == "summary":
        return {
            "status": "ok", "backend": "codex-native", "threadId": thread_id,
            "threadUrl": f"codex://threads/{thread_id}", "detail": "summary",
            "task": meta, "messageCount": visible_messages,
            "toolEventCount": 0, "visibleChunkCount": chunks,
            "latestMessages": last_visible, "complete": stable,
            "historyAvailable": True,
            "hint": "Use detail=full and cursor=0, then follow nextCursor until null.",
        }
    if cursor > chunks:
        raise NativeThreadError("invalid_cursor", "Cursor exceeds the available visible history.")
    next_cursor = cursor + len(collected)
    has_more = next_cursor < chunks
    return {
        "status": "ok", "backend": "codex-native", "threadId": thread_id,
        "threadUrl": f"codex://threads/{thread_id}", "detail": "full",
        "task": meta, "history": collected,
        "cursor": cursor, "nextCursor": next_cursor if has_more else None,
        "messageCount": visible_messages, "toolEventCount": visible_tools,
        "visibleChunkCount": chunks, "rawRecordCount": raw_lines,
        "pageComplete": len(collected) > 0 or cursor == chunks,
        "complete": stable and not has_more,
        "snapshotStable": stable,
        "omitted": ["hidden_reasoning", "system_and_developer_messages", "world_state", "private_runtime_metadata"],
        "redaction": "secret-bearing strings and keys are redacted",
    }
