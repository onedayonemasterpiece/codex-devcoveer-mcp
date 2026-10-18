# Model consultant

DevCoveer exposes `consult_model` for an optional independent second opinion.

The consultant is deliberately narrower than `council_run`:

- one model per consultation;
- NVIDIA aliases `kimi_k3` and `deepseek`, quota-gated native Codex profiles,
  and the installed Claude Code alias `claude_opus_5_5`;
- read-only OpenCode outcome; NVIDIA consultations use the build agent with every mutation-capable tool explicitly disabled;
- no automatic fallback to another model;
- advisory output only: it cannot change requirements, source or runtime state;
- the caller continues to own the decision and may ignore the consultation.

Supported purposes are `requirements`, `edge_cases`, `architecture`,
`code_review`, and `ideas`.

The call returns the ordinary durable DevCoveer `taskId`. Poll it with
`read_task`. When the consultation reaches a terminal state, DevCoveer writes
a redacted Markdown result under
`/home/dev/.local/share/openai-codex-mcp/model-consultations/<taskId>.md`
and returns its path, SHA-256 and size in `consultationArtifact`.

The Markdown file contains the selected model, purpose, terminal status,
latency, question digest and the redacted final consultation response. It does
not duplicate the original question or hidden reasoning.

A provider error, timeout, rate limit or unavailable model is reported for the
selected model. `consult_model` does not silently switch between Kimi and
DeepSeek: this keeps reliability statistics attributable to the model that was
actually requested.

A terminal provider turn with no visible text is not considered successful. DevCoveer records it as failed with errorCategory=empty_output and the Markdown artifact records the failure rather than presenting an empty response as advice.


## Provided-context-only product analysis

Multi-user products must not use ordinary project-readable consultation as tenant
isolation. For a caller-authorized evidence bundle, call `consult_model` with
`context_mode=provided_only`, a non-empty `evidence_bundle`, and a stable
`request_key`.

In this mode DevCoveer runs the provider session in a dedicated empty directory,
does not inject project requirements/reuse context, denies the session permission
surface, and disables file/search/web/subagent/mutation tools. The model therefore
receives the question plus the supplied evidence bundle, not the host project.

`request_key` is durably reserved before provider dispatch. Replaying the same
key and identical request returns the existing task instead of starting another
inference. Reusing a key with a different request is rejected. If delivery to
OpenCode times out after a provider session exists, the task becomes
`dispatch_unknown`; `read_task` reconciles that same provider session and
callers must not blind-retry with a new key.

The legacy project-readable mode remains available for trusted owner/operator
consultation and is not a substitute for product tenant isolation.


## Provided-context-only council

Tenant-scoped product analysis may use `council_run` with
`context_mode=provided_only`, a caller-authorized `evidence_bundle`, and a
stable `request_key`. This remains distinct from the trusted project-readable
council.

`tier=free` remains strictly OpenCode-only and never silently falls back to
NVIDIA. `tier=pro` is the product council and runs exactly Kimi K3 plus
DeepSeek V4.1 Flash.

NVIDIA is treated as a slow bounded-capacity resource, not as an end-user
budget confirmation surface. No confirmation token is required. Provided-only
NVIDIA rounds use the direct NVIDIA chat-completions transport: the model
receives only the constructed council prompt/evidence, with no OpenCode
session, project filesystem, web tool or subagent capability.

The NVIDIA council credential pool is intentionally assembled from exactly two
trusted project-owned EnvironmentFiles:

- `/home/dev/.config/opencode/devcoveer-providers.env`;
- `/home/dev/.local/state/regional-knowledge-base/service.env`.

Across those sources DevCoveer requires two distinct non-empty NVIDIA
credentials. They map to two cross-process slot locks, so at most two NVIDIA
inference calls execute concurrently across users/processes. A third provider
call waits for a slot within the bounded council deadline. Missing sources keep
capacity below two and fail closed; duplicate credential values across projects
still count as one slot. Credential values are never returned in task records,
logs or tool results.

Every result remains attributed per model. Provider 429/5xx failures use bounded
retries; credential/validation failures are terminal. Capacity waiting is
separate from provider execution: once a slot is acquired, all retries for that
provider stage share one provider deadline rather than each receiving a fresh
long timeout.

A council has a bounded total lifetime. Each direct NVIDIA round persists its
stage/state/start/finish timestamps into the durable task record, so operators
can distinguish capacity wait/provider execution/completion instead of seeing
an opaque long-running `initial` stage. Cancelling a council signals the
direct NVIDIA transport, stopping slot wait and suppressing later retries/rounds.
If the MCP process restarts, `read_task` detects a durable council whose
recorded worker identity no longer exists and terminates it as
`council_worker_lost` instead of leaving it `running` forever.

The stable `request_key` is reserved durably before launch, so replaying the
same request returns the same council task rather than starting another inference.


### Directory-enforced OpenCode isolation

OpenCode Zen free-tier calls must retain the official OpenCode session/prompt
shape. For provided-only council runs, DevCoveer therefore enforces the
tenant boundary in the dedicated empty consultation workspace instead of
replacing the session permission payload.

The workspace path is versioned (`model-consult-provided-only-v2`) because the
OpenCode server caches effective configuration by directory. Any future change to
this isolation policy must use a new directory key rather than reusing a cached
path.

The workspace contains a deterministic `opencode.json` with global
`permission: {"*":"deny"}`, explicit denies for file/search/shell/web/
subagent/external-directory capabilities, and a plan-agent deny-all override.
Before creating the provider session, DevCoveer reads effective `/config`
for that workspace and fails closed unless those denies are present.

NVIDIA provided-only council calls bypass OpenCode entirely and therefore have
no agent/tool surface to deny. The direct transport reads only one of the two
trusted provider credentials after acquiring its slot, sends the bounded prompt
to NVIDIA, and releases/clears that slot after the call. The evidence bundle
remains the only caller-authorized semantic input.


## Claude Code as an independent critic

`consult_model` accepts `model=claude_opus_5_5` for the exact Anthropic model
`claude-opus-5-5`. Claude is optional; adding the profile does not change
OpenCode task dispatch, the free council, or native Codex quota rules.

Use `context_mode=provided_only`, a non-empty caller-authorized
`evidence_bundle`, and a stable `request_key`. The normal project-readable mode
is intentionally unavailable for this profile. State the product constraints,
supply the relevant evidence, and ask for material objections, missed failure
cases, and the smallest improvement that addresses them. The calling model
remains responsible for judging the advice and for every subsequent write.

The executable is selected before dispatch. Standard CLI paths have priority.
If they are absent, discovery examines at most 512 entries in the single known
`/home/dev/.vscode-server/extensions` directory, accepts only
`anthropic.claude-code-X.Y.Z-linux-x64/resources/native-binary/claude`, and chooses
the newest executable by numeric version. It does not scan other host directories,
accept a caller-supplied path, create a global symlink, or retry another installed
version after a provider error. The selected entrypoint is pinned for that worker.

The installed CLI runs once in a dedicated empty directory. The prompt and
evidence travel through stdin; they are not command-line arguments or a saved
project file. `--safe-mode` suppresses ordinary user/project customizations, plugins, skills,
memory and CLAUDE.md while preserving the existing subscription authentication.
Administrator-managed policy remains trusted and may still configure hooks,
status-line or file-suggestion commands. This profile does not override or sandbox
those managed commands; provided-only use therefore assumes the host
administrator has approved that policy for the supplied data.
`--restricted` and an empty `--setting-sources` keep user/project settings out;
`--tools ""`, deny-all tools and a strict empty MCP configuration remove the
model's tool surface. The child receives a small fixed environment, not the
bridge's provider keys, proxy overrides or project variables.

Do not substitute `--bare`: current Claude Code documentation says that it
does not use the subscription OAuth login. Do not weaken isolation flags when
an installed CLI rejects them. An unsupported flag, absent binary, unavailable
model, authentication failure, timeout or empty output fails this consultation;
no alternative model or provider is tried. This is a CLI capability boundary,
not a claim of an additional operating-system sandbox around the Claude binary.

The result must identify exactly `claude-opus-5-5` in the provider's
`modelUsage`. Missing identity or a different/additional model is reported as
`model_identity_unverified` and is not accepted as advice. The task receipt and
Markdown artifact distinguish the requested selection from the reported model.

The request key is durably reserved before launch. An identical repeat returns
the same task; changed evidence with the same key is a conflict. A crashed worker
is reported by `read_task` as `worker_lost`, without starting another inference.
A reservation lost before a worker status file exists becomes `dispatch_lost`
after a 30-second dispatch grace; cancellation can interrupt it immediately.
The worker verifies the expected prompt length and SHA-256 before invoking Claude;
EOF or changed input fails as `incomplete_input`, without inference.
`cancel_task` marks a terminal interruption and signals only a worker identified
by both its process command and task ID. Late completion cannot replace a
terminal outcome. `continue_task` refuses to mutate a consultation; use a new
key for another question. Successful and failed consultations use the existing
redacted Markdown artifact path. Provider raw stdout/stderr is bounded and
removed after normal termination, failure, timeout or handled cancellation.

### Release and acceptance

The canonical builder includes `claude_consult.py`; deploy through the existing
`canonical_install` project profile from a clean reviewed source revision.
Clients that cached the old `consult_model.model` enum must refresh/reconnect
the existing MCP app. Do not put the Claude ID behind a Codex or NVIDIA alias
to get around a cached schema.

The unit suite checks context isolation configuration, missing/wrong reported
models, visible output, idempotent dispatch, restart recovery, cancellation and
canonical build integration. Live acceptance still requires the installed CLI
and an authorized account: run one provided-only critique, verify its actual
model and artifact, then replay the same request key and check that the task ID
is unchanged. A source build or fake-provider test alone does not establish
that the account can access Opus 5.5.

Official references checked on 2026-10-18:

- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Programmatic calls and authentication in bare mode](https://code.claude.com/docs/en/headless)
- [Opus 5.5 model identifier](https://platform.claude.com/docs/en/models/opus-5-5/overview)
