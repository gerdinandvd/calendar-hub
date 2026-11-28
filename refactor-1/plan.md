# Refactor plan

Step-by-step approach for moving calendar-hub to [target.md](target.md), guided by [test-pyramid.md](test-pyramid.md).

Context: the project is not in active use. Prefer clean cuts, fresh schema, and deleting dropped surfaces over compatibility shims. Keep the existing suite green between slices; rebuild the pyramid at the end.

## Phase 0 — Spec locked

- [x] Branch `refactor/calendar-hub-architecture`
- [x] Write [target.md](target.md) (scope, hexagonal layout, stateless agent IDs)
- [x] Write [test-pyramid.md](test-pyramid.md)
- [x] Write this plan

## Phase 1 — Test harness only

Do not redesign the pyramid yet. Make the current suite editable during the move.

- [x] Unify one `FakeRadicale` / `FakeCalendarStore` in `tests/conftest.py`
- [x] Add shared `hub_client` and `db_engine` fixtures; stop per-file `_open` / `_close` copies
- [x] Stop `test_faults` / `test_performance` importing private helpers from other test modules
- [x] Delete `tests/test_harness.py` and unused conftest-only smoke
- [x] Monkeypatch `MAX_EVENTS` in capacity tests instead of seeding ~20k fake events
- [x] Confirm default `pytest` still green

## Phase 2 — Domain, Settings, ports skeleton

- [x] Add `app/domain/` (types, single `rrules`, pure iCal/time helpers, domain errors without HTTP/i18n)
- [x] Add `app/ports/` (`CalendarStore`, repos, `FeedFetcher`, `SyncCoordinator`, `Clock`, `AuditLogger`, …)
- [x] Add frozen `app/config.py` `Settings`; composition root loads env once
- [x] Update `[tool.importlinter]` contracts to the target layering
- [x] Move or re-export existing pure helpers behind domain without changing behaviour yet
- [x] Keep suite green

## Phase 3 — Adapters foundation

- [ ] `adapters/sqlite`: fresh schema (no webhook/Google columns); `PRAGMA foreign_keys=ON`; CASCADE FKs; migrations module owns DDL
- [ ] `adapters/radicale`: implement `CalendarStore` with pooling, `REPORT` calendar-query, multiget
- [ ] `adapters/http_feed`: bounded fetch + SSRF IP policy + redirect/port Authorization rules
- [ ] `adapters/crypto`: AES-GCM only for ICS Authorization
- [ ] Injectable `SyncCoordinator` replacing `sync/runner` module globals
- [ ] Wire adapters in `main` lifespan; suite green

## Phase 4 — Vertical slice: private sources + Admin UI

- [ ] Application use-cases for private source create/update/delete/list/enable
- [ ] HTMX UI routes call application only (no direct db/radicale imports)
- [ ] Remove dependence on owner JSON API for these flows (or stop using those routers in UI)
- [ ] CalDAV collection path conventions and rights unchanged for private
- [ ] Suite green for private lifecycle tests

## Phase 5 — Vertical slice: ICS sync

- [ ] Pure `plan_snapshot` / feed meta decisions in domain; apply via ports
- [ ] ICS import, scheduler tick, manual sync, fail-safe, capacity, shutdown
- [ ] `EventMapping` used **only** for ICS external id reconciliation
- [ ] Deterministic or pre-persisted `ical_uid` so PUT-then-remember cannot duplicate
- [ ] Parallel write path respects stop/cancel; batch mapping writes
- [ ] Suite green for ICS tests that still apply

## Phase 6 — Vertical slice: MCP + tokens

- [ ] Stateless opaque event IDs `(source_id, ical_uid)` encode/verify
- [ ] MCP list/search via CalDAV `REPORT` (no SQLite writes on read)
- [ ] MCP create/update/delete on `private` only; shared `SyncCoordinator` lock
- [ ] Confirm-delete store scoped to token id; TTL/replay rules
- [ ] Tokens UI: hash-only storage, one-time plaintext + `Cache-Control: no-store`, revoke
- [ ] Failed-auth and per-token rate limits; body size before heavy parse
- [ ] Remove ContextVar token leak (explicit actor passed into use-cases)
- [ ] Suite green for MCP/token behaviours

## Phase 7 — Delete dropped surfaces and dead code

- [ ] Delete webhook kind, `/api/webhooks`, webhook sync/apply/secret rotation, related UI
- [ ] Delete Agent HTTP API (`/api/agent/v1/*`) and its router/deps-only paths
- [ ] Delete owner/admin JSON API routers that UI no longer needs (`/api/sources`, `/api/sync`, `/api/calendars`, `/api/caldav-info`, …)
- [ ] Delete tests that only covered dropped surfaces
- [ ] Delete leftover `services/` transaction-script modules replaced by application
- [ ] Grep-clean: no `webhook` kind, no agent REST mounts, no Google OAuth fields
- [ ] Suite green (smaller, focused on kept product)

## Phase 8 — Rebuild test pyramid

Per [test-pyramid.md](test-pyramid.md):

- [ ] Grow layer-1 domain tests (secrets, expand edge cases, snapshot plan, opaque ids, SSRF helpers)
- [ ] Add layer-2 adapter/application tests (REPORT, coordinator, FK cascade, token repo)
- [ ] Shrink layer-3 to thin HTMX/MCP contracts; strip brittle HTML/JS/prose assertions
- [ ] Trim Docker E2E; keep ACL, internal-account bypass variants, restore
- [ ] Coverage floor still meaningful on `app/` for the default suite
- [ ] Default `pytest` green and faster than pre-refactor baseline

## Phase 9 — Deploy hardening + E2E

- [ ] Caddy: internal Radicale account allowlist / case-insensitive Basic handling
- [ ] Security headers (frame denial, nosniff, referrer; CSP as inline handlers allow)
- [ ] Per-service env scoping in Compose (no full `.env` in every container)
- [ ] Mutation guard Origin / Sec-Fetch-Site if not already done
- [ ] Run `integration` / `slow` Docker checks; fix gaps
- [ ] Update README and design docs to match target (drop webhook/agent REST; document MCP-only agents)

## Exit criteria

- [ ] Import-linter enforces domain/ports/application/adapters
- [ ] No webhook or `/api/agent` surface
- [ ] No owner JSON API required for normal operation
- [ ] MCP reads do not write SQLite; Radicale is sole event store
- [ ] ICS + private + tokens + backups work under Docker E2E
- [ ] Test mix matches the pyramid intent; critical invariants listed in test-pyramid.md are covered
