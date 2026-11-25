# Security

## Boundaries

| Surface | Auth today | Auth target |
|---------|------------|-------------|
| Admin UI | Caddy basic (`HUB_USER`) | unchanged |
| CalDAV | Radicale user/password | unchanged |
| Webhooks | per-source secret | unchanged |
| Agent API / MCP | — | **scoped Bearer tokens** |

Do not give agents `HUB_PASSWORD`.

## Tokens (phase 1)

- Created in admin UI; plaintext shown once; store hash.
- Scopes at minimum:
  - `calendars:read`
  - `calendars:write`
- Rotate / revoke without redeploy.
- Rate limit per token.
- Never put tokens in query strings, logs, or tool results.

## OAuth (phase 2, optional)

Remote MCP clients that need login flows: OAuth 2.1 resource server per MCP spec.
Not required for personal Cursor/Claude + PAT.

## Write safety

- Enforce calendar kind (ics → reject write).
- Respect `MAX_EVENTS` / capacity.
- Idempotency: stable id or `Idempotency-Key`.
- Delete: explicit confirm or soft-delete policy (see D5).
- Audit: token id, action, calendar, time — no secrets.

## Threat notes

- Prompt injection via event text: treat calendar content as **untrusted data**.
- ICS/webhook payloads already untrusted; agent reads inherit that.
- Caddy opens `/api/agent` and `/mcp` like webhooks — app auth must not be optional.
