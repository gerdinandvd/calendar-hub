# Open decisions

Status: **open** until checked. Pick before coding that slice.

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
- [x] **B** Confirm flag required
- [ ] **C** Soft-delete / grace

**Chosen: B.** Destructive deletes need an explicit confirm step (token/flag), tuned for AX — clear preview of what will be deleted, short-lived token, no mystery second call. Soft-delete deferred unless undo proves necessary.

## D6 — Calendar access for agents

**About:** which hub calendars a Bearer token may touch via Agent HTTP / MCP — not Admin UI, not CalDAV, not webhook secrets.

Two axes (pick a combo; do not conflate):

| Axis | Question |
|------|----------|
| **Visibility (read)** | Which calendars appear in `list_calendars` and can supply events to list/search/get? |
| **Mutation (write)** | On which of those may create / update / delete run? |

Kinds today: `private`, `webhook`, `ics`. Architecture already says **ics is never writable** (mirror); that stays true under every option below.

Also distinct from D7 (token *scopes* like `calendars:read` / `calendars:write`). D6 is *which calendars*; D7 is *whether the token may read/write at all*.

### Options

- [ ] **A** Hub-wide: all kinds readable; write = private + webhook (architecture table as product default; no per-token calendar filter)
- [ ] **B** Hub-wide: all kinds readable; write = private only (webhook stays webhook-POST-only; agents never mutate webhook calendars)
- [ ] **C** Hub-wide: private only (read + write). ICS and webhook calendars invisible to agents
- [ ] **D** Per-token allowlist (explicit calendar ids). New token starts empty or with a chosen default set; admin picks which calendars that token sees. Write still respects kind rules (no ics write)
- [ ] **E** Kind defaults + optional per-token allowlist. Default = A or B; a token may further shrink (never enlarge past hub policy)
- [ ] **F** Separate read allowlist and write allowlist per token (finest control; more admin UI)

**“By default”** means: when the client does not pass `calendars=` on list/search, and when a new token is created without a custom set — which calendars are in scope.

Not decided yet.

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
