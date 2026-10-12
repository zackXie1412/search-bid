BEGIN;

ALTER TABLE app.ingest_batches
  ADD COLUMN IF NOT EXISTS payload_sha256 text;

UPDATE app.ingest_batches
SET payload_sha256 = repeat('0', 64)
WHERE payload_sha256 IS NULL;

ALTER TABLE app.ingest_batches
  ALTER COLUMN payload_sha256 SET NOT NULL;

COMMIT;
