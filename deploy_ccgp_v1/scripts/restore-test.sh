#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/zhaobiao-ccgp-v1"
cd "$PROJECT_DIR"
set -a
. "$PROJECT_DIR/.env"
set +a

RESTORE_DB="ccgp_zhaobiao_v1_restore_test"
LATEST_BACKUP="$(ls -1t "$PROJECT_DIR"/backups/ccgp_zhaobiao_v1_*.dump | head -1)"

docker compose exec -T postgres dropdb --if-exists -U "$POSTGRES_USER" "$RESTORE_DB"
docker compose exec -T postgres createdb -U "$POSTGRES_USER" "$RESTORE_DB"
docker compose exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$RESTORE_DB" --no-owner --no-acl < "$LATEST_BACKUP"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$RESTORE_DB" -v ON_ERROR_STOP=1 \
  -c "SELECT count(*) AS restored_notices FROM app.notices;"
docker compose exec -T postgres dropdb -U "$POSTGRES_USER" "$RESTORE_DB"
