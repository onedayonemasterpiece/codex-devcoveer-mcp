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

In provided-only mode every participant round uses the isolated consultation
transport: initial answer, cross-review, revision, final expert review, and
bounded transient retries all run from the dedicated empty consultation
directory with a deny-all permission surface and file/search/web/subagent
tools disabled. Project requirements and reuse context are not injected into
those provider prompts.

The council result remains attributed per model, and the existing free,
extended, and pro budget rules are unchanged. Paid tiers still require the
existing separate explicit confirmation flow; isolation does not bypass or
weaken that policy.

The stable `request_key` is reserved durably before council launch. Replaying
the same key and request returns the existing council task rather than
starting another inference. A mismatched payload for the same key is rejected,
and an uncertain/lost dispatch is surfaced as durable readback rather than
blindly repeated.
