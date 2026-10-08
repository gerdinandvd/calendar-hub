#!/bin/sh
# Zet beide accounts in het userbestand en start daarna de image-entrypoint.
# Die entrypoint draait Radicale als de gebruiker radicale.

set -eu

/venv/bin/python /usr/local/bin/calendar-hub-radicale-users.py
exec /usr/local/bin/docker-entrypoint.sh /venv/bin/radicale --config /config/config
