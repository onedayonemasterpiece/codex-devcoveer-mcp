"""Focused regressions; provider/systemd effects are mocked except isolated subprocess tests."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import direct_actions as actions
import direct_github_actions as github
import direct_jobs as jobs
import direct_ops_gateway as gateway
import direct_probe_core as core


def run(coro):
    return asyncio.run(coro)


def result(stdout=b"", code=0):
    return SimpleNamespace(exit_code=code, stdout=stdout, stderr=b"", truncated=False, duration_ms=1)


def fake_venv(path):
    (path / "bin").mkdir(parents=True)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (path / "bin/python").symlink_to(sys.executable)
    return str(path / "bin/python")


def test_describe_returns_live_payload_not_another_envelope(tmp_path):
    data = run(gateway.run_direct_ops_v2(str(tmp_path), "p", "project_action", "describe", {"operation": "job_start"}))
    assert "request_key" in data["payload_schema"]["properties"]
    assert "python_env" in data["payload_schema"]["properties"]
    assert "kind" not in data["payload_schema"]["required"]
    assert "kind" not in data["payload_schema"]["properties"]
    assert {"git_fetch", "github_workflow_dispatch", "github_run_cancel", "job_list"} <= set(data["operations"])


def test_schema_digest_changes_without_renaming_tool(monkeypatch):
    before = gateway.describe_plane("project_action")["schema_digest"]
    changed = copy.deepcopy(gateway.PROJECT_ACTION_INPUT_SCHEMA)
    changed["properties"]["project"]["maxLength"] += 1
    monkeypatch.setattr(gateway, "PROJECT_ACTION_INPUT_SCHEMA", changed)
    assert gateway.describe_plane("project_action")["schema_digest"] != before


def test_describe_cannot_execute_or_override_reserved_fields(tmp_path):
    with pytest.raises(core.ProbeError):
        run(gateway.run_direct_ops_v2(str(tmp_path), "p", "project_action", "describe", {"kind": "job_start"}))


def test_python_auto_prefers_cwd_venv_and_explicit_system(tmp_path):
    fake_venv(tmp_path / ".venv")
    backend = tmp_path / "backend"
    expected = fake_venv(backend / ".venv")
    assert actions._project_python(tmp_path, backend) == expected
    assert actions._project_python(tmp_path, backend, "system") == "/usr/bin/python3"
    assert actions._project_python(tmp_path, backend, ".venv") == str(tmp_path / ".venv/bin/python")


def test_python_env_does_not_escape_project(tmp_path):
    with pytest.raises(core.ProbeError):
        actions._project_python(tmp_path, tmp_path, "../outside")
    (tmp_path / ".venv").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(core.ProbeError):
        actions._project_python(tmp_path, tmp_path)


def test_pytest_receipt_and_environment_use_project_python(tmp_path, monkeypatch):
    python = fake_venv(tmp_path / ".venv")
    seen = {}
    async def process(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return result(b"1 passed\n")
    monkeypatch.setattr(actions, "_run_process", process)
    data = run(actions._run_tool(tmp_path, {"tool": "pytest", "args": ["-q"], "env": {"CI": "1"}}))
    assert seen["argv"] == [python, "-m", "pytest", "-q"]
    assert seen["env"]["PATH"].startswith(str(tmp_path / ".venv/bin"))
    assert seen["env"]["PYTHONUNBUFFERED"] == "1"
    assert data["executable"] == python and data["output_mode"] == "tail"


def test_secret_environment_override_rejected():
    with pytest.raises(core.ProbeError):
        actions._tool_env({"env": {"API_KEY": "dummy"}}, None)


def test_execution_tail_keeps_summary_and_probe_prefix_stays_unchanged(tmp_path):
    code = "print('noise\\n'*300);print('FINAL SUMMARY')"
    tail = run(core._run_process([sys.executable, "-c", code], output_limit=64, capture_tail=True, log_dir=tmp_path))
    head = run(core._run_process([sys.executable, "-c", code], output_limit=64))
    assert tail.truncated and b"FINAL SUMMARY" in tail.stdout
    assert b"FINAL SUMMARY" not in head.stdout
    assert "FINAL SUMMARY" in (tmp_path / "stdout.log").read_text()


def test_timeout_preserves_last_output_and_log(tmp_path):
    code = "import time,sys;print('CHECKPOINT',flush=True);print('ERROR DETAIL',file=sys.stderr,flush=True);time.sleep(5)"
    with pytest.raises(core.ProbeError) as failure:
        run(core._run_process([sys.executable, "-c", code], timeout=.5, capture_tail=True, log_dir=tmp_path))
    assert failure.value.category == "timeout"
    assert b"CHECKPOINT" in failure.value.partial_result.stdout
    assert b"ERROR DETAIL" in failure.value.partial_result.stderr
    assert "CHECKPOINT" in (tmp_path / "stdout.log").read_text()


def test_timeout_covers_child_pipes_after_parent_exit():
    code = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']);print('CHILD STARTED',flush=True)"
    with pytest.raises(core.ProbeError) as failure:
        run(core._run_process([sys.executable, "-c", code], timeout=.5, capture_tail=True))
    assert failure.value.category == "timeout"
    assert b"CHILD STARTED" in failure.value.partial_result.stdout


def test_log_redaction_spans_pipe_chunks(tmp_path):
    token = "ghp_" + "a" * 36
    async def exercise():
        stream = asyncio.StreamReader()
        reader = asyncio.create_task(core._read_limited(stream, 64, None, tail=True, log_path=tmp_path / "stdout.log"))
        stream.feed_data(("token=" + token[:10]).encode())
        await asyncio.sleep(0)
        stream.feed_data((token[10:] + "\nDONE\n").encode())
        stream.feed_eof()
        return await reader
    output, _ = run(exercise())
    assert token not in output.decode()
    assert token not in (tmp_path / "stdout.log").read_text()
    assert token not in core._redact_text(token)[0]
    assert b"DONE" in output


def prepare_job(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOB_ROOT", tmp_path / "jobs")
    calls = []
    async def process(argv, **kwargs):
        calls.append(argv)
        return result(b"LoadState=loaded\nActiveState=active\n" if "systemctl" in argv[0] else b"")
    monkeypatch.setattr(jobs, "_run_process", process)
    action = {"tool": "pytest", "args": ["-q"], "request_key": "repeatable"}
    started = run(jobs.start_job(tmp_path, action))
    return started, action, calls


def test_identical_job_retry_and_cleaned_retry_never_relaunch(tmp_path, monkeypatch):
    started, action, calls = prepare_job(tmp_path, monkeypatch)
    duplicate = run(jobs.start_job(tmp_path, action))
    assert duplicate["job_id"] == started["job_id"] and duplicate["reused"]
    directory = jobs._job_dir(started["job_id"])
    jobs._atomic_json(directory / "result.json", {"status": "succeeded", "result": {"exit_code": 0}})
    (directory / "stdout.log").write_text("log")
    assert jobs.cleanup_job(tmp_path, started["job_id"])["receipt_retained"]
    replay = run(jobs.start_job(tmp_path, action))
    assert replay["cleaned"] and replay["status"] == "succeeded"
    assert sum("systemd-run" in argv[0] for argv in calls) == 1
    assert not (directory / "stdout.log").exists()


def test_same_key_different_command_is_conflict(tmp_path, monkeypatch):
    _started, action, calls = prepare_job(tmp_path, monkeypatch)
    with pytest.raises(core.ProbeError) as failure:
        run(jobs.start_job(tmp_path, {**action, "args": ["-x"]}))
    assert failure.value.category == "idempotency_conflict"
    assert len(calls) == 1


def test_dead_job_gets_terminal_receipt_instead_of_pending_forever(tmp_path, monkeypatch):
    started, _action, _calls = prepare_job(tmp_path, monkeypatch)
    directory = jobs._job_dir(started["job_id"])
    spec = jobs._read_json(directory / "spec.json")
    jobs._atomic_json(directory / "spec.json", {**spec, "created_at": 0})
    async def gone(*args, **kwargs):
        return result(b"LoadState=not-found\nActiveState=inactive\n")
    monkeypatch.setattr(jobs, "_run_process", gone)
    data = run(jobs.job_status(tmp_path, started["job_id"]))
    assert data["status"] == "interrupted" and data["error"]["category"] == "result_missing"
    assert (directory / "result.json").exists()


def test_failed_status_transport_does_not_invent_terminal_result(tmp_path, monkeypatch):
    started, _action, _calls = prepare_job(tmp_path, monkeypatch)
    async def unavailable(*args, **kwargs):
        return result(code=1)
    monkeypatch.setattr(jobs, "_run_process", unavailable)
    data = run(jobs.job_status(tmp_path, started["job_id"]))
    assert data["status"] == "unknown" and data["retryable"]
    assert not (jobs._job_dir(started["job_id"]) / "result.json").exists()


def test_job_list_paginates_and_does_not_poll_units(tmp_path, monkeypatch):
    _started, action, calls = prepare_job(tmp_path, monkeypatch)
    run(jobs.start_job(tmp_path, {**action, "request_key": "another"}))
    first = jobs.list_jobs(tmp_path, limit=1)
    second = jobs.list_jobs(tmp_path, limit=1, before=first["next_cursor"])
    assert first["jobs"][0]["job_id"] != second["jobs"][0]["job_id"]
    assert len(calls) == 2


def test_cancel_finish_race_preserves_success(tmp_path, monkeypatch):
    started, _action, _calls = prepare_job(tmp_path, monkeypatch)
    directory = jobs._job_dir(started["job_id"])
    async def finished(*args, **kwargs):
        jobs._atomic_json(directory / "result.json", {"status": "succeeded"})
        return result()
    monkeypatch.setattr(jobs, "_run_process", finished)
    assert run(jobs.cancel_job(tmp_path, started["job_id"]))["status"] == "terminal"
    assert jobs._read_json(directory / "result.json")["status"] == "succeeded"


def test_missing_result_receipt_does_not_replace_runner_result(tmp_path):
    path = tmp_path / "result.json"
    jobs._atomic_json(path, {"status": "succeeded", "result": {"exit_code": 0}})
    assert jobs._atomic_json_if_absent(path, {"status": "interrupted"}) is False
    assert jobs._read_json(path)["status"] == "succeeded"


def mock_repo(monkeypatch):
    async def repo(*args):
        return "origin", "owner/repo"
    monkeypatch.setattr(github, "_repo", repo)


def test_git_fetch_has_no_checkout_reset_or_force(tmp_path, monkeypatch):
    mock_repo(monkeypatch)
    calls = []
    async def git(root, *args, **kwargs):
        calls.append(args)
        return result(("a" * 40 + "\n").encode())
    monkeypatch.setattr(github, "_git", git)
    data = run(github.git_fetch(tmp_path, {}, {"branch": "main"}))
    assert calls[0][0] == "fetch"
    assert not any(word in {"reset", "checkout", "--force", "--prune"} for row in calls for word in row)
    assert data["checkout_updated"] is False


def test_dispatch_uses_provider_run_id_and_literal_string_inputs(tmp_path, monkeypatch):
    mock_repo(monkeypatch)
    calls = []
    async def gh(binding, args, **kwargs):
        calls.append(args)
        return json.dumps({"workflow_run_id": 123})
    monkeypatch.setattr(github, "_gh", gh)
    data = run(github.workflow_dispatch(tmp_path, {}, {"workflow": "test.yml", "ref": "main", "inputs": {"note": "@not-a-file"}}))
    assert data["run_id"] == 123 and data["receipt_complete"]
    index = calls[0].index("inputs[note]=@not-a-file")
    assert calls[0][index - 1] == "-f" and len(calls) == 1


def test_empty_dispatch_receipt_is_not_guessed_or_retried(tmp_path, monkeypatch):
    mock_repo(monkeypatch)
    calls = []
    async def gh(*args, **kwargs):
        calls.append(args)
        return ""
    monkeypatch.setattr(github, "_gh", gh)
    data = run(github.workflow_dispatch(tmp_path, {}, {"workflow": "test.yml", "ref": "main"}))
    assert data["run_id"] is None and not data["automatic_retry_safe"] and len(calls) == 1


def test_cancel_rejects_other_sha_before_write(tmp_path, monkeypatch):
    mock_repo(monkeypatch)
    calls = []
    async def gh(binding, args, **kwargs):
        calls.append(args)
        return json.dumps({"head_sha": "b" * 40, "status": "in_progress"})
    monkeypatch.setattr(github, "_gh", gh)
    with pytest.raises(core.ProbeError):
        run(github.run_cancel(tmp_path, {}, {"run_id": 123, "expected_head_sha": "a" * 40}))
    assert len(calls) == 1 and "POST" not in calls[0]
