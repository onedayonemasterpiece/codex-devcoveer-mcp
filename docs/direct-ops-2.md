# Direct Ops 2 - liberal typed operator surface

Status: implementation slice started 2026-09-20.

Codex DevCoveer should not start OpenCode for work that is deterministic once the operation is known. The direct surface is intentionally liberal. The hard boundary is narrow: no caller-supplied arbitrary shell/eval, no arbitrary sudo or host path/systemd unit, no secrets/env/cookies/private browser state, no force push, no unconditional destructive reset, and no arbitrary authenticated HTTP request.

Implemented in this slice:

Project reads:
- capabilities returns actual GitHub remotes plus registered project/host capabilities.
- workspace_compare classifies target vs HEAD vs index vs worktree vs untracked with hashes.
- GitHub origin is derived from the real Git remote for any project already resolved under /home/dev/projects; per-project config is no longer required for ordinary GitHub remote operations.

Project writes:
- atomic write_text with optional SHA-256 or must-not-exist precondition.
- mkdir, move_path, delete_file and chmod_executable, repository-relative with secret/symlink/.git denial.
- git_create_branch limited to chatgpt/*, exact git_stage paths, and non-force git_commit_push.
- capture_handoff snapshots a dirty worktree including allowed untracked files into the existing isolated managed handoff without resetting the canonical checkout.
- run_tool additionally supports pnpm/yarn scripts, Gradle wrapper tasks, Make targets and named Python modules.

Host writes:
- service_action supports start/stop/restart/reload/enable/disable for registered user services. Stop/restart are deferred so the current RPC can return.
- write_registered_file atomically updates only a registered host_files alias, with optional expected SHA-256.

Recovery rule:
A dirty production checkout is evidence, not permission to reset. workspace_compare answers which files already match a target release and which are independent recovery changes. capture_handoff preserves the actual overlay before any convergence.

To-be, using the same typed design rather than project-specific one-off tools:
- registered host-root read/stat/hash and process-tree/sanitized argv/cwd;
- DNS/TCP/TLS and bounded unauthenticated HTTP rollout probes with SSRF denial;
- MCP runtime capability manifest with version/tool/schema digest;
- generic release_materialize/release_deploy/release_activate/edge_route_publish and generic deployment receipts;
- daemon-reload plus registered unit/drop-in materialization through host_files;
- asynchronous deterministic jobs and cancellation/status for long builds/deployments;
- fail-fast batch actions with per-step receipts;
- typed Android Gradle/APK/ADB operations;
- guarded workspace restore/reconcile that requires a prior capture receipt.

Capabilities should evolve independently by contract. They must not be coupled to another project's SHA.
