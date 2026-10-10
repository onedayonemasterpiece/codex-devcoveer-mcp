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



def test_byte_budget_pages_are_contiguous_and_lossless(tmp_path: Path) -> None:
    roots = _session(tmp_path)
    source = next(roots[0].glob("*/*/*/*.jsonl"))
    # Intentionally exceed the 100-KB response budget; a small event after a
    # large rejected one must never be returned out of cursor order.
    with source.open("a", encoding="utf-8") as stream:
        for i in range(27):
            stream.write(json.dumps({
                "type": "response_item", "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": str(i),
                    "output": f"event-{i:02d}:" + ("Q" * 7000),
                },
            }) + "\n")
    cursor = 0
    received = []
    for _ in range(20):
        page = read_native_thread(UUID, roots=roots, cursor=cursor, limit=100)
        assert page["history"]
        indices = [item["index"] for item in page["history"]]
        assert indices == list(range(cursor, cursor + len(indices)))
        assert len(json.dumps(page["history"]).encode()) < 110000
        received.extend(page["history"])
        cursor = page["nextCursor"]
        if cursor is None:
            assert page["complete"] is True
            assert len(received) == page["visibleChunkCount"]
            break
    else:
        pytest.fail("The byte-budget pagination did not converge.")
    assert [entry["index"] for entry in received] == list(range(len(received)))
    tool_results = [entry["text"] for entry in received
                    if entry["kind"] == "tool_result" and "event-" in entry["text"]]
    assert len(tool_results) == 27
    assert all(text == f'{{"output":"event-{i:02d}:' + ("Q" * 7000)
               + f'","callId":"{i}"}}' for i, text in enumerate(tool_results))

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


def _append_rows(roots: tuple[Path, ...], rows: list[dict]) -> Path:
    source = next(roots[0].glob("*/*/*/*.jsonl"))
    with source.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return source


def test_snapshot_pins_prefix_across_append(tmp_path: Path) -> None:
    roots = _session(tmp_path)
    first = read_native_thread(UUID, roots=roots, limit=1)
    token = first["snapshot"]
    _append_rows(roots, [{"type": "response_item", "payload": {
        "type": "message", "role": "assistant", "channel": "final",
        "content": [{"type": "output_text", "text": "NEW APPEND"}],
    }}])
    rest = read_native_thread(UUID, roots=roots, cursor=first["nextCursor"], snapshot=token)
    assert rest["snapshot"] == token
    assert rest["snapshotStable"] and rest["complete"]
    assert rest["messageCount"] == 2
    assert "NEW APPEND" not in str(rest["history"])
    fresh = read_native_thread(UUID, roots=roots)
    assert fresh["messageCount"] == 3
    assert fresh["snapshot"]["bytes"] > token["bytes"]


@pytest.mark.parametrize("change", ["mutate", "truncate"])
def test_snapshot_rejects_changed_prefix(tmp_path: Path, change: str) -> None:
    roots = _session(tmp_path)
    first = read_native_thread(UUID, roots=roots, limit=1)
    source = next(roots[0].glob("*/*/*/*.jsonl"))
    data = source.read_bytes()
    source.write_bytes(data.replace(b"Start", b"Other") if change == "mutate" else data[:-10])
    with pytest.raises(NativeThreadError) as error:
        read_native_thread(UUID, roots=roots, cursor=first["nextCursor"], snapshot=first["snapshot"])
    assert error.value.status == "snapshot_mismatch"


@pytest.mark.parametrize("bad", [b'{"type":', b'\xff\n', b'[]\n'])
def test_malformed_record_never_reports_complete(tmp_path: Path, bad: bytes) -> None:
    roots = _session(tmp_path)
    source = next(roots[0].glob("*/*/*/*.jsonl"))
    with source.open("ab") as stream:
        stream.write(bad)
    for detail in ("summary", "full"):
        page = read_native_thread(UUID, roots=roots, detail=detail)
        assert page["parseErrors"] == 1
        assert page["complete"] is False
        assert page["snapshotStable"] is True


@pytest.mark.parametrize("change", [
    {"bytes": -1}, {"bytes": True}, {"sha256": "invalid"},
    {"threadId": "wrong"}, {"includeTools": False}, {"includeTools": 1},
    {"version": 2}, {"version": True}, {"path": "/etc/passwd"},
])
def test_snapshot_schema_is_exact_and_bound(tmp_path: Path, change: dict) -> None:
    roots = _session(tmp_path)
    token = read_native_thread(UUID, roots=roots)["snapshot"]
    with pytest.raises(NativeThreadError) as error:
        read_native_thread(UUID, roots=roots, snapshot={**token, **change})
    assert error.value.status == "invalid_snapshot"


@pytest.mark.parametrize("channel", ["analysis", "private", "unknown", "", {}, []])
def test_private_channels_exclude_messages_calls_and_results(tmp_path: Path, channel: object) -> None:
    roots = _session(tmp_path)
    private_payloads = [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "PRIVATE MESSAGE"}]},
        {"type": "function_call", "name": "private_tool", "arguments": "PRIVATE CALL", "call_id": "private"},
        {"type": "function_call_output", "output": "PRIVATE RESULT", "call_id": "other"},
    ]
    for location in ("row", "payload"):
        rows = []
        for payload in private_payloads:
            row = {"type": "response_item", "payload": dict(payload)}
            (row if location == "row" else row["payload"])["channel"] = channel
            rows.append(row)
        rows.append({"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "private", "output": "PRIVATE INHERITED RESULT",
        }})
        _append_rows(roots, rows)
    page = read_native_thread(UUID, roots=roots)
    assert "PRIVATE" not in str(page["history"])
    assert page["messageCount"] == 2
    assert page["toolEventCount"] == 2


@pytest.mark.parametrize("channel", [None, "final", "commentary"])
def test_public_channels_keep_messages_and_tools(tmp_path: Path, channel: str | None) -> None:
    roots = _session(tmp_path)
    _append_rows(roots, [{"type": "response_item", "payload": {**payload, "channel": channel}} for payload in [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "PUBLIC"}]},
        {"type": "function_call", "name": "visible_tool", "arguments": "PUBLIC", "call_id": "public"},
        {"type": "function_call_output", "output": "PUBLIC", "call_id": "public"},
    ]])
    page = read_native_thread(UUID, roots=roots)
    assert page["messageCount"] == 3
    assert page["toolEventCount"] == 4
    assert [row["role"] for row in page["history"][-3:]] == ["assistant", "assistant", "tool"]


def test_oversized_unicode_tools_reconstruct_under_pinned_pagination(tmp_path: Path) -> None:
    roots = _session(tmp_path, text="Публичное сообщение")
    output = "🎨界" * 17000
    _append_rows(roots, [{"type": "response_item", "payload": {
        "type": "function_call_output", "output": output, "call_id": "big",
    }}])
    page = read_native_thread(UUID, roots=roots, limit=2)
    token = page["snapshot"]
    history = list(page["history"])
    while page["nextCursor"] is not None:
        page = read_native_thread(UUID, roots=roots, limit=2,
                                  cursor=page["nextCursor"], snapshot=token)
        assert page["snapshot"] == token
        history.extend(page["history"])
    assert page["complete"]
    assert [item["index"] for item in history] == list(range(len(history)))
    pieces = history[4:]
    assert len(pieces) > 1
    assert all(part["kind"] == "tool_result" and part["role"] == "tool" for part in pieces)
    value = json.loads("".join(part["text"] for part in pieces))
    assert value == {"output": output, "callId": "big"}

def test_first_page_boundary_is_fixed_before_source_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import native_thread_links as reader
    roots = _session(tmp_path)
    source = next(roots[0].glob("*/*/*/*.jsonl"))
    original_size = source.stat().st_size
    original_digest = reader._prefix_digest
    appended = False

    def append_during_read(stream: object, boundary: int) -> str | None:
        nonlocal appended
        if not appended:
            appended = True
            _append_rows(roots, [{"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "text", "text": "LATER"}],
            }}])
        return original_digest(stream, boundary)

    monkeypatch.setattr(reader, "_prefix_digest", append_during_read)
    page = read_native_thread(UUID, roots=roots)
    assert page["snapshot"]["bytes"] == original_size
    assert page["complete"] and page["snapshotStable"]
    assert page["messageCount"] == 2
    assert "LATER" not in str(page["history"])


def test_incomplete_pinned_tail_requires_fresh_snapshot(tmp_path: Path) -> None:
    roots = _session(tmp_path)
    source = next(roots[0].glob("*/*/*/*.jsonl"))
    extra = json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "text", "text": "FINISHED"}],
    }}).encode()
    with source.open("ab") as stream:
        stream.write(extra[:20])
    first = read_native_thread(UUID, roots=roots)
    assert first["parseErrors"] == 1 and not first["complete"]
    with source.open("ab") as stream:
        stream.write(extra[20:] + b"\n")
    pinned = read_native_thread(UUID, roots=roots, snapshot=first["snapshot"])
    assert pinned["snapshotStable"] and not pinned["complete"]
    assert pinned["parseErrors"] == 1
    fresh = read_native_thread(UUID, roots=roots)
    assert fresh["complete"] and fresh["parseErrors"] == 0
    assert fresh["messageCount"] == 3
