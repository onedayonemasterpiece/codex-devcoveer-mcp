---
name: devserver-interservice
description: "Unified reliable DevCoveer intra-server service-to-service integrations. Trigger for any new/changed connection between local apps such as RKB, Street Story, VibePublish or other products; review local transport, least-privilege service principals, idempotent receipts, recovery and actual E2E verification."
---

# Devserver: reliable interservice integration

Canonical policy: `/home/dev/projects/codex-devcoveer-mcp/docs/operations/local-service-connections.md` (same document in onedayonemasterpiece/codex-devcoveer-mcp). Read that first, then the receiving service's current API and the caller's actual runtime configuration.

1. Classify **same Linux host / separate host / external user**. For same-host use loopback HTTP `127.0.0.1` or an existing Unix socket; do **not** add public DNS, TLS reverse proxy, OAuth/browser, tunnel or new gateway to the critical path. External networks use authenticated TLS.
2. Check an existing working peer implementation (e.g. Street Story → VibePublish) to reuse route structure and error semantics. Do not copy overbroad owner privileges.
3. Provision a **distinct least-privileged technical principal** via the existing receiver's admin interface, with immutable destination/topic/operation scope and durable revoke capability. Secret stays in protected server config and is not printed/read into reports. No user bearer forwarding.
4. Require stable idempotency keys, receipts, durable pending/outbox, bounded timeouts/backoff, exact replay and provider verification. Treat unknown provider effects as unknown; observe rather than resend. Keep source bytes until verified readback (including SHA-256).
5. Test local authenticated request, permission denial on outside topics and actions, worker/restart and lost response, exact same-key replay, provider receipt and retrieval of the original bytes. Only claim ready when **live** evidence exists. A green code-only CI is not archive acceptance.
6. Preserve unrelated services, session bindings, current runtime releases and dirty checkouts. Agent source review is not authorization to change credentials or grant rights outside the approved service scope.

Record per integration: caller, receiver, transport, principal, methods, target scope, key/receipt lifecycle, deferred-state behavior, pre/post-deployment tests. Minimum change wins; no new central service dependency.

If a user says they are going to test, report precisely: **deployed/not deployed**, **real E2E verified/not verified**, and a link to the actual artifact/evidence. Never imply implementation from a plan.