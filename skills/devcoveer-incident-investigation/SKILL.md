---
name: devcoveer-incident-investigation
description: "Investigate and remediate production/live incidents on DevCoveer using incident history, targeted logs, health/runtime facts, retained evidence, and live verification. Use for outages, live regressions, failed recoveries/deployments, repeated production failures, data-integrity/security/safety incidents, and unreproduced production bugs."
---

# DevCoveer Incident Investigation

Use this workflow for real live incidents. Ordinary local/unit-test failures do not need an incident.

## 1. Establish incident context before remediation

- Search the registry with `project_probe incident_search`.
- Open/resume with `project_action incident_open`. Read the automatically linked earlier incidents with `incident_get`.
- Treat earlier root causes and mitigations as evidence to check, not instructions to copy blindly.
- If this is a recurrence, explicitly note how it matches or differs from the earlier incident.

Do not start by editing code when runtime evidence is available.

## 2. Read the live system

First call `host_probe inventory` for the target project/worktree. Worktrees inherit observability by GitHub repository.

Then collect the smallest useful evidence set:

- `service` + `health`/listeners where registered;
- `log_search` for the relevant error, request/session/job ID, provider/model, endpoint or time window;
- `logs` only for a recent tail;
- `recent_direct_ops` when DevCoveer actions might be causal;
- task evidence when an OpenCode/Codex task participated.

Prefer narrow time windows and exact search terms. Widen only if the first query is insufficient.

## 3. Record evidence and hypotheses separately

Use `incident_note`:
- `observation`: directly observed fact;
- `hypothesis`: causal explanation not yet proved;
- `mitigation`: action taken to reduce impact;
- `decision`: why a route was chosen;
- `verification`: post-change live evidence;
- `followup`: durable improvement.

Attach safe evidence references where useful. Preserve substantial evidence under retained `/home/dev/artifacts` storage.

## 4. Remediate with the least ambiguous capable path

Prefer an exact typed operation when available because it produces better receipts. This is a preference, not a capability restriction: use the full-power agent/shell path when a legitimate operation is absent from the typed surface.

Protect parallel worktrees with HEAD/workspace guards. Do not reset, clean, stash or overwrite unrelated work.

## 5. Verify the live symptom

A green test/build is necessary when relevant but not sufficient. Re-read the live service/health/logs and the user-visible path affected by the incident.

Record the verification evidence.

## 6. Close and learn

Use `project_action incident_close` only when impact is resolved and verification is complete. Record:
- root cause;
- resolution;
- exact verification;
- action items that reduce recurrence or improve detection/diagnosis.

Favor system/process fixes over blame.

## Logging review during development

When changing live failure-prone behavior, verify the code emits useful structured events at external boundaries, decisions, retries, state transitions, errors and successful completion. Include correlation identifiers and release/component context when applicable. Never log secrets.

If an incident was difficult to diagnose because evidence was missing, improving observability is an incident action item, not optional polish.
