#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from direct_actions import _run_tool
from direct_jobs import _atomic_json, source_state
from direct_probe_core import ProbeError, _redact_text


async def main() -> int:
    if len(sys.argv) != 2:
        return 2
    spec_path = Path(sys.argv[1]).resolve(strict=True)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    started = time.time()
    before = {}
    try:
        root = Path(spec["root"]).resolve(strict=True)
        before = await source_state(root)
        result = await _run_tool(root, spec["action"], log_dir=spec_path.parent)
        after = await source_state(root)
        payload = {"status": "succeeded" if result.get("exit_code") == 0 else "failed", "result": result,
                   "source_before": before, "source_after": after,
                   "source_changed": before != after if before.get("complete") and after.get("complete") else None}
    except ProbeError as exc:
        message, redacted = _redact_text(str(exc))
        payload = {"status": "timed_out" if exc.category == "timeout" else "failed",
                   "error": {"category": exc.category, "message": message[:2000]}, "redacted": redacted,
                   "source_before": before}
        partial = getattr(exc, "partial_result", None)
        if partial is not None:
            stdout, _ = _redact_text(partial.stdout.decode("utf-8", "replace"))
            stderr, _ = _redact_text(partial.stderr.decode("utf-8", "replace"))
            payload["result"] = {"exit_code": partial.exit_code, "stdout": stdout, "stderr": stderr,
                                 "duration_ms": partial.duration_ms, "truncated": partial.truncated}
    except Exception as exc:
        message, redacted = _redact_text(f"{type(exc).__name__}: {exc}")
        payload = {"status": "failed", "error": {"category": "internal", "message": message[:2000]}, "redacted": redacted}
    payload.update(started_at=started, finished_at=time.time())
    _atomic_json(spec_path.parent / "result.json", payload)
    return 0 if payload["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
