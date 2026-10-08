# Calendar Hub

Persoonlijke hub die kalenderbronnen op één plek zet. De stack start vijf diensten. Caddy beëindigt TLS en vraagt basic auth voor de hub. Sync, bronbeheer, de Admin UI en backups zitten er nog niet in.

## Tests zonder Docker

```text
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\pytest -m "not integration"
```

Op Linux of macOS wijst de venv naar `.venv/bin/python` in plaats van `.venv\Scripts\python`.

## Stack

Kopieer het omgevingsbestand, zet daarin `HUB_USER`, `HUB_PASSWORD`, `HUB_INTERNAL_USER` en `HUB_INTERNAL_PASSWORD`, en start de stack:

```text
copy .env.example .env
docker compose up -d --build
docker compose ps
```

Vijf diensten horen te draaien: `hub`, `radicale`, `caddy`, `litestream` en `backup`. Alleen Caddy publiceert een hostpoort, `8443` naar HTTPS. `https://localhost:8443/` antwoordt 401 zonder wachtwoord. Met `HUB_USER` is de hub bereikbaar; een Admin UI is er nog niet.

`/api/webhooks/` en `/api/oauth/google/callback` vragen geen basic auth. `/radicale/` vraagt het wachtwoord van `HUB_USER` aan Radicale. Het interne account schrijft later vanaf het Docker-netwerk en werkt niet via Caddy. Hub en Radicale publiceren zelf geen poort. De hub koppelt het Radicale-volume niet.

Litestream en de backup-dienst starten hun programma en blijven daarna draaien. Ze repliceren niet en maken geen snapshot.

Stoppen:

```text
docker compose down
```

## Koude start als test

De integratietest start de stack, controleert TLS, basic auth en CalDAV, en stopt ze weer inclusief volumes. Docker moet daarvoor draaien.

```text
.venv\Scripts\pytest -m integration
```

`pytest` zonder marker draait de gewone tests en deze koude start samen.
