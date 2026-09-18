# DevCoveer2 provider pools and Secure MCP tunnel recovery

Status: recovery contract. Do not infer missing credentials, tunnel IDs, provider definitions,
or gateway services from source names alone.

## OpenCode provider pools

### Groq

Owner credentials:

- `GROQ_API_KEY`
- `GROQ_API_KEY2`
- `GROQ_API_KEY3`

The generated OpenCode provider environment exposes the historical compatibility names:

- `GROQ_API_KEY` + `GROQ_KEY`
- `GROQ_API_KEY2` + `GROQ_KEY2`
- `GROQ_API_KEY3` + `GROQ_KEY3`

The adaptive router expects provider IDs `groq`, `groq2`, `groq3` in that order,
serving GPT-OSS-120B. Credential rendering and routing metadata are necessary but not
sufficient: recovery acceptance must also verify that the live OpenCode config/catalog
actually contains all three providers.

### GigaChat

Canonical owner credentials are exactly:

- `GIGACHAT_API_KEY`
- `GIGACHAT_API_KEY2`
- `GIGACHAT_API_KEY3`

Do not replace this pool with a single `GIGACHAT_CREDENTIALS` owner field.
`GIGACHAT_CREDENTIALS` is only a compatibility name for the first historical gateway.

The pre-crash production fact is narrower than the three-key owner pool:

- only OpenCode provider `giga1` was actually deployed and accepted;
- model `giga1/GigaChat-3-Ultra`;
- local `gpt2giga` gateway on `127.0.0.1:8091`;
- user service `gpt2giga-giga1.service`;
- `giga2` and `giga3` were planned future lanes, not active gateway services.

The owner contract still preserves all three GigaChat credentials so the planned pool can
be completed later. DevCoveer2 recovery must restore and verify `giga1` now, preserve
keys 2/3 without claiming that corresponding gateways exist, and only add `giga2` /
`giga3` after their local gateway services and bounded acceptance are explicitly
implemented.

GigaChat keys are not injected directly into `opencode-devcoveer.service`; they belong to
the local gateway services. OpenCode should receive the gateway provider definitions/base
URLs, not upstream GigaChat secrets.

## Secure MCP tunnels

The owner-confirmed Platform tunnel objects to restore on DevCoveer2 are three distinct
tunnels:

1. `DevCoveer Codex`
2. `DevCoveer browser-bridge`
3. `DevCoveer mailru`

These are distinct tunnel identities even when a runtime API key can authorize more than
one tunnel in the same Platform organization.

### Codex

- user unit: `codex-devserver-tunnel.service`;
- local MCP transport: existing Codex DevCoveer stdio bridge;
- existing tunnel profile/state must be preserved and revalidated.

### my-browser-bridge

Repository deployment evidence records:

- tunnel-client profile: `my-browser-bridge`;
- historical profile directory: `/home/dev/.config/tunnel-client-my-browser-bridge`;
- user unit: `my-browser-bridge-tunnel.service`;
- MCP endpoint: `http://127.0.0.1:7311/mcp`;
- tunnel health/admin listener: `127.0.0.1:8081`.

Recovery should migrate this profile into the canonical tunnel-client auth/state tree
without merging it with the Codex profile.

### Mail MCP

Repository deployment contract records:

- unit: `mail-mcp-bridge-tunnel.service`;
- MCP endpoint: `http://127.0.0.1:7312/mcp`;
- private config: `/etc/mail-mcp-bridge/tunnel-client.yaml`;
- private runtime environment: `/etc/mail-mcp-bridge/tunnel-client.env`.

The tunnel config references a `CONTROL_PLANE_TUNNEL_ID`, a
`CONTROL_PLANE_API_KEY`, and a loopback Mail MCP Authorization header. Real values must
remain outside Git.

## Storage boundary

`/home/dev/.env` is the human owner credential store for external provider/account
credentials. Tunnel IDs are configuration, not owner secrets. Tunnel-client runtime API
keys/profile state belong to the canonical tunnel-client auth/state area or the service's
private runtime EnvironmentFile, not to the generic owner credential file unless an
existing deployment explicitly requires that owner-managed external credential there.

The canonical tunnel-client root may contain multiple named profiles. Recovery must not
interpret "one canonical auth root" as "one tunnel".

## Acceptance

Provider recovery is incomplete until:

- OpenCode catalog exposes `groq`, `groq2`, `groq3`;
- a bounded task can actually use each admitted Groq lane;
- all three GigaChat owner credentials are preserved;
- the live GigaChat gateway/provider topology is read back from host state and each admitted
  `giga*` lane passes a bounded model call;
- the normal resource order is Zen -> Groq pool -> NVIDIA -> GigaChat.

Tunnel recovery is incomplete until:

- all three Platform tunnel identities are mapped to separate local profiles/services;
- each tunnel profile passes `tunnel-client doctor`;
- Codex, browser-bridge and Mail MCP tunnel services are active;
- browser-bridge remains loopback-only on 7311;
- Mail MCP remains loopback-only on 7312;
- an external harmless MCP read succeeds through each tunnel.
