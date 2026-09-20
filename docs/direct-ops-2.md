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

## Direct Ops 2 host/runtime slice

Implemented after the initial Direct Ops 2 project slice:
- root_read_text, root_file_stat and root_file_sha256 inside registered host roots with the same secret/symlink policy;
- process_tree for registered services, returning executable/cwd plus argv shape and explicitly never reading process environment;
- network probe modes dns/tcp/tls/http for public or loopback targets, with pinned resolved IP, no auth headers, no redirect following, bounded bodies and denial of private/link-local/reserved connect targets;
- mcp_manifest from a registered runtime bridge file, returning runtime version, bridge hash, tool catalogue and catalogue digest for client/server schema-cache diagnostics;
- daemon_reload when at least one registered user service authorizes host control;
- delete_registered_file with alias-only addressing and optional expected SHA-256.

Still to-be:
- generic release materialization/deploy/activate/edge-route receipts;
- async deterministic jobs and typed batch actions;
- guarded workspace reconcile tied to a capture receipt;
- typed Android ADB/install/logcat primitives.

## Project-owned runtime worktrees and guarded reconciliation

A recovered production runtime often is already a detached Git worktree of the same repository. Direct Ops therefore does not need a product-specific deployment executor for that common case.

Implemented:
- worktree_compare reads target/HEAD/index/worktree/untracked state for any path that Git itself reports as a worktree of the resolved project.
- worktree_capture_handoff captures an external runtime worktree into the normal isolated handoff and writes a private capture receipt containing source worktree, HEAD, base and exact overlay digest.
- worktree_reconcile is the only direct destructive convergence primitive. It requires an external detached worktree, a full capture receipt, the original handoff, unchanged HEAD and an exact unchanged overlay digest. It creates a local release commit from the tested handoff index and resets only that external runtime worktree. It never cleans untracked leftovers and never touches the canonical development checkout.
- owned_service_action can status/start/stop/restart/reload/enable/disable a user service without a static project registry entry only after systemd WorkingDirectory is verified as one of the Git worktrees of the resolved project.

This makes the recovery rollout path:
worktree_compare -> worktree_capture_handoff -> tests/build on handoff -> worktree_reconcile -> owned_service_action restart -> health/network probes.

No OpenCode task is required for any step when the desired source/recovery merge is already known.

## Android / ADB direct operations

Android projects no longer need an agent simply to operate an emulator or attached device.

Implemented:
- adb_devices inventory;
- adb_package_state using pm path + pidof;
- adb_package_logcat, restricted to the running PIDs of the requested package and redacted;
- adb install of a repository/worktree APK;
- uninstall, force-stop and explicit-confirm clear-data;
- start_activity with package-bound component and bounded typed string/int/bool Intent extras;
- instrumentation with a package-bound runner and bounded typed extras.

There is intentionally no arbitrary adb shell, push, pull or unrestricted device filesystem access. Gradle wrapper tasks are already covered by the Direct Ops 2 project runner.

## Durable direct jobs and typed batches

Long deterministic work is no longer a reason to start an agent.

Implemented:
- job_start runs the same allowlisted project run_tool surface in a private systemd user transient job, with a fixed runner, bounded timeout and private spec/result files;
- job_status reads durable redacted results even after the transient unit exits;
- job_cancel and job_cleanup are bound to the exact project/worktree that created the job;
- project batch executes up to 20 already-typed project actions sequentially, fail-fast, with no nested batch.

Jobs do not accept an arbitrary executable, shell string or environment. Batch does not enlarge the set of allowed effects; it only reduces orchestration round trips.
