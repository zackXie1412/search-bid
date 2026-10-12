-- Auditable schema for SQLite-to-cloud synchronization. The application
-- rebuilds pending_notices in code when upgrading its CHECK constraint.
CREATE TABLE IF NOT EXISTS sync_batches (
    batch_key TEXT PRIMARY KEY,
    payload_sha256 TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    status TEXT NOT NULL CHECK (status IN ('syncing', 'synced', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_batches_status_updated
    ON sync_batches(status, updated_at);

CREATE TABLE IF NOT EXISTS sync_batch_items (
    batch_key TEXT NOT NULL REFERENCES sync_batches(batch_key) ON DELETE CASCADE,
    canonical_url TEXT NOT NULL REFERENCES pending_notices(canonical_url) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (batch_key, canonical_url),
    UNIQUE (batch_key, position)
);
