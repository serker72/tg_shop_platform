#!/bin/sh
# db_update: wait for PostgreSQL -> alembic upgrade head -> sync pg_partman retention.
# POSIX sh only (docker-compose: command ["sh", "ops/scripts/migrate.sh"]).
# Any failing step stops the script with a non-zero exit code.
set -eu

cd "$(dirname "$0")/../.."

log() {
    printf '[migrate] %s\n' "$*" >&2
}

log "waiting for PostgreSQL (timeout ${MIGRATE_WAIT_SECONDS:-60}s)"
python ops/scripts/wait_for_db.py

log "alembic upgrade head"
alembic upgrade head

log "sync partman retention"
python ops/scripts/sync_partman_retention.py

log "done"
