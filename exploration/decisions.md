# Open decisions

Status: **open** until checked. Pick before coding that slice.

## D1 — MCP hosting

- [x] **A** Same process as hub (`/mcp`)
- [ ] **B** Tiny sidecar → Agent HTTP only

**Chosen: A.** Stays on the VPS in the hub process; no separate hosting/container. Revisit only if RAM/latency hurts.

## D2 — Search endpoint shape

- [ ] **A** `GET /events?q=`
- [ ] **B** Separate `GET /events/search`

Prefer **B** if list defaults (time window) clash with open search semantics.

## D3 — Agent create vs webhook JSON

- [ ] **A** One JSON shape shared with webhook upsert
- [ ] **B** Agent-native schema; webhook stays as today; shared domain only

**A** = less surface. **B** = freer UX for agents.

## D4 — Event ids exposed to agents

- [ ] **A** Hub opaque id (mapping table)
- [ ] **B** CalDAV uid / href
- [ ] **C** Both (`id` + `uid`)

Agents need stable ids across list → get → update.

## D5 — Delete semantics

- [ ] **A** Hard delete immediately
- [ ] **B** Confirm flag required
- [ ] **C** Soft-delete / grace

## D6 — Which calendars agents see by default

- [ ] **A** All
- [ ] **B** Allowlist per token
- [ ] **C** All readable; write only private (+ webhook?) 

## D7 — Auth phase 1

- [ ] **A** PAT scopes only
- [ ] **B** PAT now + stub OAuth metadata for later

## D8 — caldav-mcp

- [ ] **A** Ignore for product path
- [ ] **B** Optional personal shortcut (local stdio) while Agent API is built
- [ ] **C** Document as unsupported parallel

Does not replace hub policy/tokens.

## D9 — Free/busy tool

- [ ] **A** v1
- [ ] **B** later
- [ ] **C** never (use list + agent reason)

## D10 — Ship order

Suggested:

1. Domain query (filter/search/get) + caps  
2. Domain write private (+ webhook apply reuse)  
3. Agent HTTP + PAT  
4. MCP wrapper  
5. Admin token UI + audit  

Change only if a decision above forces it.
