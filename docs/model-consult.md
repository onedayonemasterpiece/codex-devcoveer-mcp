# Model consultant

DevCoveer exposes `consult_model` for an optional independent second opinion.

The consultant is deliberately narrower than `council_run`:

- one model per consultation;
- NVIDIA only;
- aliases: `kimi_k3` and `deepseek`;
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
stable `request_key`. This is distinct from the existing trusted
project-readable council.

In provided-only mode OpenCode participants use the dedicated empty consultation
directory with a deny-all permission surface and file/search/web/subagent tools
disabled. NVIDIA participants do not run through an OpenCode agent at all:
DevCoveer sends only the caller-authorized council prompt/evidence to NVIDIA's
OpenAI-compatible chat endpoint. Project requirements, repository files, reuse
context and tool access are not injected into those direct provider calls.

NVIDIA is an admission/capacity resource, not an end-user budget confirmation
surface. DevCoveer requires two distinct configured NVIDIA credentials and
maps them to two cross-process slot locks; at most two NVIDIA inference calls
run concurrently, and a saturated additional call waits for bounded capacity.
The product pro preset is Kimi K3 + DeepSeek V4.1 Flash. No confirmation token
or user budget approval is required. The free tier remains OpenCode-only and
never silently falls back to NVIDIA.

The stable `request_key` is reserved durably before council launch. Replaying
the same key and request returns the existing council task rather than
starting another inference. A mismatched payload for the same key is rejected,
and an uncertain/lost dispatch is surfaced as durable readback rather than
blindly repeated.


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
