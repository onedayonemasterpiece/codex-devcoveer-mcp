"""Safe, read-only live acceptance for Codex Native deep links.

Print only counts, cursors and timings; never print session text, hidden
reasoning, credentials or tool output. Run via registered DevCoveer job.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from native_thread_links import read_native_thread

REF = "codex://threads/01a121f0-9d2b-7da0-9d06-cffd2c97e871"


def main() -> None:
    started = time.monotonic()
    summary = read_native_thread(REF, detail="summary")
    assert summary["status"] == "ok"
    assert summary["messageCount"] >= 200
    print(f"summary=PASS messages={summary['messageCount']} elapsed={time.monotonic() - started:.2f}s", flush=True)

    full = read_native_thread(REF, cursor=0, limit=100, include_tools=True)
    assert full["nextCursor"] is not None and full["toolEventCount"] > 1000
    assert [entry["index"] for entry in full["history"]] == list(range(full["nextCursor"]))
    print(f"first_full_page=PASS events={len(full['history'])} tools={full['toolEventCount']} next={full['nextCursor']} elapsed={time.monotonic() - started:.2f}s", flush=True)

    # Test continuity across a byte-budget page boundary and near the end
    # of the full mixed conversation+tools history.
    cursor = full["nextCursor"]
    second = read_native_thread(REF, cursor=cursor, limit=100, include_tools=True)
    assert second["history"]
    assert [entry["index"] for entry in second["history"]] == list(
        range(cursor, cursor + len(second["history"]))
    )
    tail_start = max(0, full["visibleChunkCount"] - 12)
    tail = read_native_thread(REF, cursor=tail_start, limit=100, include_tools=True)
    assert tail["nextCursor"] is None and tail["complete"]
    assert [entry["index"] for entry in tail["history"]] == list(
        range(tail_start, full["visibleChunkCount"])
    )
    print(f"mixed_pagination=PASS first={cursor} second={len(second['history'])} tail={len(tail['history'])} visible={full['visibleChunkCount']} elapsed={time.monotonic() - started:.2f}s", flush=True)

    cursor = 0
    all_messages = 0
    pages = 0
    while True:
        response = read_native_thread(REF, cursor=cursor, limit=90, include_tools=False)
        pages += 1
        all_messages += len(response["history"])
        cursor = response["nextCursor"]
        if cursor is None:
            assert response["complete"], "Source changed during final snapshot"
            assert response["messageCount"] == summary["messageCount"]
            assert all_messages == response["visibleChunkCount"]
            print(f"complete_conversation=PASS pages={pages} chunks={all_messages} messages={response['messageCount']} elapsed={time.monotonic() - started:.2f}s", flush=True)
            break
        assert pages < 30, "Unbounded pagination"
        print(f"page={pages} next={cursor} elapsed={time.monotonic() - started:.2f}s", flush=True)


if __name__ == "__main__":
    main()
