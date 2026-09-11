# Codex DevCoveer MCP

Private source-of-truth repository for the DevCoveer execution MCP used by ChatGPT to operate Codex and OpenCode on the DevCoveer host.

## Current state

The live host currently has a newer runtime than the historical Git snapshot available for import. The live runtime must be recovered byte-for-byte before this repository becomes deployment authority.

- known live bridge version: `3.8.0` (host-local; exact bytes not yet imported here)
- historical Git snapshot: `3.6.1` from `onedayonemasterpiece/my-data-hub@c15688a9f16df9d9acf4478ebe854993c03b2679`
- installed host path: `/home/dev/.local/libexec/openai-codex-mcp/`
- public entrypoint on host: `/home/dev/.local/bin/codex-mcp-server`
- transport: `codex-devserver-tunnel.service` via the OpenAI secure tunnel
- OpenCode backend: authenticated localhost service (`opencode-devcoveer.service`)

**Do not deploy the historical snapshot as current.** It is provenance only.

## Repository policy

This repository owns the base DevCoveer execution bridge. Product-specific MCPs and adapters stay in their own repositories (for example Dataset Loop). Secrets, credentials, task state, tunnel credentials/IDs, generated runtime state, and user data must never be committed.

GitHub Actions are intentionally not used. Verification and deployment are performed on DevCoveer using the checked-in tests and explicit rollback/readback procedures.

## Operating contract

The canonical development modes, model resource classes, live-stall diagnostics, short-term model-health memory and adaptive failover policy are defined in [`docs/operating-model-and-adaptive-routing.md`](docs/operating-model-and-adaptive-routing.md).

This contract is deliberately stored in Git rather than relying on one ChatGPT conversation or personal memory.

## Recovery sequence

1. Recover the exact currently installed `3.8.0` source and verification files from DevCoveer without changing the running installation.
2. Redact/omit credentials and runtime state; compare hashes with the installed files.
3. Commit those exact source bytes as the first current source checkpoint.
4. Run local regression tests on DevCoveer.
5. Only after exact readback, make this repository the deployment source-of-truth.

See `docs/provenance.md` after the historical import.
