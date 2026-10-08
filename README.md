# Calendar Hub

Personal hub that puts calendar sources in one place. The stack starts five services. Caddy terminates TLS and asks for basic auth in front of the hub. Sync, source management, the Admin UI, and backups are not included yet.

## Tests without Docker

```text
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\pytest -m "not integration"
```

On Linux or macOS the venv points at `.venv/bin/python` instead of `.venv\Scripts\python`.

## Stack

Copy the environment file, set `HUB_USER`, `HUB_PASSWORD`, `HUB_INTERNAL_USER`, and `HUB_INTERNAL_PASSWORD` in it, and start the stack:

```text
copy .env.example .env
docker compose up -d --build
docker compose ps
```

Five services should be running: `hub`, `radicale`, `caddy`, `litestream`, and `backup`. Only Caddy publishes a host port, `8443` for HTTPS. `https://localhost:8443/` returns 401 without a password. With `HUB_USER` the hub is reachable; there is no Admin UI yet.

`/api/webhooks/` and `/api/oauth/google/callback` do not ask for basic auth. `/radicale/` asks Radicale for the `HUB_USER` password. The internal account writes later from the Docker network and does not work through Caddy. Hub and Radicale do not publish a port themselves. The hub does not mount the Radicale volume.

Litestream and the backup service start their program and then keep running. They do not replicate and they do not take a snapshot.

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
