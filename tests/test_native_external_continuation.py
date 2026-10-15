"""External VS Code continuation must retain native UUID, origin and safety policy."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from native_external_continuation import continue_external_native_thread

THREAD = "01a121f0-9d2b-7da0-9d06-cffd2c97e871"
HOME = "/home/dev"
PROJECT = "/home/dev/projects/street-story"


class History:
    def __init__(self, *, cwd=HOME, source="vscode", last="interrupted"):
        self.cwd = cwd
        self.source = source
        self.last = last
        self.calls = []
        self.model = "gpt-6.1-sol"
        self.effort = "high"
        self.resume_mismatch = False
        self.fail_start = False

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            return {"thread": {
                "id": THREAD, "cwd": self.cwd, "source": self.source,
                "model": self.model, "modelProvider": "openai",
                "reasoningEffort": self.effort,
                "turns": [{"id": "previous-turn", "status": self.last}],
            }}
        if method == "turn/start":
            if self.fail_start:
                raise TimeoutError("the RPC may have been accepted")
            return {"turn": {"id": "new-turn"}}
        raise AssertionError("unexpected RPC: " + method)

    async def ensure_thread_loaded(self, thread_id, metadata, *, force_resume=False):
        self.calls.append(("thread/resume", {"threadId": thread_id, **metadata}))
        assert force_resume is True
        return {
            "thread": {"id": thread_id if not self.resume_mismatch else "other"},
            "cwd": metadata["cwd"], "approvalPolicy": "never",
            "sandbox": {
                "type": "readOnly" if metadata["access"] == "read" else "workspaceWrite",
                "networkAccess": metadata["access"] == "write",
            },
            "activePermissionProfile": (
                {"id": "devcoveer-write"} if metadata["access"] == "write" else None
            ),
        }


def read_verified(thread_id, *, detail, include_tools):
    assert thread_id == THREAD and detail == "summary" and not include_tools
    return {
        "task": {"cwd": HOME, "source": "vscode"},
        "threadId": THREAD, "status": "ok",
    }


async def selection(_history, model, effort):
    assert model == "gpt-6.1-sol"
    assert effort == "high"
    return model, effort


def sandbox_matches(value, *, cwd, sandbox, permissions):
    return (
        value.get("cwd") == cwd
        and value.get("approvalPolicy") == "never"
        and value.get("sandbox", {}).get("type") ==
        ("readOnly" if sandbox == "read-only" else "workspaceWrite")
        and (permissions is None or value.get("activePermissionProfile", {}).get("id")
             == permissions)
    )


def model_matches(_value, *, requested_model, requested_effort):
    return requested_model, requested_effort


def sandbox_policy(sandbox, cwd):
    assert cwd == HOME
    return {"type": "readOnly" if sandbox == "read-only" else "workspaceWrite"}


def execute(history=None, *, args=None, requested_project=None, verified=read_verified):
    history = history or History()
    payload = {"task": f"codex://threads/{THREAD}", "prompt": "Resume the same thread."}
    payload.update(args or {})
    outcome = asyncio.run(continue_external_native_thread(
        thread_id=THREAD, arguments=payload, history=history,
        requested_project_cwd=requested_project,
        authorized_project_cwds={PROJECT},
        read_native=verified,
        resolve_model=selection,
        sandbox_matches=sandbox_matches,
        model_matches=model_matches,
        sandbox_policy=sandbox_policy,
        continuation_access=lambda a: a.get("access"),
        sandbox_by_access={"read": "read-only", "write": "workspace-write"},
        write_profile="devcoveer-write",
    ))
    return outcome, history.calls


def test_interrupted_vs_code_thread_continues_same_uuid_without_start_task():
    payload, calls = execute()
    assert payload["status"] == "running"
    assert payload["threadId"] == THREAD
    assert payload["action"] == "continued_existing_native_thread"
    assert payload["model"] == "gpt-6.1-sol"
    assert payload["reasoningEffort"] == "high"
    assert [name for name, _ in calls] == [
        "thread/read", "thread/resume", "turn/start",
    ]
    assert calls[-1][1]["threadId"] == THREAD
    assert calls[-1][1]["sandboxPolicy"]["type"] == "readOnly"
    assert not any(name == "thread/start" for name, _ in calls)


def test_write_upgrade_uses_restricted_named_permissions_no_arbitrary_sandbox():
    payload, calls = execute(args={"access": "write"}, requested_project=PROJECT)
    assert payload["status"] == "running"
    assert payload["access"] == "write"
    assert calls[1][1]["permissions"] == "devcoveer-write"
    assert "sandboxPolicy" not in calls[-1][1]
    assert calls[-1][1]["model"] == "gpt-6.1-sol"
    assert calls[-1][1]["effort"] == "high"


def test_project_cwd_mismatch_denied_before_resume():
    history = History(cwd="/untrusted/other")
    def verified(thread_id, *, detail, include_tools):
        return {"task": {"source": "vscode", "cwd": "/untrusted/other"}}
    payload, calls = execute(history, verified=verified)
    assert payload["status"] == "project_mismatch"
    assert calls == []


def test_requested_project_conflict_denied_without_new_turn():
    history = History(cwd=PROJECT)
    def verified(thread_id, *, detail, include_tools):
        return {"task": {"source": "vscode", "cwd": PROJECT}}
    payload, calls = execute(history, requested_project="/home/dev/projects/another",
                             verified=verified)
    assert payload["status"] == "project_mismatch"
    assert calls == []


def test_missing_or_untrusted_rollout_denied_before_rpc():
    class Missing(Exception):
        status = "not_found"

    def unavailable(thread_id, *, detail, include_tools):
        raise Missing()
    payload, calls = execute(verified=unavailable)
    assert payload["status"] == "not_found"
    assert calls == []


def test_active_vs_code_turn_never_creates_a_competing_turn():
    payload, calls = execute(History(last="inProgress"))
    assert payload["status"] == "already_running"
    assert [name for name, _ in calls] == ["thread/read"]


def test_resume_must_confirm_original_uuid():
    history = History()
    history.resume_mismatch = True
    payload, calls = execute(history)
    assert payload["status"] == "resume_failed"
    assert [name for name, _ in calls] == ["thread/read", "thread/resume"]


def test_lost_turn_start_reply_returns_unknown_without_retry():
    history = History()
    history.fail_start = True
    payload, calls = execute(history)
    assert payload["status"] == "outcome_unknown"
    assert [name for name, _ in calls].count("turn/start") == 1
    assert not any(name == "thread/start" for name, _ in calls)


def test_mcp_work_id_and_provider_restrictions_stay_in_place():
    for args in ({"provider": "opencode"}, {"work_id": "work_" + "a" * 24}):
        payload, calls = execute(args=args)
        assert payload["status"] == "conflict"
        assert calls == []


def test_canonical_bridge_includes_external_fallback_and_helper(tmp_path):
    import subprocess
    builder = ROOT / "scripts" / "build-clean-host-runtime.py"
    out = tmp_path / "runtime"
    completed = subprocess.run([sys.executable, str(builder), str(out)],
                               capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr
    compiled = (out / "codex-chatgpt-bridge.py").read_text()
    assert "_continue_external_native_thread(" in compiled
    assert "registered = _registry_task(arguments" in compiled
    assert "native_external_continuation.py" not in compiled
    assert (out / "native_external_continuation.py").exists()
    assert "Exact existing VS Code" in compiled
