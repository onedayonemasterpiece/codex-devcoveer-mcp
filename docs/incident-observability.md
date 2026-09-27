# Incident engineering and observability

This document defines the DevCoveer incident workflow. It is deliberately evidence-first and lightweight for normal development.

## When an incident is required

Create or resume an incident for production/live outage, material degradation, data-integrity/security/safety regression, failed production recovery, repeated live failure, or an unreproduced production bug that needs retained evidence.

Do **not** create an incident for an ordinary unit-test failure, local implementation mistake, expected experiment failure, or routine feature work with no live impact.

## Automatic preflight and correlation

For a prompt that clearly combines a live/production failure with investigation or remediation, start_task automatically opens or reuses the repository incident before the agent starts. The agent receives the incident ID and related-history context. This is not an approval gate: registry/preflight failure is fail-open and does not remove agent/shell capability.

While an incident is open, Direct Ops receipts for the same repository/worktree automatically carry the current incident_id, so retrospective analysis can correlate model actions with application logs.

## Required incident sequence

1. **Open or resume.** Use `incident_search` before remediation. Then `incident_open`; the registry automatically links similar earlier incidents. Read relevant linked incidents with `incident_get`.
2. **Establish facts.** Inspect live service state/health and registered logs. Prefer `host_probe log_search` with a narrow time window/query. Use `logs` only when a tail is enough.
3. **Preserve evidence.** Runtime logs stay in their original source. Screenshots, exports, database snapshots, diagnostic bundles and other copied evidence go to a retained `/home/dev/artifacts` directory with provenance.
4. **Form hypotheses.** Record observations separately from hypotheses with `incident_note`. Do not claim root cause from code inspection alone when runtime evidence exists.
5. **Mitigate/remediate.** Use typed operations when they express the exact action. Full-power agent/shell remains available for legitimate operations absent from the typed surface.
6. **Verify live.** Re-check health and logs after the change. Verification must demonstrate the live symptom is gone; a passing build alone is insufficient.
7. **Close.** `incident_close` requires root cause, resolution, verification and follow-up action items. Postmortems are blameless and focus on system/process improvements.

## Logging standard for live projects

Live services should emit enough structured information to reconstruct what happened without reading source first. Prefer structured JSON to stdout/stderr so systemd/journald captures it.

Useful fields, when applicable:

- timestamp and severity;
- `service.name`, component and environment;
- release/commit/version;
- event/operation and outcome;
- duration;
- stable error type/code (plus concise safe message);
- request/session/job/publication/incident correlation ID;
- trace/span IDs when tracing exists;
- provider/model/adapter for AI operations;
- retry number and upstream status class.

Never log raw passwords, tokens, API keys, cookies, authorization headers, private payloads or complete credential-bearing URLs.

Do not add noisy “entered function” logging. Add logs at decisions, external boundaries, state transitions, retries, failures and success outcomes. New failure-prone production behavior should normally add or improve diagnostic logging in the same change.

## Log investigation

`host_probe inventory` shows registered services and log aliases. Worktrees inherit the binding of the canonical repository.

Use:

- `host_probe log_search(log=..., query=..., since_seconds=..., limit=..., context_lines=...)` for targeted investigation;
- `host_probe logs` for bounded recent tails;
- `host_probe service`, `listeners` and `health` for runtime state;
- host_probe service_config for explicitly registered non-secret environment paths/configuration without dumping the service environment;
- host_probe config_metadata / config_compare for allowlisted config aliases without returning raw values or hashes;
- host_probe root_git_state and host_action root_git_sync_exact for exact registered production-runtime Git readback/sync;
- `project_probe recent_direct_ops` for DevCoveer action history;
- `get_task_evidence` for terminal OpenCode session evidence.

Start with a narrow time range and exact identifiers/error text, then widen only when necessary.

For GitHub workflow dispatches, supply a stable request_key. Reusing the same key after a timeout or lost response performs reconciliation and never issues a second dispatch; use a new key only for an intentional rerun.

## Correlation and future telemetry

Journald is the minimum common live source because it is already present on the DevCoveer host. The event schema should remain compatible with OpenTelemetry semantic conventions so trace/span correlation and a collector/Loki backend can be added without rewriting application logging.

Codex/OpenCode execution evidence and Direct Ops receipts are complementary to application logs: one explains what the agent did; the other explains what the application actually did.

## Repository and worktree model

The observability binding belongs to the GitHub repository, not to one checkout name. Tool-addressable worktrees inherit the canonical repository's registered live services/logs. This avoids observability gaps whenever a new worktree is created.

## Evidence quality rule

For an incident conclusion, identify the evidence class supporting it:

- runtime log/health;
- production API/UI behavior;
- deployment/release receipt;
- source/Git history;
- agent/tool execution evidence.

Source code alone describes intended behavior; it does not prove what happened in production.
