# Calendar Hub

Persoonlijke hub die kalenderbronnen op één plek zet. Deze boom is het skeleton: de app start en stopt, `GET /api/health` geeft de basisstatus, en Docker Compose start vijf diensten. Sync, bronbeheer, authenticatie, de Admin UI en backups zitten er nog niet in.

## Tests zonder Docker

```text
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\pytest -m "not integration"
```

Op Linux of macOS wijst de venv naar `.venv/bin/python` in plaats van `.venv\Scripts\python`.

## Stack

Kopieer het omgevingsbestand en start de stack:

```text
copy .env.example .env
docker compose up -d --build
docker compose ps
```

Vijf diensten horen te draaien: `hub`, `radicale`, `caddy`, `litestream` en `backup`. Alleen Caddy publiceert een hostpoort. `http://127.0.0.1:8080/` antwoordt met een vaste tekst. Hub en Radicale luisteren alleen op het Docker-netwerk. De hub koppelt het Radicale-volume niet.

Litestream en de backup-dienst starten hun programma en blijven daarna draaien. Ze repliceren niet en maken geen snapshot.

Stoppen:

```text
docker compose down
```

## Koude start als test

De integratietest start de stack, wacht tot de diensten gezond zijn, en stopt ze weer inclusief volumes. Docker moet daarvoor draaien.

```text
.venv\Scripts\pytest -m integration
```

`pytest` zonder marker draait de gewone tests en deze koude start samen.
