# Provenance and recovery

## What is known

The DevCoveer execution MCP existed and operated before this repository was created. Its installed executable source is host-local rather than a Git checkout.

Known host paths and services:

- source: `/home/dev/.local/libexec/openai-codex-mcp/`
- entrypoint: `/home/dev/.local/bin/codex-mcp-server`
- secure transport: `codex-devserver-tunnel.service`
- native Codex backend: persistent `codex app-server --stdio`
- OpenCode backend: authenticated localhost `opencode-devcoveer.service`

The current live bridge has been observed as version `3.8.0`, but its exact current bytes have not yet been imported into this repository. Until that readback is complete, this repository is **not** deployment authority.

## Historical Git snapshot

A safe snapshot of the installed MCP was committed on 2026-09-01 to:

- repository: `onedayonemasterpiece/my-data-hub`
- branch: `feat/devcoveer-opencode-dataset-loop-20260901`
- commit: `c15688a9f16df9d9acf4478ebe854993c03b2679`
- path: `integrations/devcoveer/openai-codex-mcp/baseline/`
- observed bridge version: `3.6.1`
- observed OpenCode version: `1.18.15`

The source snapshot contains at least `codex-chatgpt-bridge.py`, `opencode_backend.py`, `README.md`, and `run-codex-mcp`. Its original SHA-256 manifest is preserved under `historical/3.6.1/BASELINE_MANIFEST.json`.

This snapshot is historical evidence only. It must not overwrite the newer live runtime.

## Related lineage

`onedayonemasterpiece/ArtKodex` contains an earlier native Codex app-server integration (`src/artkodex/codex_app_server.py`) with `codex app-server --stdio`, turn lifecycle handling and session recovery. It is useful lineage but is not the source owner of the present DevCoveer bridge.

`onedayonemasterpiece/dataset-loop-mcp` contains source-controlled Dataset Loop-specific DevCoveer operator/runtime adapters. Those are downstream/product-specific and are not the base Codex DevCoveer MCP.

`onedayonemasterpiece/events-bot-new` contains the 2026-09-03 transport incident record documenting the live secure-tunnel topology and confirming that the installed bridge was operational state under `~/.local`, not a Git checkout.

## Recovery rule

The first current source checkpoint in this repository must be produced from the **actual installed runtime**, not reconstructed from the 3.6.1 snapshot.

Use `scripts/recover-live-runtime.py` on DevCoveer. The utility is deliberately read-only with respect to the installed runtime: it inventories regular files, hashes them, rejects symlinks and secret-looking content, and copies eligible source text only into an ignored local `.recovery/` staging directory.

Required sequence:

1. Ensure no source mutation, service restart, deploy, or task cancellation is part of the recovery step.
2. Run the recovery utility against the fixed installed source directory.
3. Confirm the observed bridge version is exactly the expected live version.
4. Review the generated inventory and candidate files for secrets/runtime state.
5. Compare hashes to the installed files independently.
6. Promote reviewed source into the repository in a normal branch/commit.
7. Run the existing DevCoveer verification suite against the recovered source.
8. Only then define a source-controlled install/rollback procedure and make this repository deployment authority.

No GitHub Actions are required or planned for this repository.
