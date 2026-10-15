# Default OpenCode and Source Handoff policy

This document is the durable execution contract for ChatGPT clients using Codex DevCoveer.

## Execution default

A plain `@Codex DevCoveer` request means **OpenCode**. `start_task` with no provider is routed through the existing OpenCode model resolver and its resource/health policy (Zen first, then NVIDIA, then metered reserve as configured).

Native Codex is limited-capacity and **explicit opt-in only**. The caller selects `provider="codex"` only following an explicit owner request for a native Codex work task. No verbatim user-message field is needed: a forwarded quote was unauthenticated and unreliable for Dot, so it did not independently verify consent. The plugin name, model name and quota state never authorize a native work task; default routing remains OpenCode.

Merely connecting or naming `codex-devcoveer` does not authorize native Codex. The caller must have explicit owner task-level consent before passing `provider="codex"`; the bridge never silently falls back to native Codex.

Continuation of an existing native Codex task uses its saved task/thread identity and explicit owner instruction to continue. Legacy task history does not require a forwarded verbatim quote; mere thread inspection is read-only and cannot authorize a new task.

`consult_model` is a separate advisory surface. Native Codex profiles in the owner-authorized consultation ladder may be selected without a per-message `Codex` mention when the Codex quota gate is strictly above the 10% reserve. That standing consultation authorization does **not** authorize a native `start_task`: working tasks still require the explicit task opt-in above.

`reasoning_effort` is native-Codex-specific and therefore also requires explicit `provider="codex"`.

The account quota/reserve gate is only a capacity check and never substitutes for user authorization.

## External Codex Native VS Code thread continuation

An exact `codex://threads/<UUID>` or native UUID already created in VS Code
can be continued through `continue_task` without creating a new task. This
is separate from `start_task` and is **never automatic**: a user must request
continuation of that specific thread. The bridge cross-checks a verified local
rollout with the app-server's `thread/read` for the same UUID, original cwd and
source. It preserves the original native model/reasoning and uses
`thread/resume` plus `turn/start` on that UUID, not `thread/start`.

An external session is admitted only from the trusted operator home or an
existing registered project cwd. Unknown locations, mismatched projects,
active turns, unverified source, incompatible providers and failed effective
sandbox verification are rejected without a new turn. Default access is
read-only. Explicit `access="write"` uses the existing restricted
`devcoveer-write` profile and its secret-denial rules; it does not grant
arbitrary home/host writes. Never inherit unverified external sandbox rights.
When a `turn/start` response is lost, return outcome_unknown rather than
replaying an operation that might already have started. Poll with
`read_native_thread` for exact external native IDs.

## Native Codex write tasks: connected MCP approval

A native Codex task launched through `start_task(provider="codex", access="write")`
(or upgraded to `access="write"` by `continue_task`) runs unattended with
`approvalPolicy="never"`. By default, Codex therefore rejects MCP tools not
marked read-only *before calling the MCP server*: `direct_ops_v2(operation=describe)`
is affected because its surface can also perform mutations.

Only for an explicitly authorized native **write** task, DevCoveer supplies a
per-thread Codex config override setting `default_tools_approval_mode="approve"`
for the exact owner DevCoveer app `asdk_app_6a8b3564e3fc81918b210222d5dbb212`.
It does not set `apps._default`, change the user's `~/.codex/config.toml`,
modify other apps' approval policies, change the sandbox, or pretend
`direct_ops_v2` is read-only. The restricted `devcoveer-write` profile
and the server's own exact-target/allowlist validation remain active.

Read-only native tasks receive no such override. They should use
`project_probe`, `host_probe`, `read_native_thread`, and other genuinely
read-only capabilities. An external VS Code thread explicitly continued with
`access="write"` uses the same restricted per-thread policy. On a lost
`turn/start` reply, always read the original thread before resubmitting.

## Parallel isolated development worktrees

Native Codex write tasks may use a task-owned Git worktree when genuinely
needed for concurrency. Use the typed `managed_worktree` action through
`direct_ops_v2` and the exact native thread ID. See
[managed-native-worktrees.md](managed-native-worktrees.md) for create,
integration and cleanup. This is an exception to working in the already
selected checkout, not permission to create arbitrary clones. Clean,
tested changes merge into the original clean target, followed by
non-forced worktree removal; conflicts and dirty or active states
preserve the worktree with a concrete blocker.

## ChatGPT source and documentation authorship

In the default ChatGPT-led mode, repository code **and repository documentation** are authored by ChatGPT. Do not use `start_task` to offload implementation, README edits, Markdown rewrites, or other repository source authoring to a weaker OpenCode model unless the owner explicitly changes the development mode or asks for agent authorship.

Before starting an agent task:

- deterministic Git/source/CI reads go to `project_probe`;
- registered service/log/listener/resource/runtime reads go to `host_probe`;
- model/provider state goes to `list_models`;
- task state/evidence goes to `list_tasks`, `read_task`, and `get_task_evidence`.

OpenCode remains appropriate for unsupported investigation, runtime mutations, tests, deployment, real inference/smoke work, and other execution that actually needs an agent.

## Source Handoff fallback

When ChatGPT has authored an exact repository change but its ChatGPT-side GitHub connector is unavailable or cannot reliably write it:

1. Build the exact patch against an exact Git base SHA and call `stage_source_handoff`.
2. Use the returned `handoff:<id>` as the project for `start_task`; OpenCode remains the default executor.
3. The handoff agent verifies the supplied patch and may run tests, commit, push, or deploy it. It should not independently rewrite repository code or documentation.
4. If verification finds a source defect, return the evidence to ChatGPT; ChatGPT authors the revised patch and sends a new/continued Source Handoff.
5. Review terminal task evidence and record acceptance.
6. Call `cleanup_source_handoff` after the result is accepted/pushed and no continuation is needed.

The patch is transported through stdin and is not retained. Do not add archive storage, persistent patch files, or per-handoff virtual environments.
