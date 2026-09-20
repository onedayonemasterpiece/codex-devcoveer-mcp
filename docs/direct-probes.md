# Deterministic direct probes

`project_probe` and `host_probe` are a bounded read-only diagnostic path inside the existing DevCoveer MCP. They return observed facts directly; they never start OpenCode/Codex, create task-registry records, decide GO/NO-GO, or provide a shell-like execution surface. Mutating deterministic work is exposed separately through typed `project_action` and `host_action` tools so observability remains read-only.

Use a direct probe when the missing input is a deterministic fact: Git state/objects/diff, exact source text or file metadata, fresh bound GitHub branch/CI state, registered service/listener/log/resource/health/runtime metadata. Use `project_action` for deterministic project execution such as exact fast-forward sync, bounded test/build scripts, registered operational profiles, and test/commit/push of a managed source handoff. Use `host_action` for registered host operations such as a user-service restart. Use `start_task` / `continue_task` only when the work actually requires reasoning, exploratory diagnosis, or an unanticipated operation that the typed direct surface does not cover.

## Contract and limits

`project_probe` and `host_probe` accept `project` plus independently evaluated `checks`. The JSON schemas enumerate every operation and parameter; callers cannot supply commands, scripts, argv, cwd, environment overrides, arbitrary GitHub URLs/API paths, HTTP URLs, ports, systemd units, log files, or host roots. The response uses `execution=direct`, observation time, resolved target, duration and per-check status/source/data/error/complete/truncated/redacted information. One failed check does not discard successful siblings. A batch is not globally atomic; worktree reads report detected changes where applicable.

A probe batch has a 60-second safety ceiling and a 64 KiB response ceiling. Checks run independently/concurrently, while each operation has its own bounded timeout: cheap local facts remain short, and GitHub/disk/resource checks may use up to roughly 50 seconds. Text, logs, Git output, traversals, line pages and process output have smaller operation-specific caps. Subprocesses are limited to four concurrent executions and are terminated as a process group on timeout or cancellation. There are no probe task IDs or durable probe jobs.

## Direct execution boundary

`project_action` is intentionally more liberal than the probes without becoming an arbitrary host shell. Its caller-facing operations are typed:

- `sync_ff` fetches one server-registered remote/branch and fast-forwards only when the fetched SHA exactly matches the supplied expected SHA, the move is a true fast-forward, and tracked/staged state is clean. Untracked files are preserved.
- `run_tool` supports fixed project-local tool classes such as pytest and repository bash/Python/Node/npm scripts. The executable is selected by server code, cwd is confined to the resolved project or managed handoff, and callers cannot provide environment variables or `bash -c`.
- `run_profile` executes an exact argv/cwd/env profile from trusted server configuration. It is used when an operation needs a stable environment flag or command shape, for example canonical runtime installation.
- `handoff_commit_push` works only on a managed `handoff:<id>`, requires clean unstaged/untracked state plus `git diff --cached --check`, and pushes only a non-force `chatgpt/*` branch to a server-registered remote.

`host_action` does not accept arbitrary commands. It can execute trusted host profiles and can restart only a server-registered user service. Self-restart of the MCP tunnel is scheduled through a short deferred systemd unit so the current MCP call can return before the tunnel is replaced.

Direct Ops 2 extends this boundary to ordinary repository writes, structured dirty-worktree reconciliation, typed Git writes, broader repository task runners, registered user-service lifecycle operations and registered host-file writes. See docs/direct-ops-2.md for the implemented surface and remaining to-be capabilities. OpenCode remains the escape hatch for genuinely non-deterministic investigation rather than a remote shell for routine operations.

## Security boundary

The implementation keeps one shared bounded runner with small project/host handler modules; this is file-level separation, not a plugin architecture. Server code constructs argv for a small set of trusted executables and never uses a shell. Git runs with optional index writes, pagers, external diff/textconv, hooks, extension transports and LFS process/smudge execution disabled in probe invocations. Fresh remote reads use a server-configured GitHub repository through the existing `gh` credentials; they never fetch, checkout, clone or materialize the project and bind remote content reads to an exact resolved SHA.

Project paths are repository-relative and reject traversal, symlinks, special files, secret/config/session/database paths and option injection. The same path policy is applied before historical blob/diff content can be returned. Code search prunes dependency/cache/state trees and treats the query literally by default. Binary content is not returned.

Host targets come only from `runtime/direct_probe_config.json`, which is trusted server configuration and contains no secrets. Project source cannot extend this allowlist. Health is fixed loopback HTTP GET/HEAD without caller headers/query/body or redirect following. Runtime package versions use `importlib.metadata` from a configured interpreter without importing the application. `config_presence` is a separate narrow exception that returns only booleans for pre-allowlisted keys; ordinary file/search/hash operations cannot read that config source.

Logs/errors/short bodies are redacted, but the design intentionally does not create a new DLP system: the stronger control is not requesting unnecessary sensitive fields. Service probes do not request process environment or `ExecStart` command lines.

## Initial trusted bindings

The source config includes ordinary GitHub/project operations for every resolved DevCoveer project, plus explicit host bindings for the DevCoveer MCP runtime, my-browser-bridge, VibePublish, Dataset Loop, and Street Story. Registered names are discovered with `host_probe` `inventory`; ChatGPT should not guess unit names, ports, endpoints or operational paths.

The my-browser-bridge binding is observability only: service/listener/log/health/runtime facts belong in `host_probe`, while DOM observation, navigation, clicks, screenshots, console/network inspection, uploads and downloads stay in the dedicated my-browser-bridge MCP. Do not turn `host_probe` into a second browser execution surface.

Health aliases are diagnostic observations only. A non-2xx application result is a valid fact and does not imply the probe itself is broken.

## Deployment

`scripts/install-current-canonical-runtime.sh` is the canonical rollout path. It archives a validated main commit, builds the clean-host runtime, runs candidate MCP acceptance, installs the canonical runtime, and runs wrapper acceptance. For direct self-rollout it can set `DEVCOVEER_SKIP_TUNNEL_RESTART=1`; after candidate and wrapper acceptance the installer returns deployment evidence with `tunnel_restart=DEFERRED` and does not schedule or perform a restart. The caller then invokes `host_action.restart_service` for the registered MCP tunnel after the installer RPC has returned. The clean-host builder includes probes, direct actions, source handoff and the routing policy, so a recovered server retains the same operating contract.

`scripts/install-direct-probes.py` remains a fail-closed legacy probe-only incremental installer; it is not the canonical way to install the complete direct execution surface.
