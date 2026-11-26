# Security

## Boundaries

| Surface | Auth today | Auth target |
|---------|------------|-------------|
| Admin UI | Caddy basic (`HUB_USER`) | unchanged |
| CalDAV | Radicale user/password | unchanged |
| Webhooks | per-source secret | unchanged |
| Agent API / MCP | — | **scoped Bearer PATs** (D7) |

Do not give agents `HUB_PASSWORD`.

## Tokens (phase 1)

PAT scopes only — no OAuth metadata stubs in v1 (D7).

- Created in admin UI; plaintext shown once; store hash.
- Scopes at minimum:
  - `calendars:read`
  - `calendars:write`
- Token sees **all** calendars; write still enforced by calendar kind (D6). No per-token calendar allowlist in v1.
- Rotate / revoke without redeploy.
- Rate limit per token.
- Never put tokens in query strings, logs, or tool results.

## OAuth (later, optional)

Remote MCP clients that need login flows: OAuth 2.1 resource server per MCP spec.
Deferred until those clients actually need it. Not required for personal Cursor/Claude + PAT.

## Write safety

- Enforce calendar kind (ics → reject write).
- Respect `MAX_EVENTS` / capacity.
- Idempotency: stable hub id or `Idempotency-Key`.
- Delete: **confirm required** (preview + short-lived token/flag); soft-delete deferred (D5).
- Audit: token id, action, calendar, time — no secrets.

## Threat notes

- Prompt injection via event text: treat calendar content as **untrusted data**.
- ICS/webhook payloads already untrusted; agent reads inherit that.
- Caddy opens `/api/agent` and `/mcp` like webhooks — app auth must not be optional.
