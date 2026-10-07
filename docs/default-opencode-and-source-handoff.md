# Default OpenCode and Source Handoff policy

This document is the durable execution contract for ChatGPT clients using Codex DevCoveer.

## Execution default

A plain `@Codex DevCoveer` request means **OpenCode**. `start_task` with no provider is routed through the existing OpenCode model resolver and its resource/health policy (Zen first, then NVIDIA, then metered reserve as configured).

Native Codex is limited-capacity and **explicit opt-in only**. Use it only when the user explicitly asks for Codex, for example `@Codex DevCoveer Codex`. A native task start requires two independent signals: `provider="codex"` and `codex_user_opt_in`, which must be a verbatim excerpt from the current user message containing standalone `codex`. The bridge stores only a SHA-256 authorization receipt, not the raw excerpt.

The plugin/product identifier `codex-devcoveer` is deliberately excluded from the standalone-word match. Merely connecting or naming the plugin therefore cannot authorize native Codex. A Codex-looking model name by itself also does not opt in. There is no silent fallback from OpenCode to native Codex.

An already-running native Codex task created under this policy carries its authorization receipt, so ordinary continuation of that same task remains possible without repeatedly asking the user to restate `Codex`. Legacy/unreceipted native tasks require a fresh explicit opt-in before they can be continued. Starting a separate native Codex consultation is a new inference and requires its own explicit opt-in.

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
