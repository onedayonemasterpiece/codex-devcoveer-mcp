#!/usr/bin/env python3
"""Sanitized read-only diagnostics for VibePublish CodexTaskImagegen runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import time

DB = Path("/home/dev/.local/state/vibepublish/vibepublish.sqlite3")
TASK_ROOT = Path("/home/dev/.local/state/vibepublish/imagegen-tasks")
ARTIFACT_ROOT = Path("/home/dev/.local/state/vibepublish/imagegen")
CODEX = Path("/home/dev/.local/bin/codex")
CODEX_HOME = Path("/home/dev/.codex")
SKILL = CODEX_HOME / "skills/.system/imagegen/SKILL.md"


def safe_json(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except Exception:
        return {"malformed": True}


def dir_status(path: Path):
    try:
        st = path.stat()
        return {
            "exists": True,
            "is_dir": stat.S_ISDIR(st.st_mode),
            "mode": oct(stat.S_IMODE(st.st_mode)),
            "uid": st.st_uid,
            "writable": os.access(path, os.W_OK),
        }
    except FileNotFoundError:
        return {"exists": False}


def receipt_status(job_id: str):
    path = TASK_ROOT / job_id / "receipt.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"present": False}
    except Exception as exc:
        return {"present": True, "read_error": type(exc).__name__}
    frames = raw.get("last_observation_error")
    return {
        "present": True,
        "job_key": raw.get("job_key"),
        "input_digest": raw.get("input_digest"),
        "state": raw.get("state"),
        "phase": raw.get("phase"),
        "thread_id": raw.get("thread_id"),
        "turn_id": raw.get("turn_id"),
        "task_model": raw.get("task_model"),
        "actual_model": raw.get("actual_model"),
        "deadline": raw.get("deadline"),
        "artifact_count": len(raw.get("artifacts") or []),
        "native_image_item_count": len(raw.get("native_image_items") or []),
        "last_observation_error": frames,
    }


def main():
    out = {
        "schema_version": 1,
        "observed_at": time.time(),
        "codex_cli": None,
        "roots": {
            "task_root": dir_status(TASK_ROOT),
            "artifact_root": dir_status(ARTIFACT_ROOT),
            "codex_home": dir_status(CODEX_HOME),
        },
        "skill": {
            "exists": SKILL.is_file(),
            "size": SKILL.stat().st_size if SKILL.is_file() else None,
        },
        "recent_visual_operations": [],
        "recent_boundary_operations": [],
        "principal_scopes": [],
    }
    try:
        proc = subprocess.run(
            [str(CODEX), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        out["codex_cli"] = {
            "returncode": proc.returncode,
            "version": proc.stdout.strip()[:160],
        }
    except Exception as exc:
        out["codex_cli"] = {"error": type(exc).__name__}

    uri = f"file:{DB}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    principal_rows = db.execute(
        "SELECT id,scopes,owner,active FROM principals ORDER BY id"
    ).fetchall()
    for principal in principal_rows:
        scopes = safe_json(principal["scopes"])
        out["principal_scopes"].append({
            "principal_id": principal["id"],
            "scopes": sorted(scopes) if isinstance(scopes, list) else [],
            "owner": bool(principal["owner"]),
            "active": bool(principal["active"]),
        })
    boundary_rows = db.execute(
        """
        SELECT id,created,action,state,complete,work_state,error
          FROM operations
         WHERE action IN ('asset_ingress','visual')
         ORDER BY created DESC
         LIMIT 20
        """
    ).fetchall()
    for op in boundary_rows:
        out["recent_boundary_operations"].append({
            "operation_id": op["id"],
            "created": op["created"],
            "action": op["action"],
            "state": op["state"],
            "complete": bool(op["complete"]),
            "work_state": op["work_state"],
            "error": safe_json(op["error"]),
        })

    rows = db.execute(
        """
        SELECT o.id AS operation_id, o.created, o.state AS operation_state,
               o.complete, o.work_state, o.error,
               v.id AS job_id, v.state AS job_state, v.dispatched,
               v.execution_ref, v.deadline, v.input_digest
          FROM visual_jobs v
          JOIN operations o ON o.id = v.operation_id
         ORDER BY o.created DESC
         LIMIT 12
        """
    ).fetchall()
    db.close()
    for row in rows:
        error = safe_json(row["error"])
        item = {
            "operation_id": row["operation_id"],
            "created": row["created"],
            "operation_state": row["operation_state"],
            "complete": bool(row["complete"]),
            "work_state": row["work_state"],
            "error": error,
            "job_id": row["job_id"],
            "job_state": row["job_state"],
            "dispatched": bool(row["dispatched"]),
            "execution_ref": row["execution_ref"],
            "deadline": row["deadline"],
            "input_digest": row["input_digest"],
            "receipt": receipt_status(row["job_id"]),
        }
        out["recent_visual_operations"].append(item)

    print(json.dumps(out, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
