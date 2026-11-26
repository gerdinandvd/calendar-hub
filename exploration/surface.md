# Agent surface

Backend filters. Compact by default. No “list everything”.

## HTTP (canonical)

Stable JSON for any client. MCP wraps this with **two intents** for listing vs search (D2).

Suggested prefix: `/api/agent/v1/`

| Method | Path | Role |
|--------|------|------|
| GET | `/calendars` | list |
| GET | `/events` | window + filters + optional `q=` + cursor |
| GET | `/events/{id}` | detail |
| POST | `/events` | create (writable calendar; agent-native JSON) |
| PATCH | `/events/{id}` | update (agent-native JSON) |
| DELETE | `/events/{id}` | delete (confirm required) |

`{id}` is the **hub opaque id** (D4), not a raw CalDAV uid/href.

Query ideas for `GET /events`:

- `from`, `to` (default: now → +7d) — list contract
- `calendars=`
- `q=` — text filter **inside** the time window when used with list
- `limit`, `cursor`
- `fields=compact|standard|full` (default `compact`)

Responses should echo `window`, `q`, `truncated`, `applied_defaults` so agents see what they got (D2).

`compact`: id, calendar, summary, start, end, all_day, status.

Hard caps: max window, max limit, max response bytes.

Create/update bodies are **agent-native** (no webhook `action` / external-id dialect). Domain apply is shared with webhooks; JSON is not (D3).

## MCP tools (~7–9)

| Tool | Maps to | Notes |
|------|---------|--------|
| `list_calendars` | GET calendars | All calendars (D6) |
| `list_events` | GET events | Time-window defaults; optional in-window `q=` |
| `search_events` | GET events with search semantics | Required `q`; no silent week default (D2) |
| `get_event` | GET events/{id} | Hub opaque id |
| `create_event` | POST events | Agent-native schema |
| `update_event` | PATCH | Agent-native schema |
| `delete_event` | DELETE | Confirm step (preview + short-lived token/flag) — D5 |

Later (not v1): `find_free_slots` / free-busy (D9).

Not a tool: export-all, raw ICS dump, 30 REST clones, caldav-mcp (D8).

## Webhooks

Keep. Machine write without MCP.
Share **domain apply** with agent create/delete.
Agent HTTP uses its **own** JSON shape; webhook dialect stays as today (D3).
