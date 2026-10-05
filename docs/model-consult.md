# Model consultant

DevCoveer exposes `consult_model` for an optional independent second opinion.

The consultant is deliberately narrower than `council_run`:

- one model per consultation;
- NVIDIA only;
- aliases: `kimi_k3` and `deepseek`;
- read-only OpenCode session;
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
