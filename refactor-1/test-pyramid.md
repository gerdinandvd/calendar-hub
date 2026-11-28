# Preferred minimal test pyramid

Strategy for tests after (and while) the architecture in [target.md](target.md) lands. This is a design document, not new test code.

Related: [target.md](target.md), [plan.md](plan.md).

## Principles

- Test **behaviour and business rules**, not implementation details (route tables, exact marketing copy, verbatim inline JS, constant echoes).
- Prioritise **security, data integrity, and critical invariants**.
- Tests stay **deterministic, independent, and maintainable**.
- Minimise duplication, unnecessary mocks, and redundant full-stack coverage of the same path.
- During the refactor: **keep existing external behavioural tests green** as a safety net; **rebuild the pyramid at the end** once the domain is pure and dropped surfaces are gone.

## Target mix

| Layer | Share | Speed | What runs |
| --- | ---: | --- | --- |
| 1. Pure domain / static | 55–60% | milliseconds | No FastAPI, no sockets; optional in-memory structures only |
| 2. Adapter / application | ~20% | fast | `db_engine` + `FakeCalendarStore` / real `RadicaleClient` against a tiny local HTTP server; no full UI stack |
| 3. Thin HTTP / ASGI | ~20% | medium | Shared `hub_client` TestClient: HTMX owner flows, MCP bearer/tools, mutation guard, health |
| 4. Docker E2E | 5–8% | slow | Compose smoke, CalDAV ACL, backup/restore; marked `integration` / `slow` |

Default `pytest` stays `not integration and not slow`.

## What each layer must prove

### Layer 1 — Pure domain

Prove rules that should never need SQLite or Radicale:

- iCalendar round-trip, folding, TZ projection, EXDATE / RECURRENCE-ID, VALARM isolation
- Single `rrules` field: present, patch replace, patch clear
- Snapshot plan: upsert / skip / delete-absent / capacity cutoff
- Feed URL validation, redirect policy, cross-host/**port** Authorization strip helpers
- Opaque event-id encode/verify (tamper → reject)
- Window / cursor / pagination / response byte budget (pure assembly)
- Occurrence expansion: EXDATE, overrides, DST, FREQ allowlist, iteration / SECONDLY bounds
- Source validation (name, poll, ICS header CRLF/size), kind policy (writable vs ics path)
- Secrets helpers: encrypt/decrypt, wrong key, tampered ciphertext, constant-time compare
- Rate limiter: isolation across keys, failed-auth bucket behaviour, bounded key growth
- Content/architecture invariants (e.g. VEVENT codec location; import-linter is CI, not pytest)

### Layer 2 — Adapters and application

Prove wiring of ports without HTMX:

- `CalendarStore` / Radicale: MKCALENDAR, PROPPATCH rollback, ETag 412 retry, REPORT time-range, multiget, pooling smoke
- ICS fetch adapter: SSRF IP denial, streaming size without Content-Length, timeout
- Sync coordinator: per-source exclusion, stop, shared lock with MCP write path
- Mapping remember after PUT: no duplicate on retry; FK cascade on source delete
- Token repo: store hash only; revoke blocks auth
- Application services return domain errors (not HTTP codes)

### Layer 3 — Thin HTTP / ASGI

One short contract per surface; assert status, security headers where relevant, and invariant-bearing fragments — not full page prose:

- Mutation guard (HX-Request / JSON content-type; MCP exempt)
- Source CRUD via UI forms (create private, create ICS, confirm delete, enable toggle)
- Manual sync start / busy / unsupported for private
- Tokens: create shows plaintext once + `no-store`; reload hides; revoke blocks MCP
- MCP: missing/invalid bearer; read vs write scope on create/update/delete; body size 413; rate limit 429 on failures and on token use
- Health panel/API smoke (fresh/stale signals without brittle disk regexes)
- No stacktrace / secret leakage on 500

### Layer 4 — Docker E2E

Keep a small set:

- Cold start, TLS, owner Basic Auth, block internal Radicale account (including `basic` / whitespace variants)
- Private CalDAV round-trip; ICS collection read-only for owner
- Restart resilience as needed
- Litestream + Kopia restore drill (include that a pre-restore PAT still authenticates if cheap)

Drop or shorten pathological waits (e.g. multi-minute ETag sleep loops) unless they measure a named SLO with configurable env.

## Shared harness (intent)

Centralise in `tests/conftest.py` (implementation scheduled in [plan.md](plan.md)):

- `FakeCalendarStore` / unified `FakeRadicale` — one fake supporting the behaviours today’s four copies split
- `db_engine` — fresh schema for adapter/application tests
- `hub_client` — TestClient with Settings, paused scheduler, bound MCP, env stubs
- No test module importing private `_open` / `_create` helpers from another test module
- Capacity tests `monkeypatch` `MAX_EVENTS` to a tiny number instead of inserting ~20k dict entries

Delete the dead `test_harness.py` pattern that only validates unused fixtures.

## Invariants to prioritise (kept product)

1. ICS fail-safe: failed fetch/parse does not wipe the collection; partial write keeps cursor and saved events
2. ICS capacity and idempotency; messy UID sanitisation; `X-WR-*` metadata rules (length caps, no silent endless rename fights)
3. ICS SSRF and credential leak on redirects
4. MCP authz: scopes on all mutating tools; disabled calendars hidden/rejected; ICS not writable
5. PAT lifecycle: hash-only storage, one-time reveal, revoke, failed-auth throttling
6. Sync vs MCP concurrency: same source lock; delete source does not race orphan mappings (FKs)
7. Recurrence integrity through MCP read/update/clear
8. Backup/restore: data and auth material recoverable without secrets in backup payloads
9. Radicale rights: `ics-*` read-only for owner; internal account not usable via public edge

## Explicit non-goals

- Rewriting or “perfecting” the pyramid **before** domain/ports exist
- Asserting marketing/help sentences, exact inline JS strings, or Dependabot-pinned image tags in unit tests
- Mirroring `main.py` mount lists as tests
- Full-app TestClient for every pure validation path
- Keeping tests for dropped surfaces (webhooks, `/api/agent`, owner JSON API) after those routes are deleted — remove them in the same phase as the deletes
- Wall-clock performance assertions in the default suite (mark `slow` or replace with structural checks such as query/REPORT round-trip counts)

## Interim policy (during refactor)

Until phase “rebuild pyramid” in [plan.md](plan.md):

1. Unify harness early so interface moves do not edit four fakes.
2. Keep behavioural coverage that still maps to kept features.
3. When behaviour changes **on purpose** (e.g. rate-limit-before-auth removed with webhooks), update or delete that test in the **same** change.
4. Prefer adding a **pure unit** test when a new domain function appears; defer broad HTTP rewrites to the end.
