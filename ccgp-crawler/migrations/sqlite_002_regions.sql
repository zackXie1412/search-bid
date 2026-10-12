-- Existing SQLite buffers: add nullable region fields without deleting data.
-- The application applies this migration idempotently by checking PRAGMA
-- table_info first; raw ALTER TABLE statements cannot be rerun in SQLite.
ALTER TABLE pending_notices ADD COLUMN region_raw TEXT;
ALTER TABLE pending_notices ADD COLUMN region_code TEXT;
CREATE INDEX IF NOT EXISTS idx_pending_notices_region_published
    ON pending_notices(region_code, published_at);
ALTER TABLE crawl_page_runs ADD COLUMN filtered_count INTEGER NOT NULL DEFAULT 0;
