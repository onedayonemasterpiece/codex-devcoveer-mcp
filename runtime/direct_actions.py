from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from direct_probe_core import (
    DEFAULT_CONFIG_PATH,
    ProbeError,
    _binding,
    _git_env,
    _load_config,
    _redact_text,
    _run_process,
    _safe_worktree_path,
    _validate_alias,
    _validate_branch,
)
from direct_probe_host_services import _host_section, _systemd_env
from direct_ops2 import (
    capture_handoff, chmod_executable, delete_file, git_commit_push, git_create_branch,
    git_stage, mkdir, move_path, resolve_github_remote, runner_argv, service_action,
    write_registered_file, write_text,
)
from direct_ops2_host import daemon_reload, delete_registered_file
from direct_ops2_worktrees import capture_worktree_handoff, owned_service_action, reconcile_worktree

HANDOFF_ID_RE = re.compile(r"^src_[0-9a-f]{24}$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SECRET_ENV_RE = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|AUTH|COOKIE)", re.I)
MAX_ACTION_OUTPUT = 256 * 1024
MAX_ACTION_TIMEOUT = 3600.0
ARG_RE = re.compile(r"^[^\x00\r\n]{0,500}$")
NPM_SCRIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,79}$")

_PROJECT_ACTION_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "kind": {"const": "sync_ff"},
                "remote": {"type": "string", "minLength": 1, "maxLength": 64,
                           "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                "branch": {"type": "string", "minLength": 1, "maxLength": 200,
                           "pattern": r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"},
                "expected_sha": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
            },
            "required": ["kind", "remote", "branch", "expected_sha"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "run_tool"},
                "tool": {"type": "string", "enum": [
                    "pytest", "bash_script", "python_script", "node_script", "npm_script",
                    "pnpm_script", "yarn_script", "gradle_task", "make_target", "python_module"
                ]},
                "path": {"type": "string", "minLength": 1, "maxLength": 500},
                "script": {"type": "string", "minLength": 1, "maxLength": 80,
                           "pattern": r"^[A-Za-z0-9][A-Za-z0-9:._-]*$"},
                "cwd": {"type": "string", "maxLength": 500},
                "args": {"type": "array", "maxItems": 64,
                         "items": {"type": "string", "maxLength": 500}},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
            },
            "required": ["kind", "tool"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "run_profile"},
                "profile": {"type": "string", "minLength": 1, "maxLength": 64,
                            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
            },
            "required": ["kind", "profile"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "handoff_commit_push"},
                "remote": {"type": "string", "minLength": 1, "maxLength": 64,
                           "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                "branch": {"type": "string", "minLength": 9, "maxLength": 200,
                           "pattern": r"^chatgpt/[A-Za-z0-9][A-Za-z0-9._/-]*$"},
                "commit_message": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "required": ["kind", "remote", "branch", "commit_message"],
            "additionalProperties": False,
        },
        {"type":"object","properties":{"kind":{"const":"write_text"},"path":{"type":"string","minLength":1,"maxLength":500},"content":{"type":"string","maxLength":2097152},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"},"must_not_exist":{"type":"boolean"}},"required":["kind","path","content"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"mkdir"},"path":{"type":"string","minLength":1,"maxLength":500},"parents":{"type":"boolean"}},"required":["kind","path"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"move_path"},"source":{"type":"string","minLength":1,"maxLength":500},"target":{"type":"string","minLength":1,"maxLength":500},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"}},"required":["kind","source","target"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"delete_file"},"path":{"type":"string","minLength":1,"maxLength":500},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"}},"required":["kind","path"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"chmod_executable"},"path":{"type":"string","minLength":1,"maxLength":500},"executable":{"type":"boolean"},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"}},"required":["kind","path","executable"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"git_create_branch"},"branch":{"type":"string","minLength":9,"maxLength":200,"pattern":r"^chatgpt/[A-Za-z0-9][A-Za-z0-9._/-]*$"},"start_ref":{"type":"string","minLength":1,"maxLength":200}},"required":["kind","branch"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"git_stage"},"paths":{"type":"array","minItems":1,"maxItems":128,"items":{"type":"string","minLength":1,"maxLength":500}}},"required":["kind","paths"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"git_commit_push"},"remote":{"type":"string","minLength":1,"maxLength":64,"pattern":r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},"commit_message":{"type":"string","minLength":1,"maxLength":200}},"required":["kind","commit_message"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"capture_handoff"},"base_sha":{"type":"string","pattern":r"^[0-9a-f]{40}$"},"include_untracked":{"type":"boolean"},"paths":{"type":"array","minItems":1,"maxItems":256,"items":{"type":"string","minLength":1,"maxLength":500}}},"required":["kind","base_sha"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"worktree_capture_handoff"},"worktree":{"type":"string","minLength":1,"maxLength":1000},"base_sha":{"type":"string","pattern":r"^[0-9a-f]{40}$"},"include_untracked":{"type":"boolean"},"paths":{"type":"array","minItems":1,"maxItems":256,"items":{"type":"string","minLength":1,"maxLength":500}}},"required":["kind","worktree","base_sha"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"worktree_reconcile"},"worktree":{"type":"string","minLength":1,"maxLength":1000},"handoff_id":{"type":"string","pattern":r"^src_[0-9a-f]{24}$"}},"required":["kind","worktree","handoff_id"],"additionalProperties":False},
        {"type":"object","properties":{"kind":{"const":"owned_service_action"},"unit":{"type":"string","minLength":1,"maxLength":200,"pattern":r"^[A-Za-z0-9_.@:-]+\.service$"},"operation":{"type":"string","enum":["status","start","stop","restart","reload","enable","disable"]}},"required":["kind","unit","operation"],"additionalProperties":False},
    ]
}

PROJECT_ACTION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "minLength": 1, "maxLength": 128},
        "action": _PROJECT_ACTION_SCHEMA,
    },
    "required": ["project", "action"],
    "additionalProperties": False,
}

HOST_ACTION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "minLength": 1, "maxLength": 128},
        "action": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "restart_service"},
                        "service": {"type": "string", "minLength": 1, "maxLength": 64,
                                    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                    },
                    "required": ["kind", "service"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "run_profile"},
                        "profile": {"type": "string", "minLength": 1, "maxLength": 64,
                                    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                    },
                    "required": ["kind", "profile"],
                    "additionalProperties": False,
                },
                {"type":"object","properties":{"kind":{"const":"service_action"},"service":{"type":"string","minLength":1,"maxLength":64,"pattern":r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},"operation":{"type":"string","enum":["start","stop","restart","reload","enable","disable"]}},"required":["kind","service","operation"],"additionalProperties":False},
                {"type":"object","properties":{"kind":{"const":"write_registered_file"},"file":{"type":"string","minLength":1,"maxLength":64,"pattern":r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},"content":{"type":"string","maxLength":2097152},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"}},"required":["kind","file","content"],"additionalProperties":False},
                {"type":"object","properties":{"kind":{"const":"delete_registered_file"},"file":{"type":"string","minLength":1,"maxLength":64,"pattern":r"^[A-Za-z0-9][A-Za-z0-9._-]*$"},"expected_sha256":{"type":"string","pattern":r"^[0-9a-f]{64}$"}},"required":["kind","file"],"additionalProperties":False},
                {"type":"object","properties":{"kind":{"const":"daemon_reload"}},"required":["kind"],"additionalProperties":False},
            ]
        },
    },
    "required": ["project", "action"],
    "additionalProperties": False,
}


def _handoff_binding(root: Path, project_id: str) -> tuple[str, bool, dict[str, Any] | None]:
    if not project_id.startswith("handoff:"):
        return project_id, False, None
    handoff_id = project_id.removeprefix("handoff:")
    if HANDOFF_ID_RE.fullmatch(handoff_id) is None:
        raise ProbeError("invalid_input", "invalid source handoff id")
    handoff_root = (Path.home() / ".local" / "share" / "openai-codex-mcp" / "handoffs").resolve()
    root_real = root.resolve(strict=True)
    try:
        relative = root_real.relative_to(handoff_root)
    except ValueError as exc:
        raise ProbeError("handoff_denied", "handoff workspace is outside the managed root", status="denied") from exc
    if len(relative.parts) != 2 or relative.parts != (handoff_id, "worktree"):
        raise ProbeError("handoff_denied", "handoff workspace layout is invalid", status="denied")
    meta_path = root_real.parent / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProbeError("handoff_invalid", "handoff metadata is unavailable") from exc
    if not isinstance(meta, dict) or meta.get("id") != handoff_id or meta.get("worktree") != str(root_real):
        raise ProbeError("handoff_invalid", "handoff metadata does not match workspace")
    original = meta.get("projectId")
    if not isinstance(original, str) or not original:
        raise ProbeError("handoff_invalid", "handoff project binding is missing")
    return original, True, meta


def _registered_remote(binding: dict[str, Any], alias: str) -> str:
    alias = _validate_alias(alias)
    remotes = binding.get("remotes") or {}
    if not isinstance(remotes, dict):
        raise ProbeError("config", "trusted remotes section is invalid")
    repo = remotes.get(alias)
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ProbeError("remote_denied", "remote is not registered", status="denied")
    return repo


def _remote_repo(url: str) -> str | None:
    match = re.search(r"github\.com(?::|/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", url.strip())
    return match.group(1) if match else None


async def _git(root: Path, *args: str, timeout: float = 60.0):
    argv = [
        "/usr/bin/git", "--no-optional-locks",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        "-c", "diff.external=",
        "-C", str(root), *args,
    ]
    return await _run_process(argv, env=_git_env(), timeout=timeout, output_limit=MAX_ACTION_OUTPUT)


def _decode(result, stream: str) -> tuple[str, bool]:
    raw = result.stdout if stream == "stdout" else result.stderr
    text, redacted = _redact_text(raw.decode("utf-8", "replace"))
    return text, redacted


async def _sync_ff(root: Path, binding: dict[str, Any], action: dict[str, Any], *, handoff: bool) -> dict[str, Any]:
    if handoff:
        raise ProbeError("handoff_denied", "sync_ff is only allowed on the canonical checkout", status="denied")
    remote = _validate_alias(action["remote"])
    branch = _validate_branch(action["branch"])
    expected = str(action["expected_sha"]).lower()
    if HEX40_RE.fullmatch(expected) is None:
        raise ProbeError("invalid_input", "expected_sha must be exact 40-hex")

    if binding.get("remotes"):
        registered_repo = _registered_remote(binding, remote)
        remote_url = await _git(root, "remote", "get-url", remote, timeout=15)
        if remote_url.exit_code != 0 or _remote_repo(remote_url.stdout.decode("utf-8", "replace")) != registered_repo:
            raise ProbeError("remote_mismatch", "configured Git remote does not match trusted repository", status="denied")
    else:
        registered_repo, _remote_url = await resolve_github_remote(root, binding, remote)

    current_branch = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=10)
    if current_branch.exit_code != 0 or current_branch.stdout.decode().strip() != branch:
        raise ProbeError("branch_mismatch", "canonical checkout is not on the requested branch", status="denied")

    dirty = await _git(root, "status", "--porcelain=v1", "--untracked-files=no", timeout=15)
    if dirty.exit_code != 0:
        raise ProbeError("git_state", "could not inspect tracked working-tree state", exit_code=dirty.exit_code)
    if dirty.stdout.strip():
        raise ProbeError("dirty_tracked", "tracked/staged changes block direct fast-forward", status="denied")

    old = await _git(root, "rev-parse", "HEAD", timeout=10)
    if old.exit_code != 0:
        raise ProbeError("git_state", "could not resolve current HEAD", exit_code=old.exit_code)
    old_sha = old.stdout.decode().strip()

    fetched = await _git(root, "fetch", "--no-tags", remote, f"refs/heads/{branch}", timeout=120)
    if fetched.exit_code != 0:
        err, red = _decode(fetched, "stderr")
        raise ProbeError("fetch_failed", err[-1200:] or "git fetch failed", exit_code=fetched.exit_code)

    observed = await _git(root, "rev-parse", "FETCH_HEAD", timeout=10)
    observed_sha = observed.stdout.decode().strip() if observed.exit_code == 0 else ""
    if observed_sha != expected:
        raise ProbeError("remote_moved", f"fetched remote SHA {observed_sha or '<unknown>'} differs from expected {expected}", status="denied")

    ancestor = await _git(root, "merge-base", "--is-ancestor", old_sha, expected, timeout=15)
    if ancestor.exit_code != 0:
        raise ProbeError("non_fast_forward", "requested target is not a fast-forward of current HEAD", status="denied")

    changed = old_sha != expected
    if changed:
        merged = await _git(root, "merge", "--ff-only", expected, timeout=60)
        if merged.exit_code != 0:
            err, _ = _decode(merged, "stderr")
            raise ProbeError("fast_forward_failed", err[-1200:] or "git fast-forward failed", exit_code=merged.exit_code)

    untracked = await _git(root, "ls-files", "--others", "--exclude-standard", "-z", timeout=15)
    untracked_count = 0 if not untracked.stdout else len([x for x in untracked.stdout.split(b"\0") if x])
    return {
        "status": "ok",
        "action": "sync_ff",
        "remote": remote,
        "branch": branch,
        "old_sha": old_sha,
        "new_sha": expected,
        "changed": changed,
        "untracked_preserved_count": untracked_count,
    }


def _project_cwd(root: Path, raw: str | None) -> Path:
    if raw in (None, "", "."):
        return root.resolve(strict=True)
    return _safe_worktree_path(root, raw, require_file=False)


def _args(action: dict[str, Any]) -> list[str]:
    values = action.get("args") or []
    if not isinstance(values, list) or len(values) > 64:
        raise ProbeError("invalid_input", "args must be a bounded array")
    out: list[str] = []
    for value in values:
        if not isinstance(value, str) or not ARG_RE.fullmatch(value):
            raise ProbeError("invalid_input", "argument contains a denied character or is too long")
        out.append(value)
    return out


def _fixed_executable(*candidates: str) -> str:
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return candidate
    raise ProbeError("tool_unavailable", "requested direct project tool is not installed", status="unavailable")


async def _run_tool(root: Path, action: dict[str, Any]) -> dict[str, Any]:
    tool = action["tool"]
    cwd = _project_cwd(root, action.get("cwd"))
    args = _args(action)
    timeout = float(action.get("timeout_seconds", 900))
    if not (1 <= timeout <= MAX_ACTION_TIMEOUT):
        raise ProbeError("invalid_input", "tool timeout is out of bounds")

    if tool == "pytest":
        argv = ["/usr/bin/python3", "-m", "pytest", *args]
    elif tool in {"bash_script", "python_script", "node_script"}:
        raw_path = action.get("path")
        if not isinstance(raw_path, str):
            raise ProbeError("invalid_input", f"{tool} requires path")
        script = _safe_worktree_path(root, raw_path, require_file=True)
        if tool == "python_script":
            if script.suffix != ".py":
                raise ProbeError("invalid_input", "python_script requires a .py repository file")
            executable = "/usr/bin/python3"
        elif tool == "node_script":
            if script.suffix not in {".js", ".mjs", ".cjs"}:
                raise ProbeError("invalid_input", "node_script requires a .js/.mjs/.cjs repository file")
            executable = _fixed_executable("/usr/local/bin/node", "/usr/bin/node")
        else:
            executable = "/usr/bin/bash"
        argv = [executable, str(script), *args]
    elif tool == "npm_script":
        script = action.get("script")
        if not isinstance(script, str) or NPM_SCRIPT_RE.fullmatch(script) is None:
            raise ProbeError("invalid_input", "npm_script requires a safe script name")
        package_json = cwd / "package.json"
        if not package_json.is_file() or package_json.is_symlink():
            raise ProbeError("not_found", "package.json is missing in requested project cwd", status="not_found")
        executable = _fixed_executable("/usr/local/bin/npm", "/usr/bin/npm")
        argv = [executable, "run", "--silent", script]
        if args:
            argv.extend(["--", *args])
    else:
        extended = runner_argv(root, action, cwd)
        if extended is None:
            raise ProbeError("invalid_input", f"unknown direct project tool {tool!r}")
        argv = extended

    result = await _run_process(
        argv, cwd=cwd, env=_action_env({}), timeout=timeout, output_limit=MAX_ACTION_OUTPUT,
    )
    stdout, out_red = _decode(result, "stdout")
    stderr, err_red = _decode(result, "stderr")
    return {
        "status": "ok" if result.exit_code == 0 else "failed",
        "action": "run_tool",
        "tool": tool,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": result.truncated,
        "redacted": out_red or err_red,
    }


def _profile(binding: dict[str, Any], section: str, name: str) -> dict[str, Any]:
    name = _validate_alias(name)
    profiles = binding.get(section) or {}
    if not isinstance(profiles, dict):
        raise ProbeError("config", f"trusted {section} section is invalid")
    profile = profiles.get(name)
    if not isinstance(profile, dict):
        raise ProbeError("profile_denied", "action profile is not registered", status="denied")
    return profile


def _action_env(profile: dict[str, Any]) -> dict[str, str]:
    env = {
        "PATH": "/home/dev/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/home/dev"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "1",
    }
    extra = profile.get("env") or {}
    if not isinstance(extra, dict):
        raise ProbeError("config", "profile env must be an object")
    for key, value in extra.items():
        if not isinstance(key, str) or ENV_NAME_RE.fullmatch(key) is None or SECRET_ENV_RE.search(key):
            raise ProbeError("config", "profile env contains a denied key")
        if not isinstance(value, str) or len(value) > 500 or "\x00" in value:
            raise ProbeError("config", "profile env contains an invalid value")
        env[key] = value
    return env


async def _run_profile(root: Path, binding: dict[str, Any], profile_name: str, *,
                       section: str, handoff: bool) -> dict[str, Any]:
    profile = _profile(binding, section, profile_name)
    if handoff and profile.get("allow_handoff") is not True:
        raise ProbeError("handoff_denied", "profile is not approved for source-handoff worktrees", status="denied")

    argv = profile.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or "\x00" in x for x in argv):
        raise ProbeError("config", "action profile argv is invalid")
    if not Path(argv[0]).is_absolute():
        raise ProbeError("config", "action profile executable must be absolute")

    cwd_raw = profile.get("cwd", ".")
    if not isinstance(cwd_raw, str) or "\x00" in cwd_raw:
        raise ProbeError("config", "action profile cwd is invalid")
    if cwd_raw.startswith("/"):
        cwd = Path(cwd_raw).resolve(strict=True)
        if section == "project_actions":
            try:
                cwd.relative_to(root.resolve(strict=True))
            except ValueError as exc:
                raise ProbeError("config", "project action cwd escapes the project") from exc
    else:
        cwd = (root / cwd_raw).resolve(strict=True)
        try:
            cwd.relative_to(root.resolve(strict=True))
        except ValueError as exc:
            raise ProbeError("config", "action cwd escapes the project") from exc

    timeout = float(profile.get("timeout_seconds", 900))
    if not (1 <= timeout <= MAX_ACTION_TIMEOUT):
        raise ProbeError("config", "action profile timeout is out of bounds")
    output_limit = int(profile.get("output_bytes", 128 * 1024))
    if not (1024 <= output_limit <= MAX_ACTION_OUTPUT):
        raise ProbeError("config", "action profile output limit is out of bounds")

    result = await _run_process(argv, cwd=cwd, env=_action_env(profile),
                                timeout=timeout, output_limit=output_limit)
    stdout, out_red = _decode(result, "stdout")
    stderr, err_red = _decode(result, "stderr")
    return {
        "status": "ok" if result.exit_code == 0 else "failed",
        "action": "run_profile",
        "profile": profile_name,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": result.truncated,
        "redacted": out_red or err_red,
    }


async def _handoff_commit_push(root: Path, binding: dict[str, Any], action: dict[str, Any],
                               meta: dict[str, Any] | None) -> dict[str, Any]:
    if meta is None:
        raise ProbeError("handoff_required", "handoff_commit_push requires a handoff:<id> project", status="denied")
    remote = _validate_alias(action["remote"])
    branch = _validate_branch(action["branch"])
    if not branch.startswith("chatgpt/"):
        raise ProbeError("branch_denied", "direct handoff push is limited to chatgpt/* branches", status="denied")
    message = action["commit_message"].strip()
    if not message or any(ord(ch) < 32 and ch not in "\t" for ch in message):
        raise ProbeError("invalid_input", "commit message contains control characters")
    if binding.get("remotes"):
        registered_repo = _registered_remote(binding, remote)
        remote_url = await _git(root, "remote", "get-url", remote, timeout=15)
        if remote_url.exit_code != 0 or _remote_repo(remote_url.stdout.decode("utf-8", "replace")) != registered_repo:
            raise ProbeError("remote_mismatch", "configured Git remote does not match trusted repository", status="denied")
    else:
        registered_repo, _remote_url = await resolve_github_remote(root, binding, remote)

    base = str(meta.get("baseSha") or "")
    if HEX40_RE.fullmatch(base) is None:
        raise ProbeError("handoff_invalid", "handoff base SHA is invalid")
    head = await _git(root, "rev-parse", "HEAD", timeout=10)
    head_sha = head.stdout.decode().strip() if head.exit_code == 0 else ""
    current_branch = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=10)
    branch_name = current_branch.stdout.decode().strip() if current_branch.exit_code == 0 else ""

    if head_sha != base:
        if branch_name != branch:
            raise ProbeError("handoff_changed", "handoff HEAD moved unexpectedly", status="denied")
        pushed = await _git(root, "push", remote, f"HEAD:refs/heads/{branch}", timeout=120)
        if pushed.exit_code != 0:
            err, _ = _decode(pushed, "stderr")
            raise ProbeError("push_failed", err[-1200:] or "git push failed", exit_code=pushed.exit_code)
        return {"status": "ok", "action": "handoff_commit_push", "branch": branch,
                "commit_sha": head_sha, "reused_commit": True}

    unstaged = await _git(root, "diff", "--quiet", timeout=15)
    if unstaged.exit_code not in (0, 1):
        raise ProbeError("git_state", "could not inspect unstaged diff", exit_code=unstaged.exit_code)
    if unstaged.exit_code == 1:
        raise ProbeError("dirty_unstaged", "unstaged changes block direct handoff commit", status="denied")
    others = await _git(root, "ls-files", "--others", "--exclude-standard", "-z", timeout=15)
    if others.stdout:
        raise ProbeError("untracked_files", "untracked files block direct handoff commit", status="denied")
    staged = await _git(root, "diff", "--cached", "--quiet", base, timeout=15)
    if staged.exit_code == 0:
        raise ProbeError("empty_patch", "handoff has no staged source patch", status="denied")
    if staged.exit_code != 1:
        raise ProbeError("git_state", "could not inspect staged patch", exit_code=staged.exit_code)
    check = await _git(root, "diff", "--cached", "--check", base, timeout=30)
    if check.exit_code != 0:
        out, _ = _decode(check, "stdout")
        raise ProbeError("patch_check", out[-1200:] or "git diff --cached --check failed", status="denied")

    existing = await _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", timeout=10)
    if existing.exit_code == 0:
        raise ProbeError("branch_exists", "target branch already exists locally", status="denied")
    created = await _git(root, "switch", "-c", branch, timeout=20)
    if created.exit_code != 0:
        err, _ = _decode(created, "stderr")
        raise ProbeError("branch_create", err[-1200:] or "could not create branch", exit_code=created.exit_code)

    committed = await _git(root, "-c", "commit.gpgsign=false", "commit", "-m", message, timeout=60)
    if committed.exit_code != 0:
        err, _ = _decode(committed, "stderr")
        raise ProbeError("commit_failed", err[-1200:] or "git commit failed", exit_code=committed.exit_code)
    new_head = await _git(root, "rev-parse", "HEAD", timeout=10)
    commit_sha = new_head.stdout.decode().strip()
    pushed = await _git(root, "push", remote, f"HEAD:refs/heads/{branch}", timeout=120)
    if pushed.exit_code != 0:
        err, _ = _decode(pushed, "stderr")
        raise ProbeError("push_failed", err[-1200:] or "git push failed", exit_code=pushed.exit_code)
    return {"status": "ok", "action": "handoff_commit_push", "branch": branch,
            "commit_sha": commit_sha, "reused_commit": False}


async def run_project_action(project_root: str, project_id: str, action: dict[str, Any], *,
                             config_path: Path | None = None) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=True)
    binding_id, handoff, meta = _handoff_binding(root, project_id)
    binding = _binding(_load_config(config_path or DEFAULT_CONFIG_PATH), binding_id)
    kind = action["kind"]
    if kind == "sync_ff":
        return await _sync_ff(root, binding, action, handoff=handoff)
    if kind == "run_tool":
        return await _run_tool(root, action)
    if kind == "run_profile":
        return await _run_profile(root, binding, action["profile"], section="project_actions", handoff=handoff)
    if kind == "handoff_commit_push":
        return await _handoff_commit_push(root, binding, action, meta)
    if kind == "write_text": return write_text(root, action)
    if kind == "mkdir": return mkdir(root, action)
    if kind == "move_path": return move_path(root, action)
    if kind == "delete_file": return delete_file(root, action)
    if kind == "chmod_executable": return chmod_executable(root, action)
    if kind == "git_create_branch": return await git_create_branch(root, action)
    if kind == "git_stage": return await git_stage(root, action)
    if kind == "git_commit_push": return await git_commit_push(root, binding, action)
    if kind == "capture_handoff":
        if handoff: raise ProbeError("handoff_denied", "capture_handoff requires a canonical project", status="denied")
        return await capture_handoff(root, binding_id, action)
    if kind == "worktree_capture_handoff":
        if handoff: raise ProbeError("handoff_denied", "worktree capture requires a canonical project", status="denied")
        return await capture_worktree_handoff(root, binding_id, action)
    if kind == "worktree_reconcile":
        if handoff: raise ProbeError("handoff_denied", "worktree reconcile requires a canonical project", status="denied")
        return await reconcile_worktree(root, binding_id, action)
    if kind == "owned_service_action":
        if handoff: raise ProbeError("handoff_denied", "owned service action requires a canonical project", status="denied")
        return await owned_service_action(root, action)
    raise ProbeError("invalid_input", f"unknown project action kind {kind!r}")


async def _restart_service(binding: dict[str, Any], service_alias: str) -> dict[str, Any]:
    alias = _validate_alias(service_alias)
    cfg = _host_section(binding, "services").get(alias)
    if not isinstance(cfg, dict):
        raise ProbeError("target_denied", "service is not registered", status="denied")
    unit = cfg.get("unit")
    scope = cfg.get("scope", "user")
    if scope != "user" or not isinstance(unit, str) or re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", unit) is None:
        raise ProbeError("target_denied", "only registered user services may be restarted directly", status="denied")
    token = hashlib.sha256(unit.encode("utf-8")).hexdigest()[:12]
    transient = f"devcoveer-direct-restart-{token}"
    argv = [
        "/usr/bin/systemd-run", "--user", "--quiet", "--collect",
        f"--unit={transient}", "--on-active=1s",
        "/usr/bin/systemctl", "--user", "restart", unit,
    ]
    result = await _run_process(argv, env=_systemd_env("user"), timeout=15, output_limit=16 * 1024)
    if result.exit_code != 0:
        err, _ = _decode(result, "stderr")
        raise ProbeError("restart_schedule", err[-1200:] or "could not schedule service restart", exit_code=result.exit_code)
    return {"status": "ok", "action": "restart_service", "service": alias,
            "unit": unit, "scheduled_after_seconds": 1, "transient_unit": transient}


async def run_host_action(project_root: str, project_id: str, action: dict[str, Any], *,
                          config_path: Path | None = None) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=True)
    binding_id, handoff, _meta = _handoff_binding(root, project_id)
    if handoff:
        raise ProbeError("handoff_denied", "host actions are unavailable for handoff projects", status="denied")
    binding = _binding(_load_config(config_path or DEFAULT_CONFIG_PATH), binding_id)
    kind = action["kind"]
    if kind == "restart_service":
        return await _restart_service(binding, action["service"])
    if kind == "run_profile":
        return await _run_profile(root, binding, action["profile"], section="host_actions", handoff=False)
    if kind == "service_action": return await service_action(binding, action["service"], action["operation"])
    if kind == "write_registered_file": return write_registered_file(binding, action)
    if kind == "delete_registered_file": return delete_registered_file(binding, action)
    if kind == "daemon_reload": return await daemon_reload(binding)
    raise ProbeError("invalid_input", f"unknown host action kind {kind!r}")
