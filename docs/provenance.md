# Provenance and DevCoveer2 recovery

## Recovery boundary

The original DevCoveer host is unavailable after the hosting failure. Its host-local installed runtime and runtime state therefore cannot be treated as a donor or read back during this recovery.

The repository and its Git history are the durable evidence available for reconstruction. Historical host observations remain useful regression evidence, but any path that requires reading or copying files from the old host is obsolete.

Canonical runtime/service locations remain:

- runtime: `/home/dev/.local/libexec/openai-codex-mcp/`;
- public entrypoint: `/home/dev/.local/bin/codex-mcp-server`;
- secure transport: `codex-devserver-tunnel.service`;
- native Codex backend: persistent `codex app-server --stdio`;
- OpenCode backend: authenticated localhost `opencode-devcoveer.service`.

## Historical baseline

A safe snapshot of the earlier installed MCP was committed on 2026-09-01 to:

- repository: `onedayonemasterpiece/my-data-hub`;
- branch: `feat/devcoveer-opencode-dataset-loop-20260901`;
- commit: `c15688a9f16df9d9acf4478ebe854993c03b2679`;
- path: `integrations/devcoveer/openai-codex-mcp/baseline/`;
- observed bridge version: `3.6.1`;
- observed OpenCode version: `1.18.15`.

That snapshot contains `codex-chatgpt-bridge.py`, `opencode_backend.py`, `README.md` and `run-codex-mcp`; its original SHA-256 manifest is preserved under `historical/3.6.1/BASELINE_MANIFEST.json`.

The exact credential-free files are also preserved in this repository under
`historical/3.6.1/source/`. This makes the clean-host reconstruction input durable
and removes any dependency on a surviving old host or temporary bootstrap runtime as a
source donor.

It is a regression/reconstruction baseline, not a production version to deploy unchanged.

## Later accepted lineage

Subsequent checked-in work records the later DevCoveer behavior that existed after the historical snapshot, including:

- adaptive OpenCode routing v3.10 and bounded model-health memory;
- Acceptance Gate-aware task semantics retained by the v3.10 rollout lineage;
- source handoff;
- default OpenCode policy;
- bounded direct project and host probes.

The associated installers contain exact hashes from the previously observed host and are valuable verification evidence. On DevCoveer2 they must not be interpreted as proof that their old host-local base files already exist. Clean-host runtime reconstruction must first establish a complete source-controlled base and then apply/verify the later behavior without regressing the historical and accepted contracts.

## Related lineage

`onedayonemasterpiece/ArtKodex` contains an earlier native Codex app-server integration (`src/artkodex/codex_app_server.py`) with `codex app-server --stdio`, turn lifecycle handling and session recovery. It is useful lineage but is not by itself the current bridge.

`onedayonemasterpiece/dataset-loop-mcp` owns Dataset Loop-specific DevCoveer adapters and remains downstream of the base execution bridge.

`onedayonemasterpiece/events-bot-new` contains the 2026-09-03 secure-tunnel incident record and topology evidence.

## Current recovery rule

1. `main` is the intended source-of-truth. Long-lived feature branches are not production deployment sources.
2. Validate candidate source on DevCoveer2 with the checked-in regression suite before promotion.
3. Use the 3.6.1 snapshot and later accepted host evidence as regression baselines; do not blindly restore either old implementation.
4. Do not invent old runtime state, credentials, task records, tunnel credentials or provider sessions. Recreate those explicitly where required.
5. For DevCoveer2 clean-host convergence use `scripts/install-current-canonical-runtime.sh`. It snapshots an immutable Git ref (default `origin/main`), starts from the pinned source-controlled 3.6.1 baseline, replays the accepted later lineage under exact SHA gates, proves the final local MCP tool surface, and only then switches the public wrapper and restarts the Codex tunnel. The temporary bootstrap runtime is never a build input.
6. After activation, prove localhost OpenCode/Codex execution, MCP discovery, task lifecycle, direct probes and secure-tunnel readback on the new host.
