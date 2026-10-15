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
