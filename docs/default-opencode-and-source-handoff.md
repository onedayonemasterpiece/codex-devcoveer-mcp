# Default OpenCode and Source Handoff policy

This document is the durable execution contract for ChatGPT clients using Codex DevCoveer.

## Execution default

A plain `@Codex DevCoveer` request means **OpenCode**. `start_task` with no provider is routed through the existing OpenCode model resolver and its resource/health policy (Zen first, then NVIDIA, then metered reserve as configured).

Native Codex is limited-capacity and **explicit opt-in only**. Use it only when the user explicitly asks for Codex, for example `@Codex DevCoveer Codex`; the client must express that opt-in as `provider="codex"`. A Codex-looking model name by itself does not opt in. There is no silent fallback from OpenCode to native Codex.

`reasoning_effort` is native-Codex-specific and therefore also requires explicit `provider="codex"`.

## Source Handoff fallback

When ChatGPT has uncommitted source but its ChatGPT-side GitHub connector is unavailable or cannot reliably write it:

1. Build a patch against an exact Git base SHA and call `stage_source_handoff`.
2. Use the returned `handoff:<id>` as the project for `start_task`; OpenCode remains the default executor.
3. Continue development and tests in the isolated handoff worktree.
4. Commit and push the accepted result using the stable DevCoveer `git`/`gh` path.
5. Review terminal task evidence and record acceptance.
6. Call `cleanup_source_handoff` after the result is accepted/pushed and no continuation is needed.

The patch is transported through stdin and is not retained. Do not add archive storage, persistent patch files, or per-handoff virtual environments.
