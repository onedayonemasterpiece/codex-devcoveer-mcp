# Source handoff P0

## Purpose

Source handoff removes GitHub from the critical transport path when ChatGPT has already produced a code change but cannot reliably commit/push it. GitHub remains the final source of truth after DevCoveer/OpenCode testing and correction.

P0 deliberately uses a Git patch, not an archive or artifact store.

## MCP surface

Add exactly two methods to the existing bridge:

### `stage_source_handoff`

Inputs:

- `project`: an existing DevCoveer project identifier; never an arbitrary filesystem path;
- `base_sha`: exact 40-hex Git commit used to produce the patch;
- `patch`: output equivalent to `git diff --cached --binary <base_sha>`;
- `patch_sha256`: SHA-256 of the exact UTF-8 patch bytes.

Bridge behavior:

1. Resolve `project` through the existing safe DevCoveer project resolver.
2. Call `source_handoff.stage_source_handoff(...)` with that resolved Git root.
3. Return the helper result, including the synthetic project id `handoff:<handoff_id>`.

The helper creates a detached Git worktree at the exact `base_sha`, validates the patch with `git apply --check --index`, applies it through stdin, and stores only a small `meta.json`. The patch itself is never persisted.

### `cleanup_source_handoff`

Input: `handoff_id`.

Call `source_handoff.cleanup_source_handoff(...)`. Use this after the resulting code has been pushed/accepted and no further `continue_task` is needed.

## Existing project resolver

Teach the existing resolver one narrow new case before normal project lookup:

```python
if project.startswith("handoff:"):
    return resolve_source_handoff(project.removeprefix("handoff:"))
```

The resolver must still reject arbitrary paths. A `handoff:` value is valid only when `resolve_source_handoff` validates the managed id and metadata under the fixed handoff root.

No other `start_task` / `continue_task` lifecycle change is required: `start_task(project="handoff:src_...")` receives the isolated worktree as its cwd, and continuation remains on the same task/workspace.

## Constraints

- Do not create or copy a virtualenv for a handoff.
- Do not install dependencies as part of staging.
- Do not write into the canonical checkout.
- Do not persist the patch, ZIP, tarball or any other transport copy.
- Do not support submodule/gitlink mutations in P0.
- Do not fetch or force-push automatically in the staging helper.
- Keep the worktree until explicit cleanup so OpenCode continuation remains possible.
- Existing project environments may be reused by tasks, but Python tests must not accidentally import the project package from a canonical editable-install instead of the handoff worktree. Run from the handoff cwd and preserve handoff source precedence.

## Patch creation

To include modified, deleted and newly created files, stage the intended delta first and generate the patch from the exact base:

```bash
git add -A
git diff --cached --binary <base_sha>
```

A plain unstaged `git diff` is not sufficient because it omits untracked files.

## Disk behavior

There is no archive lifecycle in P0: patch bytes go directly from the MCP request to `git apply` stdin. On an ordinary staging failure the newly created worktree is removed. On successful staging the only persistent data are the isolated Git worktree and small metadata. `cleanup_source_handoff` removes both and prunes Git worktree metadata.

The helper refuses staging when free space is below its bounded minimum. Do not add per-handoff venvs, package caches, containers, or background artifact storage.

## Required acceptance before production

1. Run `tests/test_source_handoff.py` plus the existing bridge/OpenCode regression suite.
2. Fresh-read the exact installed bridge before wiring the methods; preserve the v3.10 adaptive-routing and Acceptance Gate changes already pending in PR #2.
3. Use the existing fail-closed installer approach: dry-run candidate, pin exact hashes, active-task gate, atomic install, controlled restart.
4. After restart, fresh MCP discovery must show `stage_source_handoff` and `cleanup_source_handoff` while all existing methods remain intact.
5. E2E: stage a tiny patch containing modify/add/delete -> `start_task(project="handoff:<id>")` -> prove OpenCode reads the changed files -> make/test one small correction -> commit/push with git/gh -> read back GitHub -> call cleanup -> verify the worktree is gone and no patch/archive or per-handoff `.venv` remains.
