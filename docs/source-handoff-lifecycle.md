# Source Handoff lifecycle

## Goal

Source Handoff is temporary transport, not a parallel workspace-management system. Normal work should stay in the already selected checkout and use Direct Ops. A managed handoff exists only when an exact ChatGPT-authored patch needs isolation.

The lifecycle must reduce operator work, not add mandatory bookkeeping.

## Fast path

- Identical `project + base SHA + patch SHA-256` reuses the same managed handoff.
- `stage_source_handoff` remains one call and does not require a prior inventory or cleanup step.
- When the managed-handoff pool grows, staging performs a bounded opportunistic sweep of stale handoffs that are already proven safe to reclaim.
- Unique dirty state is never deleted because it is old.
- No cron, background daemon, per-task approval, archive, or extra virtualenv is introduced.

## Direct lifecycle operations

`direct_ops_v2 -> project_probe -> source_handoff_list` is a bounded, read-only repository-wide inventory. It reports activity, age, current change count, default-ref subsumption and `cleanupSafe`.

`direct_ops_v2 -> project_action -> source_handoff_gc` removes only handoffs already classified `cleanupSafe`. It has no force mode. `older_than_seconds` and `limit` bound the sweep.

These operations are for diagnostics and explicit maintenance when useful; they are not required on every task.

## Conservative cleanup proof

A managed handoff is cleanup-safe only when it is not observed as active and one of these is true:

1. its worktree is clean and its HEAD is reachable from a local or remote Git ref;
2. every current local change is already represented by the repository default ref;
3. the worktree is already missing and only managed metadata remains.

Otherwise the handoff stays in review state. In particular, unique dirty changes remain protected regardless of age.

`lastUsedAt` is refreshed on handoff resolution. Opportunistic staging cleanup only considers cleanup-safe handoffs stale for at least one hour, and removes at most a bounded number per staging call.

## Repository identity

Lifecycle ownership follows the Git common directory rather than a single physical checkout path. This lets the same repository inspect and clean managed handoffs even when the checkout that originally staged them has been removed.

Explicit `cleanup_source_handoff` also resolves a surviving worktree of the same repository when the original `projectRoot` no longer exists.

## Observability

Missing optional resource roots are reported as absent/empty metadata rather than surfacing an internal `FileNotFoundError`.

The canonical lifecycle view for Source Handoff is `source_handoff_list`; native Codex worktree diagnostics remain a separate resource profile.
