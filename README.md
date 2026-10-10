# Calendar Hub

Personal hub that puts calendar sources in one place. The stack starts five services. Caddy terminates TLS and asks for basic auth in front of the hub. This build manages private calendars, webhook sources, external ICS feeds, and a read-only Google calendar. Litestream replicates the source database and Kopia snapshots the calendar files. Installation and restore are described in [docs/developers-guide.md](docs/developers-guide.md).

## Tests without Docker

```text
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\pytest -m "not integration"
```

On Linux or macOS the venv points at `.venv/bin/python` instead of `.venv\Scripts\python`.

## Stack

Copy the environment file, set `HUB_USER`, `HUB_PASSWORD`, `HUB_INTERNAL_USER`, `HUB_INTERNAL_PASSWORD`, and `HUB_SECRET_KEY` in it, and start the stack. `HUB_SECRET_KEY` encrypts webhook secrets. Keep the real key outside backups.

```text
copy .env.example .env
docker compose up -d --build
docker compose ps
```

Five services should be running: `hub`, `radicale`, `caddy`, `litestream`, and `backup`. Only Caddy publishes a host port, `8443` for HTTPS. `https://localhost:8443/` returns 401 without a password. With `HUB_USER` the hub opens the admin pages.

`HUB_HOST` is the host shown for CalDAV. Locally that is `localhost:8443`, so the collection URL includes the published port. The hub reaches Radicale at `HUB_RADICALE_URL` (`http://radicale:5232` on the Docker network) with the internal account. That account is not shown in the admin pages.

Add a private source from Sources. The calendars page lists its name, color, and collection URL. Clients sign in as `HUB_USER` on that URL. Turning a source off keeps the collection. Deleting a source asks you to confirm, because the collection is deleted with it.

Add a webhook from Sources. The page shows the endpoint and the secret once. After that it says the secret is set. Rotate secret replaces it immediately. The sync page shows whether the last run succeeded. A webhook with the source turned off is refused and does not change the calendar.

Add a Google calendar from Sources. Its page links to Google and says the connection only reads and does not write back. A live account needs `HUB_GOOGLE_CLIENT_ID` and `HUB_GOOGLE_CLIENT_SECRET`, with the redirect URL `/api/oauth/google/callback`. Without those values the hub still runs.

`/api/webhooks/` and `/api/oauth/google/callback` do not ask for basic auth. `/radicale/` asks Radicale for the `HUB_USER` password. The internal account does not work through Caddy. Hub and Radicale do not publish a port themselves. The hub does not mount the Radicale volume.

Litestream replicates the SQLite file to the destination in `LITESTREAM_REPLICA_URL`. Kopia snapshots the Radicale volume every 6 hours. The local example uses directories inside Compose volumes. On the VPS those destinations are private B2 buckets. `HUB_SECRET_KEY` and `KOPIA_PASSWORD` stay in the environment file, outside the backup. The health page shows SQLite, Radicale, the Litestream heartbeat, the backup heartbeat, and free disk.

Stop:

```text
docker compose down
```

## Cold start as a test

The integration test starts the stack, checks TLS, basic auth, and CalDAV, and stops it again including volumes. Docker has to be running for that.

```text
.venv\Scripts\pytest -m integration
```

`pytest` without a marker runs the ordinary tests and this cold start together.
