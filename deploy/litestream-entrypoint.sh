#!/bin/sh
# Replicates the hub database. Restore is a separate manual step: `sh this-file restore`.
# Debug logs are required because Litestream 0.5 writes `replica sync` at debug, not info.

set -eu

DB=/data/hub.sqlite
STATUS=/status/litestream-sync
REPLICA_URL="${LITESTREAM_REPLICA_URL:-file:///replica/hub}"

case "$REPLICA_URL" in
  *\"*|*'$'*|*' '*|*'#'*)
    echo "LITESTREAM_REPLICA_URL is not a plain URL" >&2
    exit 1
    ;;
esac

if [ "${1:-}" = "restore" ]; then
  rm -f "$DB" "${DB}-wal" "${DB}-shm" "${DB}-journal"
  exec /usr/local/bin/litestream restore -o "$DB" "$REPLICA_URL"
fi

while [ ! -f "$DB" ]; do
  sleep 1
done

mkdir -p /status /tmp
umask 077
cat > /tmp/litestream.yml <<EOF
dbs:
  - path: ${DB}
    replica:
      url: "${REPLICA_URL}"
EOF

last=0
/usr/local/bin/litestream replicate -config /tmp/litestream.yml -log-level debug 2>&1 |
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s\n' "$line"
    case "$line" in
      *'level=ERROR'*)
        last=0
        ;;
      *'msg="replica sync"'*)
        db=$(printf '%s\n' "$line" | sed -n 's/.*txid\.db=\([^ ]*\).*/\1/p')
        replica=$(printf '%s\n' "$line" | sed -n 's/.*txid\.replica=\([^ ]*\).*/\1/p')
        if [ -n "$db" ] && [ "$db" = "$replica" ]; then
          now=$(date +%s)
          if [ "$last" -eq 0 ] || [ $((now - last)) -ge 60 ]; then
            stamp=$(printf '%s\n' "$line" | sed -n 's/^time=\([0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\).*/\1Z/p')
            if [ -z "$stamp" ]; then
              stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
            fi
            printf '%s\n' "$stamp" > "${STATUS}.tmp"
            mv "${STATUS}.tmp" "$STATUS"
            last=$now
          fi
        fi
        ;;
    esac
  done
