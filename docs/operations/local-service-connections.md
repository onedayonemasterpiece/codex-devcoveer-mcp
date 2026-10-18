# Devserver intra-service connections — canonical policy

Status: **Fixed by owner, 2026-10-18**. Scope: services sharing one Linux host on DevCoveer. This is a behavioral contract, not a new gateway, daemon, database, or control plane.

## Default path

1. **Same host**: use the existing service's loopback-only HTTP endpoint (`127.0.0.1`) or an already supported Unix-domain socket. Do not route internal traffic through public DNS, an external HTTPS proxy, MCP OAuth, a browser or an owner login.
2. **Different hosts**: use authenticated TLS (HTTPS/mTLS where already supported), with the same machine-identity and receipt principles.
3. **Human/agent-facing MCP**: OAuth is appropriate only for a user-delegated external client. Never use a person's interactive OAuth refresh token to authenticate a long-running local producer/consumer.
4. **Credentials**: each service has its own long-lived, locally provisioned, revocable and least-privileged technical identity. Store the secret in root/owner-protected runtime configuration. Never share a full-owner token, embed it in code, pass it in URLs, log it, or forward an Android/ChatGPT/user bearer. Existing service auth/binding/epoch checks remain authoritative.
5. **Authorize actual effects**: bind technical identities to allowed operations, destinations and (where applicable) exact topic IDs. Caller-supplied URL, hostname, `Origin`, port or `Host` header never expands rights. Do not relax a service-wide owner gate without adding equivalent target-scoped authorization.
6. **No extra platform**: use the receiving product's existing HTTP API, service principal and durable operation ledger. Shared code/skills can standardize behavior; they must not introduce a new runtime dependency. Do not replace an already working local service protocol unless necessary.

## Reliability contract

- Freeze an idempotency key over `producer + logical object + revision + operation` before the first effect, persist operation state and the receiving system's accepted `operation_id`. Retrying after a timeout uses the **same** key, never an invented second command. On unknown provider outcome observe/reconcile first; never blindly resend.
- Receiver returns a bounded acceptance receipt promptly, then performs slow external work in its existing worker. Producer retains original bytes or recoverable source until provider-level verification and checks the exact destination/topic and immutable SHA-256 for binary media.
- Timeouts are bounded and transient errors back off with jitter; a failed optional downstream dependency leaves durable `pending` state and cannot fail, stall or erase the canonical ingest. Restart/resume must continue the saved operation. No retry storms, hot polling or duplicate schedulers.
- Startup and health: services start independently; missing downstream services are a degraded *integration*, not a reason to restart/lock up the upstream product. Use the existing systemd supervisor and bounded health/observability, not a new watchdog.
- Minimal logs: correlation/operation IDs, status, component, retry/latency and verified hashes; no credentials, private document text or original bytes.
- E2E acceptance must verify a real authenticated local call, an existing bounded target, one effect after lost response/restart, downstream provider readback, and SHA-256 for originals. Green unit tests or HTTP 200 alone never imply delivery.

## Concrete local reference

- **Street Story → VibePublish**: service Bearer identity, fixed local HTTP endpoint, bounded publication/status receipts. Preserve its existing working boundary and granted targets.
- **RKB ↔ Street Story POI**: dedicated service Bearer and bounded loopback endpoints, no reused Android/end-user bearer.
- **RKB → VibePublish archive**: migrate away from the legacy user OAuth grant to a dedicated service principal scoped to RKB's archive chat/topics (originals topic 2, images topic 4). Preserve the existing source SHA, idempotency identity, provenance, retrieval ACL, exact Telegram readback and original PDF verification. Never mark `source_archive_status=verified` until provider bytes match.
- **External ChatGPT/MCP**: retain existing OAuth; no change to the user-facing OAuth server.

## Agent implementation checklist

Before building an interservice integration, read this policy and `devserver-interservice` skill. Inspect the real deployed transport/credentials/permissions of one currently working peer (Street Story is a reference, not universal authority). Reuse the receiver's native API. Document the principal, allowed methods, destinations, local bind address, failure/recovery semantics, audit and acceptance. Refuse a design that creates an extra OAuth/browser step or new gateway merely because one product previously used it.

Never claim product completion on docs or code alone. Check live service state, original operation receipts and real downstream evidence. If a user says they are about to test, explicitly state what is deployed and which end-to-end checks actually passed.