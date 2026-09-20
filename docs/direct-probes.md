# Deterministic direct probes

`project_probe` and `host_probe` are a bounded read-only diagnostic path inside the existing DevCoveer MCP. They return observed facts directly; they never start OpenCode/Codex, create task-registry records, decide GO/NO-GO, or provide a shell-like execution surface.

Use a direct probe when the missing input is a deterministic fact: Git state/objects/diff, exact source text or file metadata, fresh bound GitHub branch/CI state, registered service/listener/log/resource/health/runtime metadata. Use the existing `start_task` / `continue_task` path when research, diagnosis requiring exploration, source modification, testing, deployment, or other execution is actually required. Probes are not a mandatory preflight ritual.

## Contract and limits

Both tools accept `project` plus 1–8 independently evaluated `checks`. The JSON schemas enumerate every operation and parameter; callers cannot supply commands, scripts, argv, cwd, environment overrides, arbitrary GitHub URLs/API paths, HTTP URLs, ports, systemd units, log files, or host roots. The response uses `execution=direct`, observation time, resolved target, duration and per-check status/source/data/error/complete/truncated/redacted information. One failed check does not discard successful siblings. A batch is not globally atomic; worktree reads report detected changes where applicable.

A probe batch has a 60-second safety ceiling and a 64 KiB response ceiling. Checks run independently/concurrently, while each operation has its own bounded timeout: cheap local facts remain short, and GitHub/disk/resource checks may use up to roughly 50 seconds. Text, logs, Git output, traversals, line pages and process output have smaller operation-specific caps. Subprocesses are limited to four concurrent executions and are terminated as a process group on timeout or cancellation. There are no probe task IDs or durable probe jobs.

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

`scripts/install-direct-probes.py` is fail-closed against exact observed production lineage. Dry-run transforms the installed bridge in memory and compiles the candidate together with every direct-probe runtime helper. `--apply` atomically installs only the bridge integration, the bounded direct-probe runtime modules and the trusted config, keeping timestamped rollback copies for pre-existing touched files. The installer never restarts a service. Restart/reload must follow the existing DevCoveer maintenance procedure and must not interrupt active tasks.
