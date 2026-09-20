#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from direct_actions import _run_tool
from direct_probe_core import ProbeError, _redact_text


def atomic_json(path: Path,data: dict) -> None:
    fd,tmp=tempfile.mkstemp(prefix=".result-",suffix=".json",dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as handle:
            json.dump(data,handle,sort_keys=True,separators=(",",":"))
            handle.write("\n");handle.flush();os.fsync(handle.fileno())
        os.replace(tmp,path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


async def main() -> int:
    if len(sys.argv)!=2: return 2
    spec_path=Path(sys.argv[1]).resolve(strict=True)
    spec=json.loads(spec_path.read_text(encoding="utf-8"))
    root=Path(spec["root"]).resolve(strict=True)
    try:
        result=await _run_tool(root,spec["action"])
        status="succeeded" if result.get("exit_code")==0 else "failed"
        payload={"status":status,"result":result}
    except ProbeError as exc:
        message,redacted=_redact_text(str(exc))
        payload={"status":"failed","error":{"category":exc.category,"message":message[:2000]},"redacted":redacted}
    except Exception as exc:
        message,redacted=_redact_text(f"{type(exc).__name__}: {exc}")
        payload={"status":"failed","error":{"category":"internal","message":message[:2000]},"redacted":redacted}
    atomic_json(spec_path.parent/"result.json",payload)
    return 0 if payload["status"]=="succeeded" else 1


if __name__=="__main__":
    raise SystemExit(asyncio.run(main()))
