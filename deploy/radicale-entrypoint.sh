#!/bin/sh
# Writes both accounts into the user file, then starts the image entrypoint.
# That entrypoint runs Radicale as the radicale user.

set -eu

/venv/bin/python /usr/local/bin/calendar-hub-radicale-users.py
/venv/bin/python /usr/local/bin/calendar-hub-radicale-tune.py
exec /usr/local/bin/docker-entrypoint.sh /venv/bin/radicale --config /config/config
