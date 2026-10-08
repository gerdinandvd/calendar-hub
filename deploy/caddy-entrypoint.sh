#!/bin/sh
# Hasht het eigenaarswachtwoord en zet het Basic-token van het interne
# account in de omgeving. Beide blijven uit git en uit de Caddyfile.

set -eu

if [ -z "${HUB_USER:-}" ] || [ -z "${HUB_PASSWORD:-}" ]; then
    echo "HUB_USER en HUB_PASSWORD zijn verplicht" >&2
    exit 1
fi

if [ -z "${HUB_INTERNAL_USER:-}" ] || [ -z "${HUB_INTERNAL_PASSWORD:-}" ]; then
    echo "HUB_INTERNAL_USER en HUB_INTERNAL_PASSWORD zijn verplicht" >&2
    exit 1
fi

HASH=$(caddy hash-password --plaintext "$HUB_PASSWORD" | tr -d '\r\n')
export HUB_PASSWORD_HASH="$HASH"

BASIC=$(printf '%s:%s' "$HUB_INTERNAL_USER" "$HUB_INTERNAL_PASSWORD" | base64 | tr -d '\r\n ')
export HUB_INTERNAL_BASIC="$BASIC"

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
