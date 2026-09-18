from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import threading
import time

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))
import direct_probes as dp
import direct_probe_project_git as project_git
import direct_probe_project_github as project_github


def git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME":"T", "GIT_AUTHOR_EMAIL":"t@example.invalid", "GIT_COMMITTER_NAME":"T", "GIT_COMMITTER_EMAIL":"t@example.invalid"})
    return subprocess.check_output(["/usr/bin/git", "-C", str(repo), *args], env=env, text=True).strip()


def init_repo(path: Path, *, commit: bool = True) -> Path:
    path.mkdir()
    subprocess.check_call(["/usr/bin/git", "init", "-q", "-b", "main", str(path)])
    git(path, "config", "user.name", "T")
    git(path, "config", "user.email", "t@example.invalid")
    if commit:
        (path / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        git(path, "add", "a.txt")
        git(path, "commit", "-q", "-m", "init")
    return path


def write_cfg(path: Path, project: str, root: Path, extra: dict | None = None) -> Path:
    binding = {"github_repo": "o/r", "remotes": {"origin": "o/r"}, "executables": {"gh": "/usr/bin/false"},
               "services": {}, "logs": {}, "roots": {"source": str(root)}, "health": {}, "runtimes": {},
               "runtime_files": {}, "config_presence": {}}
    if extra:
        binding.update(extra)
    cfg = {"version": 1, "projects": {project: binding}}
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def run(coro):
    return asyncio.run(coro)


def result_by_id(payload, ident):
    return next(x for x in payload["results"] if x["id"] == ident)


def test_schemas_are_closed_and_do_not_expose_command_surface():
    validator = Draft202012Validator(dp.PROJECT_PROBE_INPUT_SCHEMA)
    bad = {"project":"p", "checks":[{"id":"x", "kind":"git_state", "command":"rm -rf /"}]}
    assert list(validator.iter_errors(bad))
    bad2 = {"project":"p", "checks":[{"id":"x", "kind":"read_text", "path":"a", "cwd":"/tmp", "env":{"X":"Y"}}]}
    assert list(validator.iter_errors(bad2))
    hvalidator = Draft202012Validator(dp.HOST_PROBE_INPUT_SCHEMA)
    assert list(hvalidator.iter_errors({"project":"p", "checks":[{"id":"x", "kind":"health", "endpoint":"x", "url":"http://x"}]}))


def test_git_state_clean_dirty_staged_untracked_and_conflicts(tmp_path: Path):
    repo = init_repo(tmp_path / "r")
    clean = run(dp._git_state(repo))
    assert clean["clean"] is True and clean["scan_complete"] is True and clean["head_state"] == "branch"
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    dirty = run(dp._git_state(repo))
    assert dirty["clean"] is False and dirty["unstaged"] == ["a.txt"]
    git(repo, "add", "a.txt")
    staged = run(dp._git_state(repo))
    assert "a.txt" in staged["staged"] and not staged["unstaged"]
    (repo / "u.txt").write_text("u", encoding="utf-8")
    untracked = run(dp._git_state(repo))
    assert "u.txt" in untracked["untracked"]
    git(repo, "commit", "-q", "-am", "main-change")
    git(repo, "checkout", "-q", "-b", "topic", "HEAD~1")
    (repo / "a.txt").write_text("topic\n", encoding="utf-8")
    git(repo, "add", "a.txt"); git(repo, "commit", "-q", "-m", "topic-change")
    git(repo, "checkout", "-q", "main")
    env = os.environ.copy(); env.update({"GIT_AUTHOR_NAME":"T", "GIT_AUTHOR_EMAIL":"t@example.invalid", "GIT_COMMITTER_NAME":"T", "GIT_COMMITTER_EMAIL":"t@example.invalid"})
    merged = subprocess.run(["/usr/bin/git", "-C", str(repo), "merge", "--no-edit", "topic"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert merged.returncode != 0
    conflict = run(dp._git_state(repo))
    assert "a.txt" in conflict["conflicts"] and conflict["clean"] is False


def test_git_state_detached_and_unborn(tmp_path: Path):
    unborn = init_repo(tmp_path / "unborn", commit=False)
    state = run(dp._git_state(unborn))
    assert state["head_state"] == "unborn" and state["head"] is None
    repo = init_repo(tmp_path / "detached")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--detach", sha)
    state = run(dp._git_state(repo))
    assert state["head_state"] == "detached" and state["branch"] is None


def test_worktree_list(tmp_path: Path):
    repo = init_repo(tmp_path / "r")
    git(repo, "branch", "second")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "second")
    data = run(dp._worktree_list(repo))
    assert len(data["worktrees"]) >= 2


def test_search_literal_untracked_no_matches_and_prunes_dependency_dirs(tmp_path: Path):
    repo = init_repo(tmp_path / "r")
    (repo / "u.txt").write_text("needle literal.* here\n", encoding="utf-8")
    (repo / "node_modules").mkdir(); (repo / "node_modules" / "bad.txt").write_text("needle\n", encoding="utf-8")
    data = run(dp._search(repo, {"query":"literal.*", "mode":"content", "limit":20}))
    assert [m["path"] for m in data["matches"]] == ["u.txt"]
    no = run(dp._search(repo, {"query":"definitely absent", "mode":"content", "limit":20}))
    assert no["confirmed_no_matches"] is True and no["scan_complete"] is True


def test_path_secret_symlink_fifo_and_option_injection_denied(tmp_path: Path):
    repo = init_repo(tmp_path / "r")
    outside = tmp_path / "outside.txt"; outside.write_text("secret", encoding="utf-8")
    (repo / "link.txt").symlink_to(outside)
    fifo = repo / "pipe"; os.mkfifo(fifo)
    for raw in ("../outside.txt", ".env", "secrets/x", "-bad"):
        with pytest.raises(dp.ProbeError): dp._safe_worktree_path(repo, raw, require_file=False)
    with pytest.raises(dp.ProbeError): dp._safe_worktree_path(repo, "link.txt", require_file=True)
    with pytest.raises(dp.ProbeError): dp._safe_worktree_path(repo, "pipe")
    with pytest.raises(dp.ProbeError): dp._validate_ref("--help")


def test_secret_in_history_and_diff_is_denied(tmp_path: Path):
    repo = init_repo(tmp_path / "r")
    (repo / ".env.old").write_text("TOKEN=abc", encoding="utf-8")
    git(repo, "add", ".env.old"); git(repo, "commit", "-q", "-m", "secret fixture")
    secret_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "-q", ".env.old"); git(repo, "commit", "-q", "-m", "remove")
    with pytest.raises(dp.ProbeError) as exc: run(dp._git_blob(repo, secret_commit, ".env.old"))
    assert exc.value.category == "secret_path"
    parent = git(repo, "rev-parse", f"{secret_commit}^")
    with pytest.raises(dp.ProbeError) as exc: run(dp._diff(repo, {"base":parent, "target":secret_commit, "context":1}))
    assert exc.value.category == "secret_path"


def test_diff_external_helper_not_executed(tmp_path: Path):
    repo = init_repo(tmp_path / "r")
    marker = tmp_path / "owned"; helper = tmp_path / "helper.sh"
    helper.write_text(f"#!/bin/sh\necho x > {marker}\n", encoding="utf-8"); helper.chmod(0o755)
    git(repo, "config", "diff.external", str(helper)); (repo / "a.txt").write_text("delta\n", encoding="utf-8")
    data = run(dp._diff(repo, {"base":"HEAD", "target":"WORKTREE", "context":1}))
    assert "a.txt" in data["paths"] and not marker.exists()


def test_read_text_exact_versions_and_binary_denial(tmp_path: Path):
    repo = init_repo(tmp_path / "r"); head = git(repo, "rev-parse", "HEAD")
    data = run(dp._read_text(repo, {}, {"source":"local_commit", "ref":"HEAD", "path":"a.txt", "start_line":1,"end_line":1}))
    assert data["version"]["commit"] == head and data["text"] == "alpha"
    work = run(dp._read_text(repo, {}, {"source":"worktree", "path":"a.txt"}))
    assert len(work["version"]["sha256"]) == 64
    (repo / "bin.dat").write_bytes(b"a\x00b")
    with pytest.raises(dp.ProbeError) as exc: run(dp._read_text(repo, {}, {"source":"worktree", "path":"bin.dat"}))
    assert exc.value.category == "binary_denied"


def test_remote_head_distinguishes_tracking_from_fresh(monkeypatch, tmp_path: Path):
    repo = init_repo(tmp_path / "r"); old = git(repo, "rev-parse", "HEAD"); git(repo, "update-ref", "refs/remotes/origin/main", old)
    (repo / "a.txt").write_text("next", encoding="utf-8"); git(repo, "add", "a.txt"); git(repo, "commit", "-q", "-m", "next")
    fresh = git(repo, "rev-parse", "HEAD")
    async def fake_gh(binding, args, **kwargs): return dp.ProcessResult((fresh+"\n").encode(), b"", 0, False, 1)
    monkeypatch.setattr(project_git, "_gh", fake_gh)
    out = run(dp._remote_head(repo, {"github_repo":"o/r", "remotes":{"origin":"o/r"}}, "origin", "main"))
    assert out["fresh_remote_sha"] == fresh and out["local_tracking_sha"] == old and out["same_as_local_tracking"] is False


def test_partial_batch_preserves_prior_results_and_no_model_spies(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path / "r"); cfg = write_cfg(tmp_path / "cfg.json", "p", repo)
    class Bomb:
        touched = False
        def __getattr__(self, name): self.touched = True; raise AssertionError("model/task backend touched")
    model = Bomb(); registry = Bomb(); monkeypatch.setattr(dp, "OPENCODE_BACKEND", model, raising=False); monkeypatch.setattr(dp, "TASK_REGISTRY", registry, raising=False)
    payload = run(dp.run_project_probe(str(repo), "p", [{"id":"ok","kind":"file_stat","path":"a.txt"},{"id":"missing","kind":"file_stat","path":"no.txt"},{"id":"search","kind":"search","query":"alpha"}], config_path=cfg))
    assert result_by_id(payload, "ok")["status"] == "ok" and result_by_id(payload, "missing")["status"] == "not_found" and result_by_id(payload, "search")["status"] == "ok"
    assert not model.touched and not registry.touched and payload["execution"] == "direct" and payload["response_bytes"] <= dp.MAX_RESPONSE_BYTES


def test_probe_does_not_mutate_git_state(tmp_path: Path):
    repo = init_repo(tmp_path / "r"); cfg = write_cfg(tmp_path / "cfg.json", "p", repo)
    before_head = git(repo, "rev-parse", "HEAD"); index = Path(git(repo, "rev-parse", "--git-path", "index")); index = index if index.is_absolute() else repo / index
    before_index = hashlib.sha256(index.read_bytes()).hexdigest(); before_config = hashlib.sha256((repo / ".git/config").read_bytes()).hexdigest()
    run(dp.run_project_probe(str(repo), "p", [{"id":"s","kind":"git_state"},{"id":"r","kind":"read_text","path":"a.txt"}], config_path=cfg))
    assert git(repo, "rev-parse", "HEAD") == before_head and hashlib.sha256(index.read_bytes()).hexdigest() == before_index and hashlib.sha256((repo / ".git/config").read_bytes()).hexdigest() == before_config


def test_redaction_synthetic():
    text, changed = dp._redact_text("Authorization: Bearer abcdef TOKEN=supersecret https://u:pass@example.test/x")
    assert changed and "supersecret" not in text and "pass@" not in text and "abcdef" not in text


def test_process_timeout_output_cap_and_child_cleanup(tmp_path: Path):
    marker = tmp_path / "late"; child = f"import time,pathlib; time.sleep(.4); pathlib.Path({str(marker)!r}).write_text('x')"; parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(5)"
    with pytest.raises(dp.ProbeError) as exc: run(dp._run_process([sys.executable, "-c", parent, child], timeout=.08, output_limit=1024))
    assert exc.value.category == "timeout"; time.sleep(.55); assert not marker.exists()
    res = run(dp._run_process([sys.executable, "-c", "print('x'*200000)"], timeout=2, output_limit=1024)); assert res.truncated and len(res.stdout) <= 1024


def test_process_cancellation_kills_child(tmp_path: Path):
    marker = tmp_path / "late"; child = f"import time,pathlib; time.sleep(.4); pathlib.Path({str(marker)!r}).write_text('x')"; parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(5)"
    async def scenario():
        task = asyncio.create_task(dp._run_process([sys.executable, "-c", parent, child], timeout=3, output_limit=1024)); await asyncio.sleep(.08); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    run(scenario()); time.sleep(.55); assert not marker.exists()


def test_response_cap_is_enforced():
    payload = {"results":[{"data":{"text":"x"*400000},"complete":True,"truncated":False}],"execution":"direct"}; fitted = dp._fit_response(payload)
    assert fitted["response_bytes"] <= dp.MAX_RESPONSE_BYTES and fitted["results"][0]["truncated"] is True


class RedirectHandler(http.server.BaseHTTPRequestHandler):
    hit_target = False
    def do_GET(self):
        if self.path == "/health": self.send_response(302); self.send_header("Location", "/target"); self.end_headers()
        else: type(self).hit_target = True; self.send_response(200); self.end_headers(); self.wfile.write(b"target")
    def log_message(self, *args): pass


def test_health_registered_only_and_redirect_not_followed(tmp_path: Path):
    server = socketserver.TCPServer(("127.0.0.1", 0), RedirectHandler); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start(); port = server.server_address[1]
    repo = init_repo(tmp_path / "r"); extra = {"health":{"h":{"url":f"http://127.0.0.1:{port}/health","methods":["GET"],"body_bytes":1024}}}; cfg = write_cfg(tmp_path / "cfg.json", "p", repo, extra)
    try:
        payload = run(dp.run_host_probe(str(repo), "p", [{"id":"h","kind":"health","endpoint":"h","method":"GET"},{"id":"bad","kind":"health","endpoint":"other","method":"GET"}], config_path=cfg))
        assert result_by_id(payload,"h")["data"]["status"] == 302 and result_by_id(payload,"h")["data"]["followed_redirect"] is False and RedirectHandler.hit_target is False and result_by_id(payload,"bad")["status"] == "denied"
    finally: server.shutdown(); server.server_close()


def test_config_presence_returns_booleans_only(tmp_path: Path):
    repo = init_repo(tmp_path / "r"); env = tmp_path / "actual.env"; env.write_text("TOKEN=verysecret\nEMPTY=\nOTHER=ignored\n", encoding="utf-8")
    extra = {"config_presence":{"c":{"path":str(env),"keys":["TOKEN","EMPTY"]}}}; cfg = write_cfg(tmp_path / "cfg.json", "p", repo, extra)
    payload = run(dp.run_host_probe(str(repo), "p", [{"id":"c","kind":"config_presence","source":"c","keys":["TOKEN","EMPTY"]}], config_path=cfg)); out = json.dumps(result_by_id(payload,"c"))
    assert "verysecret" not in out and '"present_nonempty": true' in out and '"present_nonempty": false' in out


def test_runtime_package_uses_metadata_without_importing_app(tmp_path: Path):
    repo = init_repo(tmp_path / "r"); extra = {"runtimes":{"pytest":{"type":"package","python":sys.executable,"distribution":"pytest"}}}; cfg = write_cfg(tmp_path / "cfg.json", "p", repo, extra)
    payload = run(dp.run_host_probe(str(repo), "p", [{"id":"r","kind":"runtime","runtime":"pytest"}], config_path=cfg)); data = result_by_id(payload,"r")["data"]
    assert data["distribution"] == "pytest" and data["app_imported"] is False


def test_missing_local_object_is_not_found_without_fetch(tmp_path: Path):
    repo = init_repo(tmp_path / "r"); cfg = write_cfg(tmp_path / "cfg.json", "p", repo)
    payload = run(dp.run_project_probe(str(repo), "p", [{"id":"x","kind":"git_log","ref":"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"}], config_path=cfg))
    assert result_by_id(payload,"x")["status"] == "not_found"


def test_unbound_remote_and_github_scope_errors_are_distinct(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path / "r")
    with pytest.raises(dp.ProbeError) as exc: run(dp._remote_head(repo, {"github_repo":"o/r", "remotes":{"origin":"o/r"}}, "other", "main"))
    assert exc.value.category == "remote_denied"
    async def denied_gh(binding, args, **kwargs): raise dp.ProbeError("github_scope", "GitHub token lacks required Actions scope")
    monkeypatch.setattr(project_github, "_gh", denied_gh); cfg = write_cfg(tmp_path / "cfg.json", "p", repo)
    payload = run(dp.run_project_probe(str(repo), "p", [{"id":"ci", "kind":"github_status", "branch":"main"}], config_path=cfg)); row = result_by_id(payload, "ci")
    assert row["status"] == "error" and row["error"]["type"] == "github_scope"


def test_secret_paths_are_skipped_by_search_and_never_returned(tmp_path: Path):
    repo = init_repo(tmp_path / "r"); (repo / ".env.fixture").write_text("ONLY_SECRET_NEEDLE=yes\n", encoding="utf-8")
    data = run(dp._search(repo, {"query":"ONLY_SECRET_NEEDLE", "mode":"content", "limit":20}))
    assert data["matches"] == [] and all(".env" not in json.dumps(m) for m in data["matches"])


def test_schema_rejects_more_than_twenty_checks():
    validator = Draft202012Validator(dp.PROJECT_PROBE_INPUT_SCHEMA); payload = {"project":"p", "checks":[{"id":f"x{i}", "kind":"git_state"} for i in range(21)]}
    assert list(validator.iter_errors(payload))


def test_liberal_host_diagnostics_are_read_only_and_available_unbound(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path / "r")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"version": 1, "projects": {}}), encoding="utf-8")
    monkeypatch.setattr(dp, "_system_info", lambda: {"kernel": {"release": "test"}})
    monkeypatch.setattr(dp, "_network_listeners", lambda: {"listeners": [], "complete": True})
    payload = run(dp.run_host_probe(str(repo), "unbound", [
        {"id": "sys", "kind": "system_info"},
        {"id": "net", "kind": "network_listeners"},
    ], config_path=cfg))
    assert result_by_id(payload, "sys")["status"] == "ok"
    assert result_by_id(payload, "net")["status"] == "ok"
    assert payload["target"]["host_binding"] is False


def test_slow_checks_have_more_than_twenty_seconds_and_batch_is_parallel(tmp_path: Path, monkeypatch):
    assert dp.BATCH_DEADLINE_SECONDS > 20 and dp.CHECK_TIMEOUT_SECONDS["github_status"] > 20 and dp.CHECK_TIMEOUT_SECONDS["resources"] > 20
    repo = init_repo(tmp_path / "r"); cfg = write_cfg(tmp_path / "cfg.json", "p", repo)
    async def fake_one_project(root, binding, check): await asyncio.sleep(0.15); return {"check": check["id"]}, "fake"
    monkeypatch.setattr(dp, "_one_project", fake_one_project); monkeypatch.setitem(dp.CHECK_TIMEOUT_SECONDS, "git_state", 1.0); monkeypatch.setattr(dp, "BATCH_DEADLINE_SECONDS", 1.0)
    started = time.monotonic(); payload = run(dp.run_project_probe(str(repo), "p", [{"id":"a", "kind":"git_state"},{"id":"b", "kind":"git_state"},{"id":"c", "kind":"git_state"}], config_path=cfg)); elapsed = time.monotonic() - started
    assert elapsed < 0.35 and [row["status"] for row in payload["results"]] == ["ok", "ok", "ok"]
