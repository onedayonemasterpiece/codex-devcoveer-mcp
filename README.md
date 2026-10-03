# Codex DevCoveer MCP

Private source-of-truth repository for the DevCoveer execution MCP used by ChatGPT to operate Codex and OpenCode on the DevCoveer host.

## Operational quality and reuse

The [cross-project reuse design](docs/cross-project-reuse.md), [capability catalogue](docs/quality/reuse-catalog.yaml), [implementation plan](docs/quality/implementation-plan.yaml), and [3 October 2026 audit](docs/audits/2026-10-03-operational-quality.md) distinguish existing shared solutions from proposed improvements. The catalogue is an advisory snapshot; automatic MCP context injection and monitoring are not implemented by this documentation change.

## Current state

The original DevCoveer host is unavailable after the hosting failure, so recovery cannot depend on copying host-local bytes. The repository now carries the latest accepted source lineage needed to rebuild the control plane on DevCoveer2:

- adaptive OpenCode routing v3.10 and model-health logic;
- source handoff support;
- default OpenCode policy;
- bounded direct project/host probes;
- fail-closed installers and regression tests for those layers.

Historical `3.6.1` material remains provenance only and must not be deployed as the current runtime.

Canonical host locations remain:

- runtime: `/home/dev/.local/libexec/openai-codex-mcp/`;
- public entrypoint: `/home/dev/.local/bin/codex-mcp-server`;
- secure transport service: `codex-devserver-tunnel.service`;
- OpenCode backend: authenticated localhost service `opencode-devcoveer.service`.

## Repository policy

This repository owns the base DevCoveer execution bridge. Product-specific MCPs and adapters stay in their own repositories. Secrets, credentials, task state, tunnel credentials/IDs, generated runtime state and user data must never be committed.

Production runtime must be built from a validated `main` commit (or a release tag pointing to such a commit), not from a long-lived feature branch. GitHub Actions are intentionally not required for this repository; source verification and deployment are performed on DevCoveer using checked-in tests plus explicit rollback/readback procedures.

## Source verification

The checked-in regression environment is declared in `requirements-test.txt`. On a clean DevCoveer host run:

```bash
bash scripts/verify-source.sh
```

The verifier creates an ephemeral `uv run` environment using Python 3.12.3, runs the complete `tests/` suite and compile-checks `runtime/`, `scripts/` and `tests/`.

## Operating contract

The canonical development modes, model resource classes, live-stall diagnostics, short-term model-health memory and adaptive failover policy are defined in [`docs/operating-model-and-adaptive-routing.md`](docs/operating-model-and-adaptive-routing.md).

Source-handoff behavior is defined in [`docs/source-handoff-p0.md`](docs/source-handoff-p0.md), direct diagnostics in [`docs/direct-probes.md`](docs/direct-probes.md), and the evidence-first incident/logging workflow in [`docs/incident-observability.md`](docs/incident-observability.md).

## DevCoveer2 recovery sequence

1. Validate the exact candidate source on DevCoveer2 with `bash scripts/verify-source.sh`.
2. Merge only the validated candidate into `main` and fresh-read the resulting main SHA.
3. Reconstruct the runtime under `/home/dev/.local/libexec/openai-codex-mcp/` from that main SHA using the checked-in fail-closed installers; do not restore obsolete historical snapshots.
4. Start the authenticated localhost OpenCode service and the MCP runtime under `dev`; use root only for system-level service/install operations where required.
5. Recreate secure-tunnel credentials/state explicitly, then run local and external readback acceptance before treating the service as restored.
6. Preserve the old successful deployment behavior as a regression baseline, while preferring the current source contract when it is demonstrably better and passes acceptance.


## DevCoveer CLI auth home

DevCoveer2 uses one canonical per-user auth/session tree:

`/home/dev/.local/share/devcoveer/auth/`

Native CLI paths are preserved as symlinks or explicit environment paths so
interactive shells, VS Code terminals, systemd user services and code agents
read and refresh the same underlying credentials instead of creating parallel
copies.

Covered stores include GitHub CLI, Fly CLI, Yandex Cloud CLI, Codex,
OpenCode, Kaggle, Docker, AWS CLI, Supabase CLI fallback state and the OpenAI
tunnel client. Codex is explicitly forced to file-backed CLI and MCP OAuth
credential stores. OpenCode's whole `~/.local/share/opencode` state directory is
shared because it contains both `auth.json` and resumable session/project state.

Bootstrap:

`sudo bash scripts/bootstrap-devcoveer2-auth-home.sh`

Check without printing secrets:

`/home/dev/.local/bin/devcoveer-auth-status`

Log in only where a persisted session is missing:

`/home/dev/.local/bin/devcoveer-login-missing-clis`

Provider API keys intentionally owned by the operator remain in
`/home/dev/.env`; CLI-created OAuth/login state does not.
