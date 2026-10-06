# Additional OAuth MCP entrypoint

Owner-approved scope: expose the full existing DevCoveer MCP tool catalogue to
Grok and other compatible MCP clients. Keep the existing OpenAI stdio tunnel,
credentials, service, connections and runtime unchanged.

## Connection

- Name: `Codex DevCoveer`
- MCP URL: `https://identity.kenigevents.ru/devcoveer/mcp/`
- Issuer: `https://identity.kenigevents.ru/devcoveer`
- Owner consent key: `/home/dev/.config/devcoveer-public-mcp/owner-key` (0600).

In Grok, open Connectors → New Connector → Custom, enter the URL and complete
the OAuth popup. Enter the owner key only on the DevCoveer consent page. No xAI
API key, Telegram credential or OpenAI tunnel key is needed for this flow.
Do not put the owner key in URLs. It is a consent credential, not an MCP bearer.
The client receives its own short-lived access token and rotating refresh token.

The HTTP service loads `build_server` from the canonical installed bridge.
It does not maintain a second tool list, filter tools or introduce project scope
restrictions. Reading `street-story` uses the same project probe as ChatGPT;
write actions, task launch/continuation and host operations retain their existing
DevCoveer contracts. Task state is shared through the existing process-locked
registry. Tool-specific requirements and protected-file checks still apply.

## Implementation

`public_mcp_server.py` adds an independent loopback listener on `127.0.0.1:7320`.
The existing nginx TLS edge forwards only the new `/devcoveer/` prefix and exact
RFC 8414/9728 discovery paths on `identity.kenigevents.ru`. Existing domain routes
are retained byte for byte. nginx strips the public prefix on upstream requests;
the OAuth metadata, resource binding and consent form keep the public URLs.

`public_mcp_oauth.py` adapts the established VibePublish OAuth boundary, including
its correction for generic MCP client callbacks. It uses the MCP SDK's DCR,
PKCE verification and client authentication, binds callbacks to registered
clients, and requires owner consent protected by CSRF and a Secure/HttpOnly
cookie. Public and SDK-supported secret-authenticated clients are accepted.
Older clients that omit scope/resource are bound to this one resource;
explicitly mismatched resource/scope values are rejected.

The private SQLite ledger stores hashed grant credentials, expiry, rotation and
revocation state. SDK-issued confidential-client secrets, if any, are stored in
that private ledger for SDK client authentication. It does not contain the owner
key or upstream provider credentials. Rotating the owner key revokes existing
grants on their next use. The consent key never grants bearer access directly.

The transport uses the installed MCP SDK's Streamable HTTP manager in stateless
mode and JSON responses. Both `/mcp` and `/mcp/` are accepted. Authentication is
checked on every request. Safe structured events cover client registration,
consent, grant issuance, rejection and HTTP outcomes; credentials and arguments
are not logged. Public discovery/auth endpoints have bounded bodies and rates.
Host/Origin checks preserve the HTTPS origin behind the fixed reverse proxy.

## Deployment and rollback

Deploy only a clean, tested commit in `origin/main`, using
`scripts/install-public-mcp.py --evidence <managed-artifact-directory>`.
The installer creates an immutable source release under
`/home/dev/.local/libexec/devcoveer-public-mcp/releases/<sha>` and a separate user
unit `devcoveer-public-mcp.service`. It creates the owner key only if absent.

The Python 3.12 service environment is
`/home/dev/.local/share/devcoveer/public-mcp/venv`. The first installation uses a
read-only `.pth` reference to the existing bridge's installed MCP/ASGI
dependencies, plus its own HTTP/test dependencies, because server disk capacity
was low. It never installs into or changes the bridge virtualenv. Version pins
are in `requirements-public-mcp.txt`; a future complete isolated environment can
be provisioned from that file without changing service behavior.

The installer checks backend health before opening ingress, preserves the bound
nginx file inode, runs `nginx -t` and gracefully reloads. On failure it restores
the previous configuration and disables the new unit. It never restarts the
OpenAI tunnel. Evidence includes the previous/candidate nginx files and source,
configuration hashes and service receipt. No credential values go in evidence.

To roll back after acceptance: disable only `devcoveer-public-mcp.service`, remove
the exact additive nginx locations (preserving any later unrelated changes),
validate nginx and reload gracefully. Do not replace a newer nginx configuration
wholesale. Keep the key/ledger private for deliberate revocation or resumption.

## Validation

Offline tests cover DCR, Grok-shaped and generic callbacks, full read/write tool
visibility and calls, confidential clients, resource defaults/binding, anonymous
denial, owner-key rejection as bearer, grant reuse, refresh-family revocation,
owner rotation, hostile Host/Origin, request bounds and additive ingress changes.
Live acceptance must compare the complete HTTP tool catalogue against stdio,
read a `street-story` source file over HTTPS, exercise refresh/revocation, and
confirm the original tunnel PID/start time and harmless MCP reads are unchanged.

Protocol acceptance is separate from the user's Grok account connection: the
user must finish its OAuth popup. Do not report the real Grok UI as tested until
the client has actually registered, granted access and called MCP.
