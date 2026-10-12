BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION CURRENT_USER;
REVOKE ALL ON SCHEMA app FROM PUBLIC;

CREATE TABLE IF NOT EXISTS app.regions (
  code text PRIMARY KEY,
  name text NOT NULL UNIQUE,
  aliases text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.notices (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source text NOT NULL DEFAULT 'ccgp',
  source_notice_id text,
  canonical_url text NOT NULL,
  title text NOT NULL,
  published_at timestamptz NOT NULL,
  region_code text REFERENCES app.regions(code),
  region_raw text,
  notice_type text,
  category text NOT NULL,
  source_list_url text NOT NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT notices_url_unique UNIQUE (canonical_url),
  CONSTRAINT notices_title_not_empty CHECK (length(btrim(title)) > 0),
  CONSTRAINT notices_url_http CHECK (canonical_url ~ '^https?://')
);

CREATE UNIQUE INDEX IF NOT EXISTS notices_source_notice_id_uidx
  ON app.notices (source, source_notice_id)
  WHERE source_notice_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS notices_published_idx
  ON app.notices (published_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS notices_region_published_idx
  ON app.notices (region_code, published_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS notices_region_raw_published_idx
  ON app.notices (region_raw, published_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS notices_title_trgm_idx
  ON app.notices USING gin (title gin_trgm_ops);

CREATE TABLE IF NOT EXISTS app.ingest_batches (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_key text NOT NULL UNIQUE,
  received_count integer NOT NULL CHECK (received_count >= 0),
  inserted_count integer NOT NULL DEFAULT 0 CHECK (inserted_count >= 0),
  updated_count integer NOT NULL DEFAULT 0 CHECK (updated_count >= 0),
  source_name text NOT NULL DEFAULT 'local-crawler',
  created_at timestamptz NOT NULL DEFAULT now()
);

GRANT USAGE ON SCHEMA app TO ccgp_zhaobiao_v1_reader_role, ccgp_zhaobiao_v1_ingest_role;
GRANT SELECT ON app.notices, app.regions TO ccgp_zhaobiao_v1_reader_role;
GRANT SELECT, INSERT, UPDATE ON app.notices TO ccgp_zhaobiao_v1_ingest_role;
GRANT SELECT ON app.regions TO ccgp_zhaobiao_v1_ingest_role;
GRANT INSERT, SELECT, UPDATE ON app.ingest_batches TO ccgp_zhaobiao_v1_ingest_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO ccgp_zhaobiao_v1_ingest_role;

COMMIT;
