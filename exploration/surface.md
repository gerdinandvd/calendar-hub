# Agent surface

Backend filters. Compact by default. No “list everything”.

## HTTP (canonical)

Stable JSON for any client. MCP wraps this.

Suggested prefix: `/api/agent/v1/`

| Method | Path | Role |
|--------|------|------|
| GET | `/calendars` | list |
| GET | `/events` | window + filters + cursor |
| GET | `/events/{id}` | detail |
| POST | `/events` | create (writable calendar) |
| PATCH | `/events/{id}` | update |
| DELETE | `/events/{id}` | delete |

Query ideas for `GET /events`:

- `from`, `to` (default: now → +7d)
- `calendars=`
- `q=` (search; or separate `/events/search`)
- `limit`, `cursor`
- `fields=compact|standard|full` (default `compact`)

`compact`: id, calendar, summary, start, end, all_day, status.

Hard caps: max window, max limit, max response bytes.

## MCP tools (~7–10)

| Tool | Maps to |
|------|---------|
| `list_calendars` | GET calendars |
| `list_events` | GET events (time window) |
| `search_events` | GET events?q= / search |
| `get_event` | GET events/{id} |
| `create_event` | POST events |
| `update_event` | PATCH |
| `delete_event` | DELETE |

Optional later: `find_free_slots`.

Not a tool: export-all, raw ICS dump, 30 REST clones.

## Webhooks

Keep. Machine write without MCP.
Share domain apply with `create_event` / delete.
Do not invent a second payload dialect for agents unless HTTP reuses the webhook shape on purpose (see D3).
