#!/bin/sh
# Snapshots the Radicale volume every 6 hours. No Kopia server and no UI.
# Restore is a separate manual step: `sh this-file restore` with /restore mounted writable.

set -eu

export KOPIA_CACHE_DIRECTORY="${KOPIA_CACHE_DIRECTORY:-/cache}"
export KOPIA_CONFIG_PATH="${KOPIA_CONFIG_PATH:-/cache/kopia.config}"
export KOPIA_LOG_DIR="${KOPIA_LOG_DIR:-/cache/log}"
export KOPIA_CHECK_FOR_UPDATES=false
export KOPIA_USE_KEYRING=false

STATUS=/status/kopia-backup
SOURCE=/source
KIND="${KOPIA_REPOSITORY:-filesystem}"
INTERVAL=21600
RETRY=60

mkdir -p "$KOPIA_CACHE_DIRECTORY" "$KOPIA_LOG_DIR" /status

if [ -z "${KOPIA_PASSWORD:-}" ]; then
  echo "KOPIA_PASSWORD is required" >&2
  exit 1
fi

connect() {
  if [ "$KIND" = "filesystem" ]; then
    path="${KOPIA_REPO_PATH:-/repo}"
    if /bin/kopia repository connect filesystem --path="$path"; then
      return 0
    fi
    /bin/kopia repository create filesystem --path="$path"
    return $?
  fi
  if [ "$KIND" = "b2" ]; then
    if [ -z "${B2_BUCKET:-}" ]; then
      echo "B2_BUCKET is required" >&2
      return 1
    fi
    prefix="${KOPIA_PREFIX:-radicale}"
    if /bin/kopia repository connect b2 --bucket="$B2_BUCKET" --prefix="$prefix"; then
      return 0
    fi
    /bin/kopia repository create b2 --bucket="$B2_BUCKET" --prefix="$prefix"
    return $?
  fi
  echo "KOPIA_REPOSITORY must be filesystem or b2" >&2
  return 1
}

if [ "${1:-}" = "restore" ]; then
  connect
  exec /bin/kopia snapshot restore --delete-extra --snapshot-time=latest "$SOURCE" /restore
fi

while true; do
  if connect && /bin/kopia snapshot create "$SOURCE"; then
    date -u +%Y-%m-%dT%H:%M:%SZ > "${STATUS}.tmp"
    mv "${STATUS}.tmp" "$STATUS"
    sleep "$INTERVAL"
  else
    echo "kopia snapshot failed" >&2
    sleep "$RETRY"
  fi
done
