"""Small web-test preflight and exact instruction reads; no installer or executor."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from direct_probe_core import ProbeError, _redact_text, _run_process, _safe_worktree_path

MAX_METADATA_BYTES = 128 * 1024


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(MAX_METADATA_BYTES + 1)
    if len(data) > MAX_METADATA_BYTES:
        raise ProbeError("file_too_large", "web/instruction metadata exceeds 128 KiB", status="denied")
    return data


def read_instructions(root: Path, check: dict[str, Any]) -> dict[str, Any]:
    """Only AGENTS.md at the selected scope; never search HOME or adjacent checkouts."""
    scope = check.get("scope", "project")
    cwd = check.get("cwd", "")
    if scope == "home":
        if cwd not in ("", "."):
            raise ProbeError("invalid_input", "home instructions accepts no cwd; only ~/AGENTS.md is read")
        base, rel = Path.home(), "AGENTS.md"
    elif scope == "project":
        base = root
        directory = _directory(root, cwd)
        rel = (directory / "AGENTS.md").relative_to(root.resolve()).as_posix()
    else:
        raise ProbeError("invalid_input", "instruction scope must be home or project")
    try:
        path = _safe_worktree_path(base, rel, require_file=True)
    except ProbeError as exc:
        if exc.category == "not_found":
            return {"scope": scope, "path": str(base / rel), "found": False, "complete": True}
        raise
    before = path.stat()
    data = _read_bounded(path)
    after = path.stat()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeError("file_type", "AGENTS.md must be UTF-8 text") from exc
    lines = text.splitlines()
    start = int(check.get("start_line", 1))
    end = min(int(check.get("end_line", start + 199)), start + 199, len(lines))
    if start < 1 or int(check.get("end_line", start + 199)) < start:
        raise ProbeError("invalid_input", "invalid instruction line range")
    excerpt, redacted = _redact_text("\n".join(lines[start - 1:end]))
    truncated = len(excerpt.encode("utf-8")) > 32768
    if truncated:
        excerpt = excerpt.encode("utf-8")[:32768].decode("utf-8", "ignore")
    changed = (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
    return {"scope": scope, "path": str(path), "found": True,
            "sha256": hashlib.sha256(data).hexdigest(), "start_line": start, "end_line": end,
            "total_lines": len(lines), "text": excerpt, "redacted": redacted,
            "next_start_line": end + 1 if end < len(lines) and not truncated else None,
            "truncated": truncated, "changed_during_read": changed,
            "complete": not truncated and not changed}


def _directory(root: Path, cwd: str) -> Path:
    directory = root.resolve(strict=True) if cwd in ("", ".") else _safe_worktree_path(root, cwd)
    if not directory.is_dir():
        raise ProbeError("file_type", "cwd must be a project directory")
    return directory


# Fixed server-authored metadata query. No npx, installation, browser launch, site
# navigation or caller-supplied JavaScript. Refuse dependencies outside the project.
_PLAYWRIGHT_FACTS = r"""
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const [cwd, root, browser] = process.argv.slice(1);
const local = createRequire(path.join(cwd, 'package.json'));
const inside = p => {const r = path.relative(root, fs.realpathSync(p)); return r !== '..' && !r.startsWith('..'+path.sep) && !path.isAbsolute(r);};
let pkg;
for (const name of ['playwright', '@playwright/test', 'playwright-core']) {
  try {
    const manifest = local.resolve(name + '/package.json');
    if (!inside(manifest)) { console.log(JSON.stringify({status:'dependency_outside_project'})); process.exit(0); }
    pkg = {name, manifest}; break;
  } catch (e) { if (e.code !== 'MODULE_NOT_FOUND') throw e; }
}
if (!pkg) { console.log(JSON.stringify({status:'dependency_missing'})); process.exit(0); }
const info = JSON.parse(fs.readFileSync(pkg.manifest, 'utf8'));
const entry = local.resolve(pkg.name);
if (!inside(entry)) { console.log(JSON.stringify({status:'dependency_outside_project'})); process.exit(0); }
const api = local(pkg.name);
const executable = api[browser].executablePath();
const cli = path.join(path.dirname(pkg.manifest), 'cli.js');
console.log(JSON.stringify({status:'inspected', package:pkg.name, version:info.version, node_version:process.version,
  executable, cli:fs.existsSync(cli) && inside(cli) ? path.relative(root, cli) : null}));
"""


async def web_preflight(root: Path, check: dict[str, Any]) -> dict[str, Any]:
    from direct_actions import _fixed_executable, _tool_env

    root = root.resolve(strict=True)
    cwd = _directory(root, check.get("cwd", ""))
    manifest = _safe_worktree_path(root, (cwd / "package.json").relative_to(root).as_posix(), require_file=True)
    try:
        package = json.loads(_read_bounded(manifest))
    except (ValueError, UnicodeError) as exc:
        raise ProbeError("invalid_package", "package.json is not valid JSON") from exc
    if not isinstance(package, dict):
        raise ProbeError("invalid_package", "package.json must be an object")
    browser = check.get("browser", "chromium")
    if browser not in {"chromium", "firefox", "webkit"}:
        raise ProbeError("invalid_input", "unknown browser")
    scripts = package.get("scripts", {})
    scripts = sorted(str(name)[:100] for name in scripts) if isinstance(scripts, dict) else []
    data: dict[str, Any] = {"cwd": str(cwd), "project_relative_cwd": cwd.relative_to(root).as_posix(),
        "package_json": manifest.relative_to(root).as_posix(), "scripts": scripts[:100],
        "scripts_truncated": len(scripts) > 100, "launch_verified": False,
        "guidance": "Use existing npm_script/node_script tests. Full builds, browser suites and unknown-duration work use job_start then job_status on the same project/worktree with a request_key. path is project-root-relative, not cwd-relative. UI inspection uses my-browser-bridge. Do not scan HOME or other checkouts, fetch latest Playwright with npx, or read .env."}
    declared = {}
    for section in ("dependencies", "devDependencies"):
        dependencies = package.get(section) or {}
        if not isinstance(dependencies, dict):
            raise ProbeError("invalid_package", f"{section} must be an object")
        for name, version in dependencies.items():
            if name in {"playwright", "@playwright/test", "playwright-core"}:
                declared[name] = str(version)[:100]
    data["declared_playwright"] = declared
    node = _fixed_executable("/home/dev/.local/bin/node", "/usr/local/bin/node", "/usr/bin/node")
    env = _tool_env({}, None)
    result = await _run_process([node, "-e", _PLAYWRIGHT_FACTS, str(cwd), str(root), browser],
                                cwd=cwd, env=env, timeout=10, output_limit=8192)
    data["node_executable"] = node
    if result.exit_code != 0 or result.truncated:
        error, redacted = _redact_text(result.stderr.decode("utf-8", "replace"))
        return {**data, "playwright": {"status": "inspection_failed", "error": error[-1200:]},
                "complete": False, "redacted": redacted}
    try:
        facts = json.loads(result.stdout)
        if not isinstance(facts, dict):
            raise ValueError("not an object")
    except (ValueError, UnicodeError) as exc:
        raise ProbeError("invalid_output", "local Playwright metadata was not JSON") from exc
    if facts.get("status") != "inspected":
        return {**data, "playwright": facts}
    executable = str(facts["executable"])
    data["playwright"] = {"status": "inspected", "package": facts["package"], "version": facts["version"],
        "node_version": facts["node_version"], "browser": browser, "executable_path": executable,
        "executable_present": Path(executable).is_file() and os.access(executable, os.X_OK),
        "note": "Executable presence is not launch acceptance; Chromium's default headless shell and OS libraries still require a real test. Install with this project's pinned CLI, never substitute a different cached revision."}
    cli = facts.get("cli")
    if cli:
        try:
            _safe_worktree_path(root, cli, require_file=True)
        except ProbeError:
            data["playwright"]["install_hint"] = "CLI path is outside the direct-file contract; use an existing project-owned install script/profile, not another checkout."
        else:
            data["install_action"] = {"plane": "project_action", "operation": "job_start", "payload": {
                "tool": "node_script", "path": cli, "cwd": cwd.relative_to(root).as_posix(),
                "args": ["install", browser], "timeout_seconds": 900}}
    return data
