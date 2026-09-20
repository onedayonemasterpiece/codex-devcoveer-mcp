from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from jsonschema import Draft202012Validator
import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))

import direct_actions as da  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    })
    return subprocess.check_output(
        ["/usr/bin/git", "-C", str(cwd), *args],
        env=env, text=True,
    ).strip()


def run(coro):
    return asyncio.run(coro)


def write_cfg(path: Path, root: Path, *, project_actions=None, services=None) -> Path:
    payload = {
        "version": 1,
        "projects": {
            "p": {
                "github_repo": "o/r",
                "remotes": {"origin": "o/r"},
                "services": services or {},
                "logs": {},
                "roots": {"source": str(root)},
                "health": {},
                "runtimes": {},
                "runtime_files": {},
                "config_presence": {},
                "project_actions": project_actions or {},
            }
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.check_call(["/usr/bin/git", "init", "-q", "-b", "main", str(path)])
    git(path, "config", "user.name", "T")
    git(path, "config", "user.email", "t@example.invalid")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "base")
    return path


def test_action_schemas_are_typed_and_do_not_expose_shell_surface():
    p = Draft202012Validator(da.PROJECT_ACTION_INPUT_SCHEMA)
    h = Draft202012Validator(da.HOST_ACTION_INPUT_SCHEMA)
    assert list(p.iter_errors({
        "project": "p",
        "action": {"kind": "run_profile", "profile": "tests", "command": "rm -rf /"},
    }))
    assert list(p.iter_errors({
        "project": "p",
        "action": {"kind": "run_tool", "tool": "pytest", "executable": "/bin/sh"},
    }))
    assert list(p.iter_errors({
        "project": "p",
        "action": {"kind": "run_tool", "tool": "pytest", "env": {"TOKEN": "x"}},
    }))
    assert list(p.iter_errors({
        "project": "p",
        "action": {"kind": "sync_ff", "remote": "origin", "branch": "main",
                   "expected_sha": "0" * 40, "cwd": "/tmp"},
    }))
    assert list(h.iter_errors({
        "project": "p",
        "action": {"kind": "restart_service", "service": "browser", "unit": "evil.service"},
    }))


def test_run_tool_is_project_scoped_and_has_fixed_executable(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    cfg = write_cfg(tmp_path / "cfg.json", repo)
    observed = {}

    async def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(
            stdout=b"TOOL_OK\n", stderr=b"", exit_code=0,
            truncated=False, duration_ms=2,
        )

    monkeypatch.setattr(da, "_run_process", fake_run)
    result = run(da.run_project_action(
        str(repo), "p",
        {"kind": "run_tool", "tool": "pytest",
         "args": ["-q", "tests/test_example.py"], "timeout_seconds": 30},
        config_path=cfg,
    ))
    assert result["status"] == "ok"
    assert observed["argv"][:3] == ["/usr/bin/python3", "-m", "pytest"]
    assert observed["argv"][3:] == ["-q", "tests/test_example.py"]
    assert observed["cwd"] == repo.resolve()

    script = repo / "scripts" / "check.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    result = run(da.run_project_action(
        str(repo), "p",
        {"kind": "run_tool", "tool": "bash_script", "path": "scripts/check.sh"},
        config_path=cfg,
    ))
    assert result["status"] == "ok"
    assert observed["argv"][:2] == ["/usr/bin/bash", str(script.resolve())]

    with pytest.raises(da.ProbeError):
        run(da.run_project_action(
            str(repo), "p",
            {"kind": "run_tool", "tool": "bash_script", "path": "../escape.sh"},
            config_path=cfg,
        ))


def test_run_profile_executes_only_registered_argv(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    cfg = write_cfg(tmp_path / "cfg.json", repo, project_actions={
        "tests": {
            "argv": [sys.executable, "-c", "print('PROFILE_OK')"],
            "cwd": ".",
            "timeout_seconds": 10,
            "output_bytes": 4096,
            "allow_handoff": True,
        }
    })
    result = run(da.run_project_action(
        str(repo), "p", {"kind": "run_profile", "profile": "tests"}, config_path=cfg,
    ))
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert "PROFILE_OK" in result["stdout"]
    with pytest.raises(da.ProbeError, match="not registered"):
        run(da.run_project_action(
            str(repo), "p", {"kind": "run_profile", "profile": "other"}, config_path=cfg,
        ))


def test_sync_ff_preserves_untracked_and_requires_expected_remote_sha(tmp_path: Path, monkeypatch):
    seed = init_repo(tmp_path / "seed")
    bare = tmp_path / "remote.git"
    subprocess.check_call(["/usr/bin/git", "init", "-q", "--bare", str(bare)])
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "-u", "origin", "main")

    work = tmp_path / "work"
    subprocess.check_call(["/usr/bin/git", "clone", "-q", str(bare), str(work)])
    git(work, "config", "user.name", "T")
    git(work, "config", "user.email", "t@example.invalid")
    old = git(work, "rev-parse", "HEAD")
    (work / "keep.untracked").write_text("keep\n", encoding="utf-8")

    (seed / "a.txt").write_text("two\n", encoding="utf-8")
    git(seed, "add", "a.txt")
    git(seed, "commit", "-q", "-m", "next")
    expected = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "-q", "origin", "main")

    cfg = write_cfg(tmp_path / "cfg.json", work)
    monkeypatch.setattr(da, "_remote_repo", lambda _url: "o/r")
    result = run(da.run_project_action(
        str(work), "p",
        {"kind": "sync_ff", "remote": "origin", "branch": "main", "expected_sha": expected},
        config_path=cfg,
    ))
    assert result["status"] == "ok" and result["changed"] is True
    assert result["old_sha"] == old and result["new_sha"] == expected
    assert git(work, "rev-parse", "HEAD") == expected
    assert (work / "keep.untracked").read_text() == "keep\n"

    with pytest.raises(da.ProbeError, match="differs from expected"):
        run(da.run_project_action(
            str(work), "p",
            {"kind": "sync_ff", "remote": "origin", "branch": "main", "expected_sha": "f" * 40},
            config_path=cfg,
        ))


def test_handoff_profile_and_commit_push_need_no_agent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    handoff_id = "src_" + "1" * 24
    handoff_dir = tmp_path / ".local" / "share" / "openai-codex-mcp" / "handoffs" / handoff_id
    work = init_repo(handoff_dir / "worktree")
    base = git(work, "rev-parse", "HEAD")

    bare = tmp_path / "remote.git"
    subprocess.check_call(["/usr/bin/git", "init", "-q", "--bare", str(bare)])
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "origin", "main")

    meta = {
        "id": handoff_id,
        "projectId": "p",
        "projectRoot": str(tmp_path / "canonical"),
        "baseSha": base,
        "patchSha256": "2" * 64,
        "worktree": str(work.resolve()),
    }
    (handoff_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    cfg = write_cfg(tmp_path / "cfg.json", work, project_actions={
        "tests": {
            "argv": [sys.executable, "-c", "print('HANDOFF_TEST_OK')"],
            "cwd": ".",
            "timeout_seconds": 10,
            "output_bytes": 4096,
            "allow_handoff": True,
        }
    })

    tested = run(da.run_project_action(
        str(work), f"handoff:{handoff_id}",
        {"kind": "run_profile", "profile": "tests"}, config_path=cfg,
    ))
    assert tested["status"] == "ok" and "HANDOFF_TEST_OK" in tested["stdout"]

    (work / "a.txt").write_text("patched\n", encoding="utf-8")
    git(work, "add", "a.txt")
    monkeypatch.setattr(da, "_remote_repo", lambda _url: "o/r")
    pushed = run(da.run_project_action(
        str(work), f"handoff:{handoff_id}",
        {"kind": "handoff_commit_push", "remote": "origin",
         "branch": "chatgpt/direct-test", "commit_message": "Direct handoff commit"},
        config_path=cfg,
    ))
    assert pushed["status"] == "ok"
    remote_sha = subprocess.check_output(
        ["/usr/bin/git", "--git-dir", str(bare), "rev-parse", "refs/heads/chatgpt/direct-test"],
        text=True,
    ).strip()
    assert remote_sha == pushed["commit_sha"]


def test_restart_service_is_deferred_and_allowlisted(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    cfg = write_cfg(tmp_path / "cfg.json", repo, services={
        "mcp_tunnel": {"unit": "codex-devserver-tunnel.service", "scope": "user"}
    })
    observed = {}

    async def fake_run(argv, **kwargs):
        observed["argv"] = argv
        return SimpleNamespace(stdout=b"", stderr=b"", exit_code=0, truncated=False, duration_ms=1)

    monkeypatch.setattr(da, "_run_process", fake_run)
    result = run(da.run_host_action(
        str(repo), "p", {"kind": "restart_service", "service": "mcp_tunnel"},
        config_path=cfg,
    ))
    assert result["status"] == "ok"
    assert result["scheduled_after_seconds"] == 1
    assert "/usr/bin/systemd-run" == observed["argv"][0]
    assert "codex-devserver-tunnel.service" in observed["argv"]
    with pytest.raises(da.ProbeError, match="not registered"):
        run(da.run_host_action(
            str(repo), "p", {"kind": "restart_service", "service": "other"},
            config_path=cfg,
        ))
