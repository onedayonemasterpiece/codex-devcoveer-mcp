# Process recovery release (2026-09-30)

## Outcome
Preserve completed operations and work checkpoints across lost responses/new chat windows.
Reduce status polling and schema output cost without changing authorization, source/worktree
checks, secret redaction, external side-effect approvals or supported executors.

## Release scope
1. Durable operation receipts keyed by physical checkout and request_key; replay returns the
   stored result, never repeats an uncertain operation. Legacy calls remain supported.
2. Work open/checkpoint/resume with revision checks, bounded operation readback and explicit
   incident association. No guessing the latest incident in a repository.
3. Lightweight running-task summaries, stable terminal timestamps, explicit idle-without-result
   state rather than fabricated model_error; bounded full snapshots.
4. Compact operation-specific discovery and field-level validation errors without payload echo.
5. Retained Direct Ops receipt segments and truthful pagination/coverage metadata.
6. Short instructions and a live recovery acceptance test. Long work uses existing durable jobs.

## Non-goals
No disabling platform or provider checks, permissions, guards, redaction or approval contracts.
No claim to repair ChatGPT's browser stream or client-side catalogue cache from a server patch.
No new agent backend; no new telemetry stack; no blanket deletion of host files.

## Acceptance
- Drop a successful mutation response; retry same key after reopening storage: one side effect.
- Concurrent retries and changed payloads never duplicate mutations.
- Interrupted/unknown outcomes retain operation identity and prohibit automatic replay.
- A second client resumes a work checkpoint and sees outputs, jobs and the next step.
- Work revision conflicts and cross-checkout/cross-incident mixups fail explicitly.
- Existing tools, safety validations and canonical source tests remain passing.
- Deploy only merged tested main; verify installed hashes, readiness and live operation recovery.

## Deployment
Use an isolated exact-base handoff. Preserve the dirty canonical checkout. Build and install
through scripts/install-current-canonical-runtime.sh with deferred restart, retaining rollback
artifacts. Restart only the MCP tunnel after install; do not restart product services.
