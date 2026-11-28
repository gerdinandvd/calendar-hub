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
MCP is a **thin adapter** over the Agent HTTP API (or shared domain calls), in the **same hub process** (D1), using the **official Python SDK** (`mcp` / `MCPServer`) mounted as Streamable HTTP (D10).
No second event store. No CalDAV-only bypass for hub policy.
caldav-mcp is **out of product path** (D8).

## Placement (VPS)

| Option | Choice |
|--------|--------|
| Agent API | **In hub process** (`/api/agent/...`) |
| MCP | **In hub process** (Streamable HTTP `/mcp`) — D1; lib = official `mcp` — D10 |
| Extra container | **Avoid** on 2 GB |

Caddy: agent routes outside basic auth; Bearer checked in app (like webhooks).

## Identifiers

Agents see **hub opaque ids** from `EventMapping` (D4), stable across list → get → update/delete. CalDAV uid/href stay behind the mapping.

- Hub creates (agent / webhook / ICS) **always** mint a mapping.
- CalDAV clients write Radicale directly → no row until agent **list/get lazy-backfills** (D11).
- Revisit SQLite/RAM cost on the 2 GB host with other sizing tradeoffs later.

## Recurrence

Radicale stores series as one object (master + exceptions). Agent **list/search expand** instances into the query window; **get/update/delete** target the series master only in v1 (D13).

## Calendar kinds

Agents see **all enabled** calendars (D6, D14). Disabled sources are hidden. Write follows kind:

| Kind | Agent read | Agent write |
|------|------------|-------------|
| private | yes | yes |
| webhook | yes | yes (same apply path as webhook POST) |
| ics | yes | **no** (mirror) |
