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

    full = read_native_thread(REF, cursor=0, limit=20, include_tools=True)
    assert full["nextCursor"] is not None and full["toolEventCount"] > 1000
    print(f"first_full_page=PASS events={len(full['history'])} tools={full['toolEventCount']} next={full['nextCursor']} elapsed={time.monotonic() - started:.2f}s", flush=True)

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
