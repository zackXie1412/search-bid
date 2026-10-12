#!/usr/bin/env bash
set -euo pipefail
umask 077

PROJECT_DIR="/opt/zhaobiao-ccgp-v1"
BACKUP_DIR="$PROJECT_DIR/backups"
STAMP="$(date +%Y%m%d_%H%M%S)"

cd "$PROJECT_DIR"
set -a
. "$PROJECT_DIR/.env"
set +a

mkdir -p "$BACKUP_DIR"
docker compose exec -T postgres \
  pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --format custom --no-owner --no-acl \
  > "$BACKUP_DIR/ccgp_zhaobiao_v1_${STAMP}.dump"

test -s "$BACKUP_DIR/ccgp_zhaobiao_v1_${STAMP}.dump"
find "$BACKUP_DIR" -type f -name 'ccgp_zhaobiao_v1_*.dump' -mtime +14 -delete
