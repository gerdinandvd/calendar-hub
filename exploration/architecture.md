# Architecture

## Already true

```
sources / webhooks / ICS ──► hub ──► Radicale (events)
                              │
admin UI / CalDAV clients ────┴──► same collections
```

- **Event truth** = Radicale (CalDAV).
- **Control plane** = SQLite (sources, secrets, mappings, sync state).
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

Same writes/rules for webhook and agent create.
MCP is a **thin adapter** over the Agent HTTP API (or shared domain calls).
No second event store. No CalDAV-only bypass for hub policy.

## Placement (VPS)

| Option | Choice |
|--------|--------|
| Agent API | **In hub process** (`/api/agent/...`) |
| MCP | **In hub process** (Streamable HTTP `/mcp`) unless proven too heavy |
| Extra container | **Avoid** on 2 GB |

Caddy: agent routes outside basic auth; Bearer checked in app (like webhooks).

## Calendar kinds

| Kind | Agent read | Agent write |
|------|------------|-------------|
| private | yes | yes |
| webhook | yes | yes (same apply path as webhook POST) |
| ics | yes | **no** (mirror) |
