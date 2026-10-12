BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ccgp_zhaobiao_v1_reader_role') THEN
    CREATE ROLE ccgp_zhaobiao_v1_reader_role NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ccgp_zhaobiao_v1_ingest_role') THEN
    CREATE ROLE ccgp_zhaobiao_v1_ingest_role NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ccgp_zhaobiao_v1_reader') THEN
    CREATE ROLE ccgp_zhaobiao_v1_reader LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ccgp_zhaobiao_v1_ingest') THEN
    CREATE ROLE ccgp_zhaobiao_v1_ingest LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END
$$;

GRANT ccgp_zhaobiao_v1_reader_role TO ccgp_zhaobiao_v1_reader;
GRANT ccgp_zhaobiao_v1_ingest_role TO ccgp_zhaobiao_v1_ingest;

COMMIT;
