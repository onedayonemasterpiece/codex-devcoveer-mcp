from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

import direct_probe_host_runtime as host_runtime  # noqa: E402
import direct_probe_host_services as host_services  # noqa: E402
import direct_probe_project_git as project_git  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_empty_listener_registration_is_never_reported_complete(monkeypatch):
    async def fake_show(_cfg):
        return {"unit": "opencode-devcoveer.service", "MainPID": "123"}

    monkeypatch.setattr(host_services, "_systemctl_show", fake_show)
    data = run(host_services._listeners({
        "unit": "opencode-devcoveer.service",
        "scope": "user",
        "ports": [],
        "transport": "loopback_http",
    }))
    assert data["listeners"] == []
    assert data["complete"] is False
    assert data["observability"] == "not_observable"
    assert data["transport"] == "loopback_http"


def test_unobservable_main_pid_does_not_claim_global_listener_ownership(monkeypatch):
    async def fake_show(_cfg):
        return {"unit": "fixture.service", "MainPID": "999999999"}

    monkeypatch.setattr(host_services, "_systemctl_show", fake_show)
    data = run(host_services._listeners({
        "unit": "fixture.service",
        "scope": "user",
        "ports": [4097],
        "transport": "loopback_http",
        "endpoints": ["http://127.0.0.1:4097"],
    }))
    assert data["listeners"] == []
    assert data["complete"] is False
    assert data["fd_scan_complete"] is False
    assert data["missing_registered_ports"] == [4097]


def test_canonical_control_plane_listener_endpoints_are_registered():
    cfg = json.loads((RUNTIME / "direct_probe_config.json").read_text(encoding="utf-8"))
    for project in ("codex-devcoveer-mcp", "vibepublish", "dataset-loop-mcp"):
        assert cfg["projects"][project]["executables"]["gh"] == "/usr/bin/gh"
    services = cfg["projects"]["codex-devcoveer-mcp"]["services"]
    assert services["opencode"]["ports"] == [4097]
    assert services["opencode"]["transport"] == "loopback_http"
    assert services["opencode"]["endpoints"] == ["http://127.0.0.1:4097"]
    assert services["mcp_tunnel"]["ports"] == [18080]
    assert services["mcp_tunnel"]["endpoints"] == ["http://127.0.0.1:18080"]


def test_runtime_file_distinguishes_loaded_snapshot_from_changed_disk(monkeypatch, tmp_path):
    path = tmp_path / "loaded.py"
    path.write_text("value = 1\n", encoding="utf-8")
    loaded_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    resolved = str(path.resolve())
    monkeypatch.setattr(host_runtime, "_PROCESS_RUNTIME_SNAPSHOT_NS", 123456789)
    monkeypatch.setattr(host_runtime, "_LOADED_MODULE_FINGERPRINTS", {
        resolved: {"module": "fixture_loaded", "sha256": loaded_sha}
    })
    binding = {"runtime_files": {"fixture": resolved}}

    before = run(host_runtime._runtime_file(binding, {"file": "fixture"}))
    assert before["loaded_process_sha256"] == loaded_sha
    assert before["disk_matches_loaded_process"] is True
    assert before["loaded_process_state"] == "matches_disk"

    path.write_text("value = 2\n", encoding="utf-8")
    after = run(host_runtime._runtime_file(binding, {"file": "fixture"}))
    assert after["sha256"] != loaded_sha
    assert after["loaded_process_sha256"] == loaded_sha
    assert after["disk_matches_loaded_process"] is False
    assert after["loaded_process_state"] == "disk_changed_after_process_import"




def test_runtime_modules_capture_bounded_startup_source_fingerprints():
    import direct_probes
    import source_handoff

    captured = host_runtime._capture_loaded_module_fingerprints()
    for module in (host_runtime, direct_probes, source_handoff):
        resolved = str(Path(module.__file__).resolve())
        assert captured[resolved]["sha256"] == hashlib.sha256(
            Path(module.__file__).read_bytes()
        ).hexdigest()

def test_remote_head_uses_verified_git_remote_not_gh(monkeypatch, tmp_path):
    remote_sha = "a" * 40
    tracking_sha = "b" * 40

    async def fake_git(_root, *args, **_kwargs):
        if args[:3] == ("remote", "get-url", "origin"):
            return SimpleNamespace(stdout=b"https://github.com/o/r.git\n", exit_code=0)
        if args[:3] == ("ls-remote", "--heads", "origin"):
            assert args[3] == "refs/heads/main"
            return SimpleNamespace(
                stdout=f"{remote_sha}\trefs/heads/main\n".encode(),
                exit_code=0,
            )
        if args[:3] == ("rev-parse", "--verify", "refs/remotes/origin/main"):
            return SimpleNamespace(stdout=(tracking_sha + "\n").encode(), exit_code=0)
        raise AssertionError(f"unexpected git invocation: {args!r}")

    async def forbidden_gh(*_args, **_kwargs):
        raise AssertionError("remote_head must not invoke gh")

    monkeypatch.setattr(project_git, "_git", fake_git)
    monkeypatch.setattr(project_git, "_gh", forbidden_gh)
    data = run(project_git._remote_head(
        tmp_path,
        {"remotes": {"origin": "o/r"}},
        "origin",
        "main",
    ))
    assert data["fresh_remote_sha"] == remote_sha
    assert data["local_tracking_sha"] == tracking_sha
    assert data["remote_url_verified"] is True


def test_source_routing_metadata_matches_ordered_runtime_fallback():
    source = (RUNTIME / "bridge_fragments" / "model_discovery_adaptive.py").read_text(
        encoding="utf-8"
    )
    assert '"automaticFallbackScope": "ordered_providers_for_automatic_tasks"' in source
    assert '"automaticFallbackScope": "groq_key_pool"' not in source


def _cancellation_fragment_env(record, inspected, *, registry_record=None):
    class FakeOpenCodeError(Exception):
        category = "unavailable"

    durable = record if registry_record is None else registry_record

    class FakeRegistry:
        def get(self, _task_id):
            return durable

        def update(self, _task_id, **changes):
            durable.update(changes)
            return durable

    class FakeEvidenceStore:
        def has_evidence(self, _task_id, _turn_id):
            return False

        def create_snapshot(self, *_args, **_kwargs):
            return {"safeSessionSha256": "e" * 64}

        def load_evidence(self, *_args, **_kwargs):
            return None

    class FakeBackend:
        async def inspect(self, **_kwargs):
            return dict(inspected)

    env = {
        "Any": object,
        "time": __import__("time"),
        "OpenCodeError": FakeOpenCodeError,
        "TASK_REGISTRY": FakeRegistry(),
        "EVIDENCE_STORE": FakeEvidenceStore(),
        "OPENCODE_BACKEND": FakeBackend(),
        "_registry_public": lambda value: dict(value),
    }
    source = (
        RUNTIME / "bridge_fragments" / "read_opencode_task_adaptive.py"
    ).read_text(encoding="utf-8")
    exec(compile(source, "<read-opencode-task-adaptive>", "exec"), env)
    return env


def test_cancelling_task_cannot_regress_to_running():
    record = {
        "taskId": "dvt_" + "a" * 32,
        "backend": "opencode",
        "backendSessionId": "ses_test",
        "cwd": "/repo",
        "provider": "opencode",
        "model": "big-pickle",
        "selection": "opencode/big-pickle",
        "access": "write",
        "status": "cancelling",
        "createdAt": 1,
    }
    inspected = {
        "status": "running",
        "sessionStatus": "busy",
        "providerState": {"type": "busy"},
        "finalResponse": None,
        "error": None,
        "messageId": "msg_live",
        "messages": [],
    }
    env = _cancellation_fragment_env(record, inspected)
    result = run(env["_read_opencode_task"](record))
    assert result["status"] == "running"
    assert record["status"] == "cancelling"


def test_cancelling_idle_provider_becomes_interrupted():
    record = {
        "taskId": "dvt_" + "b" * 32,
        "backend": "opencode",
        "backendSessionId": "ses_test",
        "cwd": "/repo",
        "provider": "opencode",
        "model": "big-pickle",
        "selection": "opencode/big-pickle",
        "access": "write",
        "status": "cancelling",
        "createdAt": 1,
    }
    inspected = {
        "status": "running",
        "sessionStatus": "idle",
        "providerState": {"type": "idle"},
        "finalResponse": None,
        "error": None,
        "messageId": "msg_cancelled",
        "messages": [],
    }
    env = _cancellation_fragment_env(record, inspected)
    result = run(env["_read_opencode_task"](record))
    assert result["status"] == "interrupted"
    assert result["executionStatus"] == "interrupted"
    assert record["status"] == "interrupted"
    assert record["acceptanceStatus"] == "review_required"


def test_cancelling_groq_task_never_auto_falls_back():
    record = {
        "taskId": "dvt_" + "c" * 32,
        "backend": "opencode",
        "backendSessionId": "ses_test",
        "cwd": "/repo",
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "selection": "groq/openai/gpt-oss-120b",
        "access": "write",
        "status": "cancelling",
    }
    inspected = {
        "providerErrorCategory": "rate_limited",
        "sessionStatus": "retry",
    }
    env = _cancellation_fragment_env(record, {
        "status": "running",
        "sessionStatus": "busy",
        "providerState": {"type": "busy"},
        "finalResponse": None,
        "error": None,
        "messageId": "msg_live",
        "messages": [],
    })
    result = run(env["_maybe_auto_groq_fallback"](record, inspected, status="running"))
    assert result is None

def test_stale_running_record_observes_fresh_cancelling_idle_state():
    stale = {
        "taskId": "dvt_" + "d" * 32,
        "backend": "opencode",
        "backendSessionId": "ses_test",
        "cwd": "/repo",
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "selection": "groq/openai/gpt-oss-120b",
        "access": "write",
        "status": "running",
        "createdAt": 1,
    }
    durable = {**stale, "status": "cancelling"}
    inspected = {
        "status": "running",
        "sessionStatus": "idle",
        "providerState": {"type": "idle"},
        "finalResponse": None,
        "error": None,
        "messageId": "msg_cancelled_race",
        "messages": [],
    }
    env = _cancellation_fragment_env(stale, inspected, registry_record=durable)
    result = run(env["_read_opencode_task"](stale))
    assert result["status"] == "interrupted"
    assert result["executionStatus"] == "interrupted"
    assert durable["status"] == "interrupted"
    assert stale["status"] == "running"


def test_stale_watcher_cannot_overwrite_fresh_cancelling_busy_state():
    stale = {
        "taskId": "dvt_" + "e" * 32,
        "backend": "opencode",
        "backendSessionId": "ses_test",
        "cwd": "/repo",
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "selection": "groq/openai/gpt-oss-120b",
        "access": "write",
        "status": "running",
        "createdAt": 1,
    }
    durable = {**stale, "status": "cancelling"}
    inspected = {
        "status": "running",
        "sessionStatus": "busy",
        "providerState": {"type": "busy"},
        "finalResponse": None,
        "error": None,
        "messageId": "msg_busy_race",
        "messages": [],
    }
    env = _cancellation_fragment_env(stale, inspected, registry_record=durable)
    result = run(env["_read_opencode_task"](stale))
    assert result["status"] == "running"
    assert result["task"]["status"] == "cancelling"
    assert durable["status"] == "cancelling"
    assert stale["status"] == "running"

