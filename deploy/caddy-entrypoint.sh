#!/bin/sh
# Hashes the owner password and puts the internal account's Basic token
# in the environment. Both stay out of git and out of the Caddyfile.

set -eu

if [ -z "${HUB_USER:-}" ] || [ -z "${HUB_PASSWORD:-}" ]; then
    echo "HUB_USER and HUB_PASSWORD are required" >&2
    exit 1
fi

if [ -z "${HUB_INTERNAL_USER:-}" ] || [ -z "${HUB_INTERNAL_PASSWORD:-}" ]; then
    echo "HUB_INTERNAL_USER and HUB_INTERNAL_PASSWORD are required" >&2
    exit 1
fi

HASH=$(caddy hash-password --plaintext "$HUB_PASSWORD" | tr -d '\r\n')
export HUB_PASSWORD_HASH="$HASH"

BASIC=$(printf '%s:%s' "$HUB_INTERNAL_USER" "$HUB_INTERNAL_PASSWORD" | base64 | tr -d '\r\n ')
export HUB_INTERNAL_BASIC="$BASIC"

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
