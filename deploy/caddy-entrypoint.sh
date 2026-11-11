#!/bin/sh
# Hashes the owner password and puts the internal account's Basic token
# in the environment. Both stay out of git and out of the Caddyfile.
# The site names and TLS mode also stay in the environment. This script
# writes them into a runtime config and does not change the mounted file.

set -eu

if [ -z "${HUB_USER:-}" ] || [ -z "${HUB_PASSWORD:-}" ]; then
    echo "HUB_USER and HUB_PASSWORD are required" >&2
    exit 1
fi

if [ -z "${HUB_INTERNAL_USER:-}" ] || [ -z "${HUB_INTERNAL_PASSWORD:-}" ]; then
    echo "HUB_INTERNAL_USER and HUB_INTERNAL_PASSWORD are required" >&2
    exit 1
fi

SITES="${HUB_CADDY_SITES:-localhost, 127.0.0.1}"
TLS="${HUB_TLS:-internal}"

case "$TLS" in
    internal) TLS_LINE='	tls internal' ;;
    public) TLS_LINE='' ;;
    *)
        echo "HUB_TLS must be internal or public" >&2
        exit 1
        ;;
esac

trim() {
    value=$1
    while :; do
        case $value in
            [[:space:]]*) value=${value#?} ;;
            *[[:space:]]) value=${value%?} ;;
            *) break ;;
        esac
    done
    printf '%s' "$value"
}

SITE_ADDRS=""
HAS_LOOPBACK=0
OLDIFS=$IFS
IFS=','
for raw in $SITES; do
    token=$(trim "$raw")
    if [ -z "$token" ]; then
        echo "HUB_CADDY_SITES contains an empty name" >&2
        exit 1
    fi
    if ! printf '%s' "$token" | grep -Eq '^[]A-Za-z0-9.:[-]+$'; then
        echo "HUB_CADDY_SITES has an invalid name: $token" >&2
        exit 1
    fi
    if [ "$token" = "127.0.0.1" ]; then
        HAS_LOOPBACK=1
    fi
    if [ -z "$SITE_ADDRS" ]; then
        SITE_ADDRS="https://$token"
    else
        SITE_ADDRS="$SITE_ADDRS, https://$token"
    fi
done
IFS=$OLDIFS

HASH=$(caddy hash-password --plaintext "$HUB_PASSWORD" | tr -d '\r\n')
export HUB_PASSWORD_HASH="$HASH"

BASIC=$(printf '%s:%s' "$HUB_INTERNAL_USER" "$HUB_INTERNAL_PASSWORD" | base64 | tr -d '\r\n ')
export HUB_INTERNAL_BASIC="$BASIC"

RUNTIME=/tmp/calendar-hub.Caddyfile
{
    printf '%s\n' '{' '	auto_https disable_redirects' '}' '' 'import /etc/caddy/Caddyfile' '' "$SITE_ADDRS {"
    if [ -n "$TLS_LINE" ]; then
        printf '%s\n' "$TLS_LINE"
    fi
    printf '%s\n' '	import hub' '}'
    if [ "$TLS" = "public" ] && [ "$HAS_LOOPBACK" -eq 0 ]; then
        printf '%s\n' '' 'https://127.0.0.1 {' '	tls internal' '	import hub' '}'
    fi
} > "$RUNTIME"

exec caddy run --config "$RUNTIME" --adapter caddyfile
