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
