# Project Requirements Guard

Codex DevCoveer treats a project's high-level product requirements as a protected contract, not as implementation notes.

## Contract file

A project may opt in by storing `.devcoveer/requirements.json`:

```json
{
  "schema_version": 1,
  "project": "example",
  "requirements": [
    {
      "id": "CORE-001",
      "statement": "A user can complete the primary product flow without manual fallback.",
      "critical": true,
      "acceptance": [
        "The primary flow is exercised by product acceptance."
      ]
    }
  ]
}
```

Requirements describe outcomes and product obligations. They should not encode incidental architecture unless that architecture is itself an owner requirement.

## Normal development path

There is no approval step on ordinary implementation changes.

When a contract exists:

- `project_probe` automatically includes `requirementsContext`;
- `requirements_context` reads the contract directly through `direct_ops_v2`;
- OpenCode-backed agent tasks receive the same contract automatically in their task prompt;
- Direct Ops guidance tells the operator to preserve the contract and surface conflicts;
- ordinary source edits, tests, builds and deployments keep their existing workflow.

This is deliberately zero-ceremony for tasks that preserve the requirements.

## Changing requirements

The contract itself is protected. Generic `write_text`, delete/move/chmod, `apply_source_patch`, and Source Handoff cannot silently change it.

An intentional requirement change uses `requirements_update` with:

- the complete replacement contract;
- the expected current digest when a contract already exists;
- a concise reason;
- an `authorization_ref` pointing to the explicit user task, decision, voice note or other owner agreement.

The update creates a short-lived authorization bound to the exact new contract digest. A commit that stages the requirements file is denied unless that staged blob matches the authorization. The authorization is consumed after successful commit/push.

A GitHub PR that changes the requirements contract also needs `requirements_change_ref` at merge time. This prevents a requirement edit from being hidden inside an otherwise ordinary implementation PR.

## Critical contour

`critical: true` marks requirements that form the product's critical contour. The implementation may be rewritten, moved between technologies or simplified, but the observable requirement remains in force unless the owner explicitly changes it.

The guard does not pretend that deterministic code can formally prove arbitrary product semantics. It combines:

1. immutable-by-default requirement text;
2. automatic model context;
3. explicit, auditable requirement changes;
4. existing tests, live acceptance and review evidence for implementation correctness.

## Efficiency and safety

This feature is designed to reduce process overhead rather than add it.

Prefer one semantic Direct Ops operation over helper scripts or repeated low-level probes. Use durable jobs for long deterministic work. After a denied call, timeout, lost response or platform safety interruption, read saved job/operation/work state before retrying. Do not attempt the same side effect through an equivalent alternate mechanism merely to bypass a denial.

The requirements guard adds no new top-level MCP tool. Its operations are discoverable through the cache-resistant `direct_ops_v2` surface.
