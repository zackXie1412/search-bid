#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
. ./.env
set +a

action=${1:?usage: $0 enable|disable|revoke KEY_PREFIX}
prefix=${2:?usage: $0 enable|disable|revoke KEY_PREFIX}

case "$action" in
  enable)
    statement="UPDATE app.api_keys SET enabled=true WHERE key_prefix=:'prefix' RETURNING key_prefix, label, enabled;"
    ;;
  disable)
    statement="UPDATE app.api_keys SET enabled=false WHERE key_prefix=:'prefix' RETURNING key_prefix, label, enabled;"
    ;;
  revoke)
    statement="DELETE FROM app.api_keys WHERE key_prefix=:'prefix' RETURNING key_prefix, label;"
    ;;
  *)
    echo "action must be enable, disable, or revoke" >&2
    exit 2
    ;;
esac

docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v prefix="$prefix" -c "$statement"
