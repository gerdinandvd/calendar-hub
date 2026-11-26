# Decisions

Status: **D1–D14 decided.** Build order left open until the idea is further shaped.
Contract field shapes / caps live in [surface.md](surface.md) and follow these decisions.

## D1 — MCP hosting

- [x] **A** Same process as hub (`/mcp`)
- [ ] **B** Tiny sidecar → Agent HTTP only

**Chosen: A.** Stays on the VPS in the hub process; no separate hosting/container. Revisit only if RAM/latency hurts.

## D2 — Search endpoint shape

- [ ] **A** `GET /events?q=`
- [ ] **B** Separate `GET /events/search`
- [x] **C** Two MCP intents (`list_events` / `search_events`); one HTTP `GET /events` (+ optional `q=`); distinct tool defaults; responses echo `window` / `q` / `truncated` / `applied_defaults`

**Chosen: C.** Predictability lives in tool contracts + response echo, not in a second URL. `q=` on list = filter inside the time window; open search is the other tool (require `q`, optional/explicit window — no silent week default). Path split only if that later proves clearer.

## D3 — Agent create vs webhook JSON

- [ ] **A** One JSON shape shared with webhook upsert
- [x] **B** Agent-native schema; webhook stays as today; shared domain only

**Chosen: B.** Better agent experience (no webhook `action` / external-id dialect). Shared domain apply only; two JSON shapes is fine.

## D4 — Event ids exposed to agents

- [x] **A** Hub opaque id (mapping table)
- [ ] **B** CalDAV uid / href
- [ ] **C** Both (`id` + `uid`)

**Chosen: A (for now).** Stable hub ids for list → get → update; CalDAV stays behind the mapping. Revisit RAM/SQLite cost on the 2 GB VPS only at the end with the other sizing tradeoffs.

## D5 — Delete semantics

- [ ] **A** Hard delete immediately
- [x] **B** Confirm required (preview + short-lived token)
- [ ] **C** Soft-delete / grace

**Chosen: B.** One delete tool/route, two steps: first call returns preview + `confirm_token` (~120s, single-use, bound to id); second call with token commits. No bare `confirm=true`. Soft-delete deferred unless undo proves necessary.

## D6 — Which calendars agents see by default

- [x] **A** All (enabled)
- [ ] **B** Allowlist per token
- [ ] **C** All readable; write only private (+ webhook?)

**Chosen: A.** Owner-parity on events: agents see every **enabled** calendar the hub has (D14); write where the kind allows (private + webhook; ics stays read-only mirror). No per-token calendar allowlist in v1. Later we may add more rights (e.g. create calendars / sources) — that is control-plane, not this default.

## D7 — Auth phase 1

- [x] **A** PAT scopes only
- [ ] **B** PAT now + stub OAuth metadata for later

**Chosen: A.** Scoped Bearer PATs are enough for phase 1 (Cursor/Claude + scripts). OAuth metadata deferred until remote login clients actually need it.

## D8 — caldav-mcp

- [x] **A** Ignore for product path
- [ ] **B** Optional personal shortcut (local stdio) while Agent API is built
- [ ] **C** Document as unsupported parallel

**Chosen: A.** Product path is hub Agent HTTP + MCP only. caldav-mcp stays out of scope (useful CalDAV client elsewhere; not hub policy/tokens).

## D9 — Free/busy tool

- [ ] **A** v1
- [x] **B** later
- [ ] **C** never (use list + agent reason)

**Chosen: B.** Ship list/search/get/write + MCP first; add `find_free_slots` / free-busy if agents keep misusing list for scheduling gaps.

## D10 — MCP library

- [x] **A** Official Python SDK (`mcp`, v2 `MCPServer`)
- [ ] **B** FastMCP (`fastmcp`)
- [ ] **C** FastAPI-MCP (`fastapi-mcp` auto-bridge)

**Chosen: A.** Mount Streamable HTTP in-process via `mcp.streamable_http_app()` (D1). Thin adapter over Agent HTTP with hand-written tools (D2 intents). FastMCP deferred — useful later for IdP/OAuth helpers, not required for PAT phase (D7) or resource-server OAuth later. FastAPI-MCP rejected: auto-exposing routes fights distinct MCP tool contracts.

## D11 — Mapping coverage (CalDAV bypass)

- [x] **A** Hub writes always map + lazy backfill on list/get for unmapped Radicale objects
- [ ] **B** Only events that already have a mapping (agent/webhook/ICS)
- [ ] **C** Eager full backfill job

**Chosen: A.** Every create through the hub mints an `EventMapping`. CalDAV clients (phone, Thunderbird, …) write Radicale directly, so list/get **lazy-fills** missing rows (stable hub id; CalDAV uid stays internal). Needed for D4 + D6 owner parity. Not caldav-mcp-specific — any CalDAV bypass.

## D12 — Search without time window

- [ ] **A** Refuse until `from`/`to` are set
- [x] **B** Allow; hard result cap + pagination; no silent week default
- [ ] **C** Allow with a silent default window (echoed)

**Chosen: B.** `search_events` requires `q`; window optional. No list-style week default. Cap + opaque cursor keep scans bounded on the 2 GB host.

## D13 — Recurring events in list/search

- [ ] **A** Expand occurrences in list/search; occurrence-scoped writes in v1
- [ ] **B** Masters only (no expansion)
- [x] **C** Expand in list/search; writes (get/update/delete) target the **series master** only in v1

**Chosen: C.** Agents see concrete instances in a window (“what’s this week?”). Mutations use the hub id of the series; occurrence edit/`scope` later. May revisit toward masters-only (B) if expansion cost or AX noise hurts.

## D14 — Disabled sources

- [x] **A** Hide from agents (`enabled=0` invisible)
- [ ] **B** Show read-only
- [ ] **C** Show with same write rules as enabled

**Chosen: A.** Agents only see enabled calendars. Turning a source off removes it from agent list/read/write until re-enabled.
