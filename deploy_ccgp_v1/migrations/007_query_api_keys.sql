BEGIN;

CREATE TABLE IF NOT EXISTS app.api_keys (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key_prefix text NOT NULL UNIQUE,
  key_hash text NOT NULL UNIQUE CHECK (key_hash ~ '^[a-f0-9]{64}$'),
  label text NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 200),
  enabled boolean NOT NULL DEFAULT true,
  expires_at timestamptz,
  requests_per_minute integer NOT NULL DEFAULT 60 CHECK (requests_per_minute BETWEEN 1 AND 10000),
  created_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON app.api_keys TO ccgp_zhaobiao_v1_reader_role;

COMMIT;
