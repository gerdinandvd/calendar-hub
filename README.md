# Calendar Hub

A personal hub that brings your calendar sources together in one place — private calendars, webhook sources, and external ICS feeds — behind HTTPS and basic auth.

## Quick start

1. Copy the example environment file:

```text
copy .env.example .env
```

2. Set the required variables in `.env`:

- `HUB_USER` — username for the admin UI and CalDAV clients
- `HUB_PASSWORD` — password for `HUB_USER`
- `HUB_INTERNAL_USER` — internal account used between the hub and Radicale
- `HUB_INTERNAL_PASSWORD` — password for `HUB_INTERNAL_USER`
- `HUB_SECRET_KEY` — encrypts webhook secrets (keep the real key outside backups)

3. Start the stack:

```text
docker compose up -d --build
```

4. Open `https://localhost:8443/` and sign in with `HUB_USER` and `HUB_PASSWORD`.

## Using the hub

**Private calendars** — Add a private source from **Sources**. The **Calendars** page lists its name, color, and collection URL. Clients sign in as `HUB_USER` on that URL. Turning a source off keeps the collection; deleting a source removes the collection as well.

**Webhooks** — Add a webhook from **Sources**. The endpoint and secret are shown once; use **Rotate secret** to replace the secret immediately. The **Sync** page shows whether the last run succeeded. Requests to a disabled source are refused.

**External ICS feeds** — Add an external ICS feed from **Sources**. The hub polls the feed and writes events into Radicale. Changes made on a phone or desktop client are not written back to the upstream feed.

## Stop

```text
docker compose down
```
