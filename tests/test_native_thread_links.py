"""Codex Native deep-link regression and pagination tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from native_thread_links import (  # noqa: E402
    NativeThreadError,
    normalize_thread_reference,
    read_native_thread,
)

UUID = "01a121f0-9d2b-7da0-9d06-cffd2c97e871"


def _session(tmp_path: Path, *, text: str = "Some native response") -> tuple[Path, ...]:
    root = tmp_path / "sessions"
    target = root / "2026" / "10" / "09" / f"rollout-2026-10-09T12-00-00-{UUID}.jsonl"
    target.parent.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {
            "id": UUID, "cwd": "/home/dev/projects/street-story",
            "timestamp": "2026-10-09T12:00:00Z", "source": "vscode",
        }},
        {"type": "response_item", "timestamp": "2026-10-09T12:01:00Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "Start"}]}},
        {"type": "response_item", "payload": {"type": "reasoning", "text": "PRIVATE REASONING"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                             "content": [{"type": "input_text", "text": "PRIVATE DEVELOPER"}]}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "shell",
                                             "arguments": "{\"cmd\":\"echo ok\"}", "call_id": "x"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output",
                                             "output": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                                             "call_id": "x"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                             "content": [{"type": "output_text", "text": text}]}},
    ]
    target.write_text("".join(json.dumps(x) + "\n" for x in rows))
    return (root,)


def test_exact_deep_link_and_invalid_paths() -> None:
    assert normalize_thread_reference(f"codex://threads/{UUID}") == UUID
    assert normalize_thread_reference(UUID.upper()) == UUID
    for wrong in (f"codex://threads/{UUID}/../../etc/passwd",
                  f"codex://threads/{UUID}?path=/etc/passwd", "codex://threads/abc"):
        assert normalize_thread_reference(wrong) is None


def test_read_uses_conversation_and_tool_events_without_reasoning(tmp_path: Path) -> None:
    roots = _session(tmp_path)
    answer = read_native_thread(f"codex://threads/{UUID}", roots=roots)
    assert answer["backend"] == "codex-native"
    assert answer["messageCount"] == 2
    assert answer["toolEventCount"] == 2
    assert answer["rawRecordCount"] == 7
    assert answer["nextCursor"] is None
    assert answer["complete"] is True
    assert [item["kind"] for item in answer["history"]] == [
        "message", "tool_call", "tool_result", "message",
    ]
    assert "REDACTED" in answer["history"][2]["text"]
    assert "PRIVATE" not in str(answer["history"])


def test_complete_large_message_reconstructed_with_cursor(tmp_path: Path) -> None:
    text = "abc-" * 18000
    roots = _session(tmp_path, text=text)
    parts = []
    cursor = 0
    for _ in range(15):
        response = read_native_thread(UUID, roots=roots, cursor=cursor, limit=2,
                                      include_tools=False)
        parts.extend(response["history"])
        cursor = response["nextCursor"]
        if cursor is None:
            assert response["complete"]
            break
    else:
        pytest.fail("pagination did not converge")
    assert "".join(part["text"] for part in parts if part["role"] == "assistant") == text
    assert all(len(part["text"]) <= 10000 for part in parts)
    assert len(parts) > 3


def test_summary_does_not_return_tool_content(tmp_path: Path) -> None:
    roots = _session(tmp_path)
    response = read_native_thread(UUID, roots=roots, detail="summary")
    assert response["messageCount"] == 2
    assert response["toolEventCount"] == 0
    assert all(part["kind"] == "message" for part in response["latestMessages"])


def test_wrong_metadata_and_missing_session_fail_closed(tmp_path: Path) -> None:
    root, = _session(tmp_path)
    with pytest.raises(NativeThreadError, match="not present"):
        read_native_thread(UUID, roots=(tmp_path / "missing",))
    target = next(root.glob("*/*/*/*.jsonl"))
    source = target.read_text()
    target.write_text(source.replace(f'"id": "{UUID}"', '"id": "wrong"'))
    with pytest.raises(NativeThreadError, match="metadata"):
        read_native_thread(UUID, roots=(root,))


def test_symlinked_rollouts_are_ignored(tmp_path: Path) -> None:
    root, = _session(tmp_path)
    path = next(root.glob("*/*/*/*.jsonl"))
    actual = tmp_path / "outside"
    path.rename(actual)
    path.symlink_to(actual)
    with pytest.raises(NativeThreadError, match="not present"):
        read_native_thread(UUID, roots=(root,))
