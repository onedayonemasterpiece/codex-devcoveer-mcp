# Adaptive OpenCode routing v3.10 rollout

Status: implementation candidate; production acceptance required before merge.

The goal is a working production bridge quickly and safely, not an exhaustive model benchmark. This rollout implements `docs/operating-model-and-adaptive-routing.md` without adding an MCP method or silent provider fallback.

## Live lineage reconciled on 2026-09-11

The original candidate was based on an older v3.9 source checkpoint. Real production readback established the current live lineage:

- bridge version: `3.9.0`;
- installed `opencode_backend.py`: `0b769ac4aae681345d6857b0322c8dddb76a499fbb86986bddc39b7ace68e902`;
- installed `codex-chatgpt-bridge.py`: `2006a60b5bdbbed9b6892ce9c0308512a5fc31dceb9fa197726d74157aac57ea`;
- installed `_model_discovery_payload` block: `14903373ede193e18ca9fbcef92bb77ab23c5b99060d81a8e1ebd6551bcbee1a`;
- installed `_read_opencode_task` block: `ccc473d2c50c519ce979c1cee7a3235f4a7ca59890cb6e67abdfa69566f00275`.

The Sep-10 backend drift is legitimate: WRITE shell permission was enabled and the OpenCode `task` tool was disabled. The Sep-11 bridge drift is the Acceptance Gate hardening. Both must be preserved.

The obsolete policy-text hunk was removed from `opencode_backend_adaptive.patch.2`. The installer now supports a fail-closed bootstrap dry-run: it may calculate the new candidate hashes, but `--apply` is impossible until those exact hashes are committed back into the source and re-verified.

## Scope

v3.10 must provide:

- visible OpenCode retry/provider failures instead of opaque `running`;
- progress/stall timestamps and bounded redacted live snapshots before cancellation;
- 1h / 3h / 5h model-health memory;
- historical verification separated from current runtime health;
- Zen -> NVIDIA -> metered reserve routing;
- model-local NVIDIA 429/timeout/EOL handling;
- agent-capability filtering for generic coding tasks;
- no global model mutex and no hidden fallback;
- unchanged terminal Acceptance Gate semantics.

## Phase 1 — source verification

From a fresh checkout of PR #2:

```bash
python3 -m py_compile runtime/model_health.py \
  runtime/bridge_fragments/model_discovery_adaptive.py \
  runtime/bridge_fragments/read_opencode_task_adaptive.py \
  scripts/install-adaptive-routing-v3.10.py
python3 -m pytest -q tests/test_adaptive_routing.py
```

Do not touch the dirty `/home/dev/projects/my-data-hub` checkout.

## Phase 2 — bootstrap dry-run and pin

Run with **no** `--apply`:

```bash
python3 scripts/install-adaptive-routing-v3.10.py
```

Required result:

- exact live backend/bridge lineage matches the hashes above;
- rebased adaptive patches apply cleanly to the live backend copy;
- cancel-neutral patch applies cleanly;
- backend, bridge and `model_health.py` candidates compile;
- JSON reports exact `adaptivePatch2Sha256`, `backendRoutingSha256`, `backendNewSha256`, and `bridgeNewSha256`;
- `candidatePinned=false` is expected during this bootstrap pass;
- active task IDs are reported;
- no installed file or service changes.

Commit those four exact SHA values into the installer/tests, then rerun the same dry-run. The second dry-run must return `candidatePinned=true` and the same candidate hashes byte-for-byte.

Never use `--apply` while candidate hashes are unpinned.

## Phase 3 — active-task gate

Before installation/restart, fresh-read all tasks registered `running` or `cancelling` through the DevCoveer MCP.

- Do not cancel merely because elapsed time is large.
- Reconcile stale registry entries through `read_task`.
- Preserve unrelated work from other chats.
- Disposable rollout probes may be controlled-cancelled after repeated no-progress reads when their result is no longer needed.
- Installer has no active-task bypass.

If meaningful unrelated work is active, wait for a safe maintenance window rather than competing with it.

## Phase 4 — install and host regression

Only with `candidatePinned=true` and a clear active-task gate:

```bash
python3 scripts/install-adaptive-routing-v3.10.py --apply
```

The installer must create timestamped backups, atomically install the three Python files, compile them, and **not** restart anything.

Then run:

```bash
python3 -m py_compile /home/dev/.local/libexec/openai-codex-mcp/*.py
```

Run the existing relevant bridge/OpenCode/Acceptance Gate tests plus `tests/test_adaptive_routing.py`. Re-check the active-task gate immediately before restart.

## Phase 5 — controlled restart and fresh readback

Restart/reload only the existing DevCoveer MCP instance. Do not start a second competing service.

Fresh discovery/readback must prove:

- bridge reports `3.10.0`;
- existing methods remain present;
- `get_task_evidence` and `record_task_review` remain present and functional;
- `read_task`, `list_models`, and `cancel_task` expose the v3.10 descriptions/output semantics;
- input schemas remain compatible.

Only after this readback ask the owner once to refresh ChatGPT plugin methods so subsequent chats receive the new descriptions.

## Phase 6 — minimum live acceptance required before merge

### Acceptance Gate

Run one tiny terminal OpenCode task. Perform `read_task` -> `get_task_evidence(scope=summary)` -> external ChatGPT review -> `record_task_review`. Do not accept model self-report without evidence.

### Zen visibility

Use an existing quota/retry fixture or a microscopic read-only Zen task. `read_task(detail=full)` must expose safe runtime/progress data, including quota category when actually observed, retryability, `lastProgressAt`, stall fields, and a bounded redacted live snapshot. Do not cancel before inspection.

### NVIDIA adaptive routing

Use bounded real probes only. Production acceptance needs:

- a fresh 410/EOL to override stale historical verification;
- one healthy agent-capable NVIDIA model to complete a microscopic task;
- failure of one NVIDIA endpoint not to blacklist siblings;
- same DevCoveer task/session continuation to another model when switching is needed;
- model switch attribution visible correctly in terminal evidence;
- `list_models(provider="nvidia")` to expose current health, active usage, agent eligibility, and health-first suggestions;
- `globalModelLock=false`.

Two stale historical NVIDIA models have already produced real HTTP 410 during pre-rollout diagnostics (`nvidia/nvidia/nemotron-3-nano-30b-a3b` and `nvidia/deepseek-ai/deepseek-v4-flash`). After v3.10 deployment, verify those observations correctly demote/exclude those exact endpoints rather than the provider.

### Metered reserve

Do not spend GigaChat merely to fill an acceptance checklist. Use it only if Zen/NVIDIA are unsuitable or a capability requires it. Confirm the custom `giga1` provider remains present.

## Phase 7 — merge and post-rollout work

If the minimum server acceptance above passes:

- update PR #2 with factual production results;
- remove Draft and merge PR #2;
- update/close issue #1 to match the actual result.

OpenCode Desktop NVIDIA picker divergence is **post-server-acceptance work** unless it blocks the owner's immediate use. Diagnose its actual service/API/cache source before any refresh/cache deletion/config rewrite, and preserve custom `giga1`.
