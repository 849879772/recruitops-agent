-- Confirmation never changes the application stage. Revocation retains its audit.
CREATE TABLE IF NOT EXISTS application_identity_bindings (
    application_id VARCHAR(255) PRIMARY KEY REFERENCES application_snapshots(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    state VARCHAR(16) NOT NULL,
    identity_digest VARCHAR(64) NOT NULL,
    page_url VARCHAR(2048) NOT NULL,
    card JSON NOT NULL,
    operation_id VARCHAR(128),
    approval_key VARCHAR(255) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
