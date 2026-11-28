# Agent surface

Backend filters. Compact by default. No “list everything”.

## HTTP (canonical)

Stable JSON for any client. MCP wraps this with **two intents** for listing vs search (D2).

Prefix: `/api/agent/v1/`

| Method | Path | Role |
|--------|------|------|
| GET | `/calendars` | enabled calendars only (D14) |
| GET | `/events` | window + filters + optional `q=` + cursor |
| GET | `/events/{id}` | series/master detail (hub opaque id) |
| POST | `/events` | create (writable calendar; agent-native JSON) |
| PATCH | `/events/{id}` | update series/master (agent-native JSON) |
| DELETE | `/events/{id}` | delete series (preview + `confirm_token`) |

`{id}` is the **hub opaque id** (D4), not a raw CalDAV uid/href. Unmapped Radicale objects get a mapping on first list/get (D11).

### `GET /events` — list vs search (same URL, different tool defaults)

| | `list_events` | `search_events` |
|--|---------------|-----------------|
| `q` | optional (filter **inside** window) | **required** |
| `from` / `to` | default **now → +7d** | optional; **no** silent week default (D12) |
| Without window | n/a (default applied) | allowed; hard result cap + pagination |

Other query params: `calendars=` (source UUIDs), `limit`, `cursor`, `fields=compact|standard|full` (default `compact`).

Responses echo `window`, `q`, `truncated`, `applied_defaults`, `pagination` (D2).

### Caps (v1 defaults)

| Knob | Value |
|------|-------|
| List default window | now → +7d |
| Max window (when set) | 366 days |
| Default `limit` | 50 |
| Max `limit` | 200 |
| Max response bytes | ~1 MiB |
| Confirm token TTL | ~120s, single-use |
| Create body max | 256 KiB (same as webhook) |

### Field levels

- `compact`: `id`, `calendar` (source id), `summary`, `start`, `end`, `all_day`, `status` (+ occurrence markers when expanded)
- `standard`: + truncated `description`, `location`, `timezone`
- `full`: remaining stored agent fields; **no** raw ICS dump

### Recurrence (D13)

List/search **expand** RRULE instances into the window (and into search hits).  
`get` / `update` / `delete` address the **series master** only in v1. Occurrence-scoped writes later.

### Create / patch (agent-native, D3)

**Create required:** `calendar` (source UUID), `summary`, `start`, `end`  
**Optional:** `all_day`, `timezone` (default = calendar TZ), `description`, `location`, `rrule`, `status`  
No attendees in v1. No webhook `action` / `external_id`.

**Patch:** partial; omit = keep; `null` = clear nullable scalars. No calendar-move in v1.

Times: ISO local + IANA `timezone`; all-day = date-only. Errors reject relative phrases (“tomorrow”).

### Delete (D5)

1. `DELETE` without valid token → no mutation; return compact preview + `confirm_token` + `expires_at`
2. `DELETE` with `confirm_token` → hard-delete series in Radicale; mapping tombstone as today

### Errors

JSON `{ "error": "<code>", "detail": "<message>" }` (existing hub shape). Stable codes for confirm/window/limit/query as needed.

## MCP tools (~7)

| Tool | Maps to | Notes |
|------|---------|--------|
| `list_calendars` | GET calendars | Enabled only (D14) |
| `list_events` | GET events | Week default; optional in-window `q=` |
| `search_events` | GET events | Required `q`; no silent week; cap + cursor (D12) |
| `get_event` | GET events/{id} | Series/master |
| `create_event` | POST events | Agent-native schema |
| `update_event` | PATCH | Series/master |
| `delete_event` | DELETE | Preview + `confirm_token` (D5); `destructiveHint` |

Later (not v1): `find_free_slots` / free-busy (D9); occurrence `scope` writes.

Not a tool: export-all, raw ICS dump, 30 REST clones, caldav-mcp (D8).

## Webhooks

Keep. Machine write without MCP.
Share **domain apply** with agent create/delete.
Agent HTTP uses its **own** JSON shape; webhook dialect stays as today (D3).
