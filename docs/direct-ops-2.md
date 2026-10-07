# Direct Ops 2 - liberal typed operator surface

Status: production-accepted operator surface as of 2026-09-20. The remaining roadmap is intentionally narrow.

Codex DevCoveer should not start OpenCode for work that is deterministic once the operation is known. The direct surface is intentionally liberal. The hard boundary is narrow: no caller-supplied arbitrary shell/eval, no arbitrary sudo or host path/systemd unit, no secrets/env/cookies/private browser state, no force push, no unconditional destructive reset, and no arbitrary authenticated HTTP request.

Implemented in this slice:

Project reads:
- capabilities returns actual GitHub remotes plus registered project/host capabilities.
- workspace_compare classifies target vs HEAD vs index vs worktree vs untracked with hashes.
- GitHub origin is derived from the real Git remote for any project already resolved under /home/dev/projects; per-project config is no longer required for ordinary GitHub remote operations.

Project writes:
- atomic write_text with optional SHA-256 or must-not-exist precondition.
- text_edit performs one atomic UTF-8 write after all exact match-count preconditions pass in memory, replacing helper-script edit chains without becoming a shell/eval surface.
- mkdir, move_path, delete_file and chmod_executable, repository-relative with secret/symlink/.git denial.
- git_create_branch limited to chatgpt/*, exact git_stage paths, non-force git_commit_push, and exact-SHA git_push_existing for clean committed candidates or an explicit local chatgpt/* branch ref without checkout.
- capture_handoff snapshots a dirty worktree including allowed untracked files into the existing isolated managed handoff without resetting the canonical checkout.
- run_tool additionally supports pnpm/yarn scripts, Gradle wrapper tasks, Make targets and named Python modules.

Host writes:
- service_action supports start/stop/restart/reload/enable/disable for registered user services. Stop/restart are deferred so the current RPC can return.
- write_registered_file atomically updates only a registered host_files alias, with optional expected SHA-256.

Recovery rule:
A dirty production checkout is evidence, not permission to reset. workspace_compare answers which files already match a target release and which are independent recovery changes. capture_handoff preserves the actual overlay before any convergence.

Remaining roadmap:
- generic release/deploy/activate/edge-route primitives and generic deployment receipts, replacing the remaining product-specific deployment operators where that abstraction is actually useful.

Capabilities should evolve independently by contract. They must not be coupled to another project's SHA.


## Local-device boundary

DevCoveer is a server/runtime/deployment host, not an Android-device host.
ADB, emulator/device discovery, package install/uninstall, package logcat,
Intent launching and instrumentation belong to the owner's local workstation
or a dedicated Android CI runner. They are intentionally outside the
Codex DevCoveer operator surface.

Repository build/test tasks such as Gradle wrapper targets remain valid direct
project operations because they do not imply ownership of an Android device.


## Direct Ops 2 host/runtime slice

Implemented after the initial Direct Ops 2 project slice:
- root_read_text, root_file_stat and root_file_sha256 inside registered host roots with the same secret/symlink policy;
- process_tree for registered services, returning executable/cwd plus argv shape and explicitly never reading process environment;
- network probe modes dns/tcp/tls/http for public or loopback targets, with pinned resolved IP, no auth headers, no redirect following, bounded bodies and denial of private/link-local/reserved connect targets;
- mcp_manifest from a registered runtime bridge file, returning runtime version, bridge hash, tool catalogue and catalogue digest for client/server schema-cache diagnostics;
- daemon_reload when at least one registered user service authorizes host control;
- delete_registered_file with alias-only addressing and optional expected SHA-256.

Still to-be:
- generic release materialization/deploy/activate/edge-route receipts.

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

## Durable direct jobs and typed batches

Long deterministic work is no longer a reason to start an agent.

Implemented:
- job_start runs the same allowlisted project run_tool surface in a private systemd user transient job, with a fixed runner, bounded timeout and private spec/result files;
- job_status reads durable redacted results even after the transient unit exits;
- job_wait long-polls for at most 25 seconds and returns on terminal state or meaningful progress, reducing repeated client polling;
- job_cancel and job_cleanup are bound to the exact project/worktree that created the job;
- project batch executes up to 20 already-typed project actions sequentially, fail-fast, with no nested batch.

Jobs do not accept an arbitrary executable, shell string or environment. Batch does not enlarge the set of allowed effects; it only reduces orchestration round trips.

## GitHub write lifecycle

A separate GitHub connector is no longer required for routine repository ownership operations.

Implemented:
- github_pr_create: create or idempotently reuse an open PR from chatgpt/* after verifying the exact remote head SHA;
- github_pr_update: edit title/body only for an open chatgpt/* PR whose head still equals expected_head_sha;
- github_pr_merge: merge/squash/rebase only an open chatgpt/* PR with exact expected head and base;
- github_branch_delete: delete only chatgpt/* remote branches after exact SHA verification;
- github_run_wait: bounded long-poll for one exact workflow run/head SHA, returning on completion or state progress without dispatching or mutating the run.

These operations use the actual GitHub remote of the resolved project (or a stricter configured binding). No arbitrary GitHub API path is exposed.

## Schema refresh and stable Direct Ops gateway

The previous Direct Ops 2 rollout exposed a client-cache failure mode: the live MCP
server returned new variants inside project_probe, project_action, host_probe and
host_action, but the top-level tool names and tool count did not change. A ChatGPT
connector could therefore retain an older JSON schema and reject a valid new
operation before the request ever reached DevCoveer.

The contract now fixes both layers:

- the MCP server advertises tools.listChanged in legacy and 2026-07-28+ protocol eras;
- modern clients receive an initial ToolsListChanged event through
  subscriptions/listen; legacy sessions receive one on their first tools/list;
- direct_ops_v2 is a new top-level tool with a deliberately stable envelope:
  project + plane + operation + payload;
- operation is intentionally not a client-side enum. The server re-validates
  payload against the current strict concrete project/host probe/action schema.
  Adding future typed operations therefore does not require changing the
  ChatGPT-side schema for direct_ops_v2.

The older strongly typed top-level tools remain available for compatibility and
ergonomics when the client has a fresh schema. direct_ops_v2 is the primary
cache-resistant execution surface and should be preferred in long-lived ChatGPT
windows or whenever schema freshness is uncertain. This preference does not remove
or narrow any legacy typed tool, shell-capable run_tool, Source Handoff, OpenCode,
native Codex, or other escape hatch.

Safety-friction diagnostics are advisory rather than admission controls:
- safety_trace summarizes retained Direct Ops receipts for mutation bursts, retry-after-denial, helper-script churn and polling bursts; it never blocks an operation.
- recovery_snapshot reads saved work/operation/job state after a disconnect or platform interruption and proposes readback operations; it never repeats a side effect automatically.
- server-side schema failures are reduced to concise field/constraint messages; full live schemas remain inspectable through direct_ops_v2 operation=describe.


## Frozen-client compatibility carrier

Some ChatGPT turns materialize connector tool schemas once and cannot add a newly
discovered top-level tool until a later discovery cycle. To avoid making the user
restart or move chats, the already-existing project_action/run_tool schema contains
a reserved compatibility carrier:

- tool = python_script
- path = @direct_ops_v2
- args[0] = plane
- args[1] = operation
- args[2:] = concatenated JSON payload chunks

The reserved path is intercepted before filesystem lookup and never executes Python
or shell. The compatibility response wraps probe batches in a top-level status=ok so
legacy project_action clients do not misclassify successful probe results as tool errors. It calls the same direct_ops_v2 server gateway, which re-validates the
stable envelope and then the current strict concrete operation schema. Nested
run_tool through the carrier is denied. This fallback is limited to the old client's
bounded args capacity; fresh clients should use the normal direct_ops_v2 tool.