# Adaptive OpenCode routing v3.10 rollout

Status: implementation candidate; production acceptance required before merge.

This rollout implements `docs/operating-model-and-adaptive-routing.md` without introducing a new MCP method or silent provider fallback.

## Scope

Candidate changes:

- OpenCode `retry` / provider failures remain visible instead of collapsing into opaque `running`;
- safe progress timing and bounded redacted live session snapshot are returned before a task is interrupted;
- model runtime observations are retained for rolling 1h / 3h / 5h windows;
- historical model verification is separate from current runtime health;
- resource order is Zen -> NVIDIA -> metered reserve (currently GigaChat), capability-aware;
- NVIDIA health is primarily model/endpoint scoped; one 429 does not blacklist the provider;
- Zen free-quota exhaustion is provider-wide until recovery/probe evidence appears;
- no global model mutex and no hidden automatic fallback;
- final Acceptance Gate remains terminal, digest-bound and unchanged.

## Source verification

From the repository checkout:

```bash
python -m py_compile runtime/model_health.py \
  runtime/bridge_fragments/model_discovery_adaptive.py \
  runtime/bridge_fragments/read_opencode_task_adaptive.py \
  scripts/install-adaptive-routing-v3.10.py
pytest -q tests/test_adaptive_routing.py
```

The installer also validates the exact known production lineage before writing anything.

## Phase 1 — read-only preflight

Do not touch the dirty `my-data-hub` checkout.

From a clean checkout of this repository on DevCoveer:

```bash
python scripts/install-adaptive-routing-v3.10.py
```

Required result:

- installed `opencode_backend.py` SHA-256 exactly matches the approved v3.9 hash;
- target bridge is exactly v3.9.0 and the two replaced functions match their approved hashes;
- patched candidate compiles;
- active DevCoveer task IDs are reported;
- no files or services are changed.

If any hash or source anchor differs, stop. Re-read the actual installed source and reconcile instead of forcing the patch.

## Phase 2 — active-task gate

Before installation/restart, inspect every task reported as active through the live DevCoveer MCP.

Do not cancel a task merely because it is old or `running`. Use current `read_task` plus available OpenCode session state/export to determine whether progress is still occurring.

If meaningful work is active, postpone restart. If a task is provider-retrying/quota-exhausted, preserve its session and handle it explicitly; do not destroy it as maintenance cleanup.

## Phase 3 — install files, still no service restart

Only after the active-task gate is clear:

```bash
python scripts/install-adaptive-routing-v3.10.py --apply
```

The installer:

- creates timestamped backups of bridge/backend;
- writes `model_health.py`, patched backend and bridge atomically;
- compiles all installed Python files;
- does **not** restart services.

If installation fails after writes begin, backend/bridge are restored from the timestamped backups.

## Phase 4 — host verification then controlled restart

Before restart:

```bash
python -m py_compile /home/dev/.local/libexec/openai-codex-mcp/*.py
```

Run the existing bridge/OpenCode/Acceptance Gate verification suite plus the v3.10 adaptive-routing tests from this repository.

Then re-check that no DevCoveer task became active during verification. Restart only the existing DevCoveer MCP transport/runtime; do not start a competing server.

After restart, verify the existing health/readiness endpoints and perform fresh MCP discovery from an independent client.

## Phase 5 — live product acceptance

### A. Acceptance Gate regression

Confirm the existing methods remain available and functional:

- `get_task_evidence`
- `record_task_review`

Use a tiny terminal OpenCode task and prove `review_required -> accepted/rejected` still binds to the exact evidence digest.

### B. Zen quota / stall diagnostics

Use a tiny read-only Zen task while the free quota is exhausted or retrying when naturally available.

Before any cancellation, `read_task(detail=full)` must expose safe equivalents of:

- provider/session state (`retry` vs generic running);
- `providerErrorCategory=quota_exhausted` when actually observed;
- retryability and provider-supplied timing only if present;
- `lastProgressAt`, `lastToolCallAt`, `stallForMs`;
- bounded redacted live snapshot;
- a switching suggestion without automatically switching.

No secret/provider token may appear.

### C. Same-task failover

Where OpenCode supports it, preserve the same DevCoveer task/OpenCode session:

1. inspect the stalled/retrying Zen turn;
2. controlled interrupt only when justified;
3. `continue_task` with the best currently healthy eligible NVIDIA model;
4. verify the provider/model switch is visible in state/evidence.

Do not create a replacement task unless same-session continuation is technically impossible.

### D. NVIDIA dynamic health

Use bounded real tasks, not a full-catalog benchmark.

Verify at least two eligible NVIDIA coding/agent models. A fresh 429/timeout must demote only that model/endpoint for a bounded cooldown; a healthy sibling remains routable. A fresh 410/EOL hard-excludes the exact endpoint and overrides historical `inference_verified`.

`list_models(provider="nvidia")` should expose recent health/latency aggregates and ranking signals without performing inference itself.

### E. Metered reserve

Use GigaChat only after Zen/NVIDIA are unsuitable/unavailable or where quality requires it. Record real runtime health but do not burn quota on routine probing.

## Phase 6 — Desktop NVIDIA divergence

Only after server-side v3.10 acceptance, diagnose the owner's connected OpenCode Desktop model picker.

Do not blindly delete caches or run model refresh first. Determine whether the Desktop picker is built from the remote server provider API, a server cache, or a desktop-local cache/state. Apply the minimum correction and verify:

- newly expected NVIDIA models become visible/selectable;
- GigaChat remains visible;
- server CLI/DevCoveer/Desktop agree on the intended curated model surface.

A Desktop restart/reconnect is performed only if evidence shows the client requires it.

## Plugin-method refresh

This implementation changes output semantics and tool descriptions but does not add a new MCP method or input field. A ChatGPT plugin method refresh is therefore not required for invocation compatibility, but after production deployment it is desirable so new conversations receive the updated descriptions.

Ask the owner to refresh methods **only after** server-side v3.10 readback and fresh independent discovery succeed.
