# Architecture

## Already true

```
sources / webhooks / ICS ──► hub ──► Radicale (events)
                              │
admin UI / CalDAV clients ────┴──► same collections
```

- **Event truth** = Radicale (CalDAV).
- **Control plane** = SQLite (sources, secrets, mappings, sync state, agent event ids).
- CalDAV is one **connector**, not the whole product.

Do not “move events to CalDAV” — they are already there.

## Target shape

```
                    ┌─ CalDAV (humans, devices)
                    ├─ Admin UI (you)
adapters ───────────┼─ Webhooks (machines)
                    ├─ Agent HTTP API (scripts, automations)
                    └─ MCP (Cursor, Claude, …)
                              │
                         domain layer
                              │
                    Radicale + SQLite
```

Webhook and agent **share domain apply** (same write rules into Radicale). JSON shapes differ: webhook keeps today’s dialect; agents get a native schema (D3).
MCP is a **thin adapter** over the Agent HTTP API (or shared domain calls), in the **same hub process** (D1).
No second event store. No CalDAV-only bypass for hub policy.
caldav-mcp is **out of product path** (D8).

## Placement (VPS)

| Option | Choice |
|--------|--------|
| Agent API | **In hub process** (`/api/agent/...`) |
| MCP | **In hub process** (Streamable HTTP `/mcp`) — D1 |
| Extra container | **Avoid** on 2 GB |

Caddy: agent routes outside basic auth; Bearer checked in app (like webhooks).

## Identifiers

Agents see **hub opaque ids** from a mapping table (D4), stable across list → get → update/delete. CalDAV uid/href stay behind the mapping. Revisit SQLite/RAM cost on the 2 GB host with other sizing tradeoffs later.

## Calendar kinds

Agents see **all** calendars (owner event parity; no per-token allowlist in v1 — D6). Write follows kind:

| Kind | Agent read | Agent write |
|------|------------|-------------|
| private | yes | yes |
| webhook | yes | yes (same apply path as webhook POST) |
| ics | yes | **no** (mirror) |
