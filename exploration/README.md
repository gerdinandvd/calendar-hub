# Exploration: agent surface

Notes for an agent-readable / agent-writable path into calendar-hub.
Not product docs. Not a commitment. A place to decide and then build.

## Read order

1. [architecture.md](architecture.md) — what stays true
2. [surface.md](surface.md) — what agents/scripts call
3. [security.md](security.md) — auth and trust boundaries
4. [decisions.md](decisions.md) — open choices (numbered)

## Constraint

Target host: netcup **VPS nano G11.5s** — 2 vCPU, **2 GB RAM**, 60 GB SSD.
Five Compose services already. Prefer zero new containers.

## Out of scope here

caldav-mcp as the product surface (useful CalDAV client; not hub policy).
Rewriting CalDAV as the only public API.
