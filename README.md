# Calendar Hub

Personal hub that puts calendar sources in one place. The stack starts five services. Caddy terminates TLS and asks for basic auth in front of the hub. This build manages private calendars, webhook sources, and external ICS feeds. Litestream replicates the source database and Kopia snapshots the calendar files.

## Tests without Docker

```text
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\pytest
```

Plain `pytest` skips Docker integration and formal/long (`slow`) tests. On Linux or macOS the venv points at `.venv/bin/python` instead of `.venv\Scripts\python`.

Coverage on `app/` (branch, default suite only; not in every plain `pytest` run):

```text
.venv\Scripts\pytest --cov=app --cov-branch --cov-report=term-missing
```

## Lint

Dev install includes Ruff, Pylint, Pyright, and pytest-cov. Format and lint with Ruff on `app/` and `tests/`; run Pylint design checks and Pyright type checks on `app/`:

```text
.venv\Scripts\ruff check app tests
.venv\Scripts\ruff format app tests
.venv\Scripts\pylint app
.venv\Scripts\pyright
```

Optional on-commit hooks: `pre-commit install` (see `.pre-commit-config.yaml`; Ruff and Pyright). Tool roles are described in `docs/python-toolchain.md`.

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

Add an external ICS feed from Sources. The hub polls the feed and writes events into Radicale. Changes on phone or Thunderbird are not written back to the feed.

`/api/webhooks/` does not ask for basic auth. `/radicale/` asks Radicale for the `HUB_USER` password. The internal account does not work through Caddy. Hub and Radicale do not publish a port themselves. The hub does not mount the Radicale volume.

Litestream replicates the SQLite file to the destination in `LITESTREAM_REPLICA_URL`. Kopia snapshots the Radicale volume every 6 hours. The local example uses directories inside Compose volumes. On the VPS those destinations are private B2 buckets. `HUB_SECRET_KEY` and `KOPIA_PASSWORD` stay in the environment file, outside the backup. The health page shows SQLite, Radicale, the Litestream heartbeat, the backup heartbeat, and free disk.

Stop:

```text
docker compose down
```

## Cold start as a test

Integration smoke starts the stack, checks TLS, basic auth, and CalDAV, and stops it again including volumes. Docker has to be running for that. Formal/long checks (`slow`) stay out of the default and smoke runs.

```text
.venv\Scripts\pytest -m "integration and not slow"
```

The large ICS import is marked `slow`. It defaults to 200 events and a 60 second timeout. That is not the formal T-03 / PQ-PE measure from the design (5,000 events within 10 minutes). Set the environment variables below for the full run:

```text
set PERF_EVENT_COUNT=5000
set PERF_TIMEOUT_SECONDS=600
.venv\Scripts\pytest -m slow -k large_ics_import
```
