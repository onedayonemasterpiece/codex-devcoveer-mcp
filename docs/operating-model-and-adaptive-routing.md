# DevCoveer operating model and adaptive model routing

Status: canonical product/operations contract.

This document defines how ChatGPT, OpenCode/DevCoveer, DAEP, Kaggle and Codex are used together, and how OpenCode models are selected and switched at runtime. It is intentionally independent of any one ChatGPT conversation or model-memory state.

## 1. Three development modes

### Mode A — ChatGPT-led, GitHub-first, OpenCode-operated

Default mode for interactive product development.

- ChatGPT owns reasoning, source implementation, review and GitHub changes.
- Source changes are authored by ChatGPT through GitHub, not delegated to Codex or OpenCode unless the owner explicitly changes the mode.
- OpenCode on DevCoveer is the execution/operations plane: diagnostics, tests, environment checks, configuration, migrations, deployment, smoke and acceptance work.
- DevCoveer MCP is the control/observability plane between ChatGPT and OpenCode.
- Terminal OpenCode work is not trusted from self-report alone. ChatGPT reviews MCP state and session evidence and records the acceptance verdict.
- Codex is not a hidden executor or fallback in this mode.

Typical flow:

`ChatGPT -> GitHub source change -> OpenCode/DevCoveer test/config/deploy -> MCP evidence -> ChatGPT acceptance`

### Mode B — DAEP distributed OpenCode execution

Use for a large task submitted as a prompt through OpenCode Desktop/DAEP when parallel decomposition is more valuable than ChatGPT authoring every source edit directly.

- DAEP decomposes the task into logically independent units.
- Most implementation runs in disposable/isolated Kaggle + OpenCode workers.
- DevCoveer is primarily coordinator, integration point, merge/assembly point, final verification and deployment host; it should not become the compute bottleneck.
- Worker outputs must be attributable and reintegrated through the project repository/evidence path.
- DevCoveer performs final integration tests, environment-specific configuration, acceptance and production rollout.
- Codex is not used unless the owner explicitly changes to Mode C.

Typical flow:

`OpenCode Desktop prompt -> DAEP decomposition -> Kaggle/OpenCode parallel workers -> Git/GitHub integration -> DevCoveer final test/deploy -> acceptance`

### Mode C — Codex premium/complex execution

Use explicitly for unusually complex tasks, tasks needing especially high quality/speed, or cases where the owner intentionally chooses Codex.

- Codex use is explicit, never a silent fallback from OpenCode.
- Model/reasoning selection remains visible in task metadata.
- Existing evidence, review and production safety rules still apply where applicable.

## 2. OpenCode model resource classes

OpenCode routing must treat providers as different resource classes rather than as a static ordered list of model names.

### Class R1 — renewable burst resource: OpenCode Zen

Examples: free Zen models.

Properties:
- preferred first;
- free;
- quota can be exhausted temporarily;
- quota normally recovers on a timescale materially shorter than a day;
- quota exhaustion is temporary runtime state, not EOL and not a permanent blacklist.

Default routing priority: first.

### Class R2 — bounded free resource: Groq GPT-OSS-120B pool

Current provider IDs: `groq`, `groq2`, `groq3`, all serving GPT-OSS-120B.

Properties:
- preferred immediately after Zen;
- three separately configured owner credentials are available to the router;
- each key is independently rate-limited and must use the existing limiter/health accounting;
- provider/key rotation is `groq -> groq2 -> groq3`;
- a rate limit on one key does not justify skipping the remaining admitted keys.

Default routing priority: second.

### Class R3 — elastic/unpredictable resource: NVIDIA

Properties:
- normal working resource, not treated as scarce;
- availability and latency can change quickly and non-uniformly between models;
- one model may be fast while another is slow/429, and this can reverse later;
- 429/rate-limit and latency observations should normally be model/endpoint scoped before being generalized to the whole provider;
- hard 410/EOL is permanent for that exact endpoint until catalog evidence changes.

Default routing priority: third.

### Class R4 — metered/scarce resource

Current example: GigaChat. Future examples may include other explicitly metered providers.

DevCoveer owner credential pool:
- `GIGACHAT_API_KEY`;
- `GIGACHAT_API_KEY2`;
- `GIGACHAT_API_KEY3`.

The historical integration used local `gpt2giga` OpenAI-compatible gateways rather than
passing a GigaChat credential directly to the OpenCode process. DevCoveer2 recovery must
therefore preserve the three owner credentials and verify the actual local provider/gateway
topology before enabling them. It must never collapse a three-key pool to one
`GIGACHAT_CREDENTIALS` field merely because one historical gateway used that compatibility
name.

Properties:
- expected to be comparatively reliable/fast when available;
- consumes a limited token/quota budget;
- do not spend on routine health probing while R1/R2 can do the work;
- use as fallback or when quality/capability requirements justify it;
- every admitted `giga*` provider is tracked separately for health and usage.

Default OpenCode routing priority: fourth.

Canonical default order for ordinary OpenCode execution:

`Zen -> Groq GPT-OSS-120B (groq -> groq2 -> groq3) -> NVIDIA -> GigaChat`

The order is capability-aware: a provider/model that cannot satisfy required tools, context or modalities is skipped.

## 3. Never cancel a merely "running" task blindly

`running` alone is not evidence of progress and not evidence of a stall.

Before interrupting an OpenCode turn, ChatGPT should inspect a safe live session/runtime snapshot and determine:

- current provider/model;
- provider runtime state (`busy`, `retry`, provider error, etc.);
- timestamps of last assistant event, tool call, token/progress event and provider-state transition;
- whether message/tool/token counters advanced since the previous observation;
- retry attempt and provider message/category when available;
- elapsed time since last real progress;
- recent latency baseline for the selected model;
- whether another active task is successfully using the same model/provider.

Decision rules:

1. If progress is still advancing, do not cancel merely because the wall-clock duration is long.
2. If the provider explicitly reports quota exhaustion/429/temporary unavailability, do not keep polling indefinitely.
3. If there has been no progress beyond a dynamic stall threshold derived from recent model latency, inspect the safe live session tail before intervention.
4. Prefer preserving the same DevCoveer task and OpenCode session.
5. When an active turn must be interrupted, use controlled `cancel_task` followed by `continue_task` on the same task/session with the next selected model/provider.
6. Create a replacement task only when same-session continuation is technically impossible; preserve an explicit original/replacement link.

Final acceptance evidence remains terminal and immutable. Live diagnostic snapshots are operational observations and must not be confused with terminal acceptance evidence.

## 4. Required live task observability

`read_task` is the primary status interface. It should expose enough safe information to avoid blind cancellation without creating another generic tool if possible.

For OpenCode tasks, output semantics should include fields equivalent to:

```text
executionStatus
acceptanceStatus
provider
model
sessionId
turnId
providerRuntimeStatus
providerErrorCategory
retryable
retryAfter
resetAt
observedAt
lastProgressAt
lastMessageAt
lastToolCallAt
progressSequence
messageCount
toolCallCount
tokenCount
stallForMs
suggestedAction
```

Only provider-supplied `retryAfter/resetAt` values may be reported as such; never invent them.

For `read_task(detail=full)` on a running OpenCode task, the desired behavior is a safe/redacted live session snapshot or tail with event timestamps. This is for diagnosis before cancellation. Raw unredacted sessions and secrets remain unavailable.

Terminal `get_task_evidence` / `record_task_review` semantics remain separate and unchanged.

## 5. Short-term model behavior memory

DevCoveer should keep a small durable, non-secret runtime telemetry cache. It is operational state, not Git source and not ChatGPT memory.

Track per provider+model at minimum:

```text
lastAttemptAt
lastSuccessAt
lastFailureAt
lastLatencyMs
activeTasks
activeSessions
lastProviderErrorCategory
lastCapacityError
cooldownUntil
hardUnavailableReason
```

Maintain rolling aggregates for approximately 1h, 3h and 5h:

```text
attempts
successes
successRate
p50LatencyMs
p95LatencyMs
rateLimitCount
quotaExhaustedCount
timeoutCount
hardFailureCount
```

Observations age out automatically. Recent behavior matters more than historical verification receipts.

Do not implement a global model mutex. Default concurrency policy is optimistic/parallel. Restrict only when a provider-declared or empirically demonstrated capacity constraint exists.

## 6. Adaptive selection within NVIDIA

Do not designate one NVIDIA model as permanently "best".

First filter by task capabilities:
- coding/agent/tool-call support;
- reasoning requirement;
- context/output size;
- attachments/modalities when required;
- current catalog/EOL state.

Then rank eligible NVIDIA models using recent runtime evidence. Prefer models that are:
- recently successful;
- currently low-latency relative to peers;
- not in a 429/retry cooldown;
- not showing repeated timeouts;
- not overloaded by current active work when equivalent alternatives are healthier.

A recent fast streak is useful but decays quickly. A new sequence of 429s should demote the model immediately for a bounded cooldown without blacklisting the whole NVIDIA provider.

When no useful recent telemetry exists, perform the minimum bounded probe necessary for the real task rather than probing the entire catalog.

## 7. Circuit-breaker semantics

### Zen quota

- classify exact quota/free-usage exhaustion as temporary;
- if evidence indicates a provider-wide shared quota, open a provider-level circuit breaker;
- otherwise start model-scoped;
- periodically permit a small probe after cooldown so recovered free capacity re-enters the pool automatically;
- historical `inference_verified` never overrides a fresh quota failure.

### NVIDIA

- 429/rate limit: temporary model/endpoint cooldown unless broader evidence exists;
- timeout/high latency: temporary score penalty, not EOL;
- 410/EOL: hard endpoint exclusion until authoritative catalog state changes;
- a healthy sibling model keeps NVIDIA available.

### Metered/scarce providers

- avoid routine probing;
- use when higher-priority resource classes are unavailable/unsuitable or when capability/quality requirements justify the spend;
- record real runtime health so a known-bad metered provider is not selected merely because it is expensive.

## 8. Failover procedure for one OpenCode task

1. Start with the highest-ranked eligible Zen model.
2. Observe real progress, not just task `running` state.
3. On suspected stall, inspect live safe session/runtime state.
4. On confirmed Zen quota or stall with no progress, preserve the task/session and switch to the Groq GPT-OSS-120B pool.
5. Rotate the admitted Groq credentials in order `groq -> groq2 -> groq3`, respecting per-key limiter/health state.
6. After bounded Groq attempts, switch to the best currently ranked eligible NVIDIA model.
7. If that NVIDIA model 429s/stalls, update short-term telemetry and try the next ranked NVIDIA model rather than abandoning the provider immediately.
8. After bounded NVIDIA attempts, switch the same task/session to GigaChat.
9. No silent fallback: provider/model switches must be visible in task state/evidence.
10. On terminal OpenCode turns, review exact terminal evidence and record accepted/rejected.

## 9. Separation of static catalog, historical verification and live health

These are different facts and must remain separate:

```text
catalog_listed
provider_connected
provider_available
historically_verified
currently_healthy
temporarily_unavailable
hard_unavailable/eol
```

A model can be catalog-listed and historically verified while currently quota-exhausted, rate-limited, slow or EOL. Routing must use current runtime health with timestamps rather than treating an old verification receipt as current availability.

## 10. Persistence and source of truth

- This repository is the durable source of truth for this operating contract.
- Short-term model runtime observations belong in sanitized DevCoveer runtime state, not Git.
- ChatGPT personal memory is optional convenience only and must not be required for correct operation.
- Any future ChatGPT session should be able to recover this policy from the repository/MCP without the owner repeating it.
- Product-specific source repositories remain separate; this document governs execution/routing behavior, not their implementation content.

## 11. Rolling 24-hour operating success criteria

The operating model is considered healthy only when a rolling 24-hour retrospective of the real DevCoveer task registry and terminal evidence shows that ChatGPT is using the control plane as designed, not merely describing the policy correctly.

Success criteria for default Mode A:

1. **Direct-read routing:** zero OpenCode tasks whose primary purpose is to obtain deterministic facts already supported by `project_probe`, `host_probe`, `list_models`, `list_tasks`, `read_task`, or `get_task_evidence`. Unsupported read-only operations are allowed, but the missing direct capability should be explicit.
2. **ChatGPT source authorship:** repository code changes are authored by ChatGPT. When ChatGPT cannot write the repository directly, `stage_source_handoff` is the normal transport for the exact ChatGPT-authored patch. The handoff executor may verify, test, commit, push, or deploy; it should not independently rewrite the supplied source.
3. **ChatGPT documentation authorship:** zero OpenCode tasks whose primary purpose is writing or rewriting repository documentation (`README`, Markdown, operator docs, runbooks, or similar source-controlled prose). Documentation is source and follows the same authorship rule as code.
4. **Source Handoff coverage:** for repository changes that require DevCoveer transport because no reliable ChatGPT-side write path is available, Source Handoff coverage should be 100%. A normal handoff task should show validation/execution activity, not model-authored source edits.

Allowed exceptions are explicit owner-selected DAEP/Codex/agent-authoring modes, runtime configuration or mutation, service restart/deployment, actual model inference/smoke tests, and unsupported operations that genuinely require an executor.

The retrospective should use `list_tasks` plus terminal evidence for suspected violations and report at least:

```text
direct_read_agent_violations
source_authoring_agent_violations
documentation_authoring_agent_violations
source_handoff_required
source_handoff_used
source_handoff_coverage
```

A successful next-day check targets zero for the three violation counters and 100% `source_handoff_coverage` whenever Source Handoff was required. If a violation occurs, fix the routing/reminder/tool contract rather than relying on the owner to repeat the rule in every chat.

## Clean-host runtime acceptance (3.12)

The canonical builder now includes the same ordered routing policy in model
listing and task creation. Only free Zen models, the Groq GPT-OSS-120B key pool,
agent-capable NVIDIA models, and configured GigaChat providers are eligible for
automatic selection. Missing providers are skipped; an explicit model/provider
selection is preserved. Native Codex always requires `provider=codex`.

For automatically routed tasks, observed provider errors or retry states can
continue the same OpenCode session on the next healthy eligible selection.
Attempts and reasons are retained in the task registry. Rotation is bounded to
16 selections per task, including at most three NVIDIA endpoints, so the metered
reserve can be reached. A merely busy session is never switched because it is
slow. Zen quota exhaustion cools down the Zen provider; NVIDIA failures remain
endpoint-specific. Temporary cooldowns expire and allow recovery probes.

Completed, failed, and interrupted turns produce content-addressed redacted
session evidence under the private runtime state directory. The task reader and
background monitor capture it; `get_task_evidence` exposes summary, suspicious,
or full-safe views. Hidden reasoning is excluded. `record_task_review` requires
the exact current digest and records an external accepted/rejected verdict.
Continuation invalidates the previous acceptance while keeping old evidence.
The review MCP is for the external controller, never for OpenCode itself.

Codex auth-home validation compares resolved paths so the canonical auth store
and its legacy symlink are treated as the same directory. The installer requires
a commit on local `main`, checks evidence tools before switching, and retains its
source, rollback copy, and failed candidates in a managed central artifact task.


## 12. Reuse advisory context (Phase 1)

Before a reasoning task begins, DevCoveer prepends a compact cross-project reuse map after Requirements Guard context. The same function is used for OpenCode start/continuation and native task prompts. The map is bounded and marked untrusted/advisory; it cannot grant authorization, rewrite requirements or require a particular implementation.

The model selects relevant donors semantically from the task. If more detail is needed, use the existing direct probe surface to expand at most five explicit cards. Polling task state does not repeat full cards. If the installed catalog cannot be read, execution continues with an explicit advisory-degraded state while Requirements Guard and normal authorization remain unchanged.
