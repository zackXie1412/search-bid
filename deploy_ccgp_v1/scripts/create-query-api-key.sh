#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
. ./.env
set +a

label=${1:?usage: $0 LABEL [RATE_PER_MINUTE]}
rate=${2:-60}
if ! [[ "$rate" =~ ^[0-9]+$ ]] || (( rate < 1 || rate > 10000 )); then
  echo "RATE_PER_MINUTE must be an integer between 1 and 10000" >&2
  exit 2
fi
key="ccgp_$(openssl rand -hex 32)"
prefix=${key:0:16}
hash=$(printf '%s' "$key" | openssl dgst -sha256 | awk '{print $2}')
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v key_prefix="$prefix" -v key_hash="$hash" -v label="$label" -v rate="$rate" <<'SQL'
INSERT INTO app.api_keys(key_prefix, key_hash, label, requests_per_minute)
VALUES (:'key_prefix', :'key_hash', :'label', :rate);
SQL
printf 'API key (shown once): %s\n' "$key"
