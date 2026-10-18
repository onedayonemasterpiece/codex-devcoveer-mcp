# DevCoveer shared agent instructions

The host-wide `/home/dev/AGENTS.md` applies to all local projects. Before creating
or changing any service-to-service connection, **MUST** read
[`docs/operations/local-service-connections.md`](docs/operations/local-service-connections.md)
and use [`skills/devserver-interservice/SKILL.md`](skills/devserver-interservice/SKILL.md).

- Same-host services reuse a loopback HTTP/Unix socket boundary and dedicated
  least-privilege service identities. No interactive OAuth/public HTTPS hop for
  local background work.
- Preserve strict destination/topic authorization, stable request identity,
  durable receipts, restart recovery, provider proof and SHA-256 validation.
- Do not claim a product ready without live verification. No new central gateway.
- Install the host-managed instructions and skill with
  `python scripts/install-interservice-skill.py --apply`, then verify a
  second dry run reports no changes.

## External and agent handoffs

Before relaying work to dot or another assistant through Slack, read
[`skills/devserver-agent-handoff/SKILL.md`](skills/devserver-agent-handoff/SKILL.md).
Delivery receipts are **not** acknowledgments. After sending, read the exact
reply thread, check that the intended recipient answered, and allow a short
bounded follow-up check when initially unanswered. Report acceptance, current
findings and real verification separately; do not re-send or invent a new
service/method for a functioning Slack handoff. The shared agent installer
runs automatically during canonical DevCoveer installation.
