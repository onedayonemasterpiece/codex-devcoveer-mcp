# Codex status and owner development profile

Date: 2026-10-03. Scope: a small additive DevCoveer capability; not an autonomous development platform.

## Interface

`codex_status({})` reads `account/rateLimits/read` through DevCoveer's existing authenticated native app-server client. This is the machine-readable quota source behind the CLI status experience, not an LLM prompt containing `/status`. It performs no inference, purchases no credits, exports no credentials and launches no task.

The result contains an observation time, each returned quota window's used and remaining percentages, duration and reset time when known, plus a conservative account-level eligibility snapshot. Remaining = 100 minus used. The minimum remaining percentage across present windows of the canonical `codex` bucket must be strictly greater than 10. Exactly 10, malformed percentages, missing canonical bucket and expired reset information are not eligible. A populated multi-bucket response takes precedence over the legacy single-bucket response; unrelated buckets are not substituted. Model-specific quota mapping must be explicitly established before unattended model-specific admission.

The status is a read-only observation, NOT a dispatch approval or a guarantee that an arbitrary future task fits. Projects Hub must separately check owner identity, exact approved request revision, model availability, freshness, concurrency and a bounded work policy. No paid/API-key fallback is allowed when the plan quota is exhausted or unknown.

## Requested model

The owner-facing default profile is `gpt-6.1-medium`. Native model and reasoning effort remain separate. When the current native catalogue contains one supported GPT-6.1 record (`gpt-6.1` or `gpt-6.1-sol`) with medium reasoning, the alias resolves to that exact native ID and `reasoning_effort=medium`. No model is invented or silently replaced with Astra/another generation. Missing/ambiguous catalogues fail closed. An explicit conflicting effort with the `-medium` alias is rejected; when changing reasoning, use the canonical model ID plus the separately approved effort.

`start_task` still requires `provider=codex`; generic OpenCode defaults and unrelated consumers remain unchanged. Explicit model selection checks the complete native catalogue, including hidden opt-in records. Ordinary model discovery is unchanged. Catalogue presence is distinct from successful inference and is reported that way.

## Implementation

- `runtime/codex_account.py`: pure normalization/profile helpers and bounded native read.
- Canonical runtime builder adds the tool, schema, validator, dispatch and profile resolution.
- No changes to historical baseline, credential storage, task implementation or application authorization.
- `tests/test_codex_account.py` covers strict threshold, independent windows, malformed values, map precedence, unavailable quota, model ambiguity/conflicts, no secret projection and generated-bridge wiring.

Local source verification: 35 targeted tests passed; full DevCoveer suite 363 passed, 4 skipped. These are source tests, not native account quota or new-model inference acceptance. Runtime readback must be recorded after deploying a validated main commit.

## Product budget boundary

A >10% snapshot cannot mathematically cap an already-running provider turn or concurrent external CLI sessions. The first Projects Hub integration must therefore serialize its own development execution per owner/account, recheck quota immediately before each turn/continuation, avoid automatic child agents and retries, observe quota updates, and interrupt/checkpoint at the reserve guard. Unknown status blocks new inference. If a strict remaining-percent floor cannot be enforced by the provider, report that limitation honestly rather than promising an absolute spend guarantee. Explicitly approved code changes do not grant permission to buy/reset quota or change these guards.

## Primary reference

OpenAI Codex app-server: https://developers.openai.com/codex/app-server/ — `account/rateLimits/read`, `account/rateLimits/updated`, `model/list`. API model documentation is not proof of this CLI account's model entitlement.
