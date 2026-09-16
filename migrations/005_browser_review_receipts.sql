-- Agent-owned receipts linking consumed browser actions to later observations.

CREATE TABLE IF NOT EXISTS browser_review_receipts (
    review_id VARCHAR(128) NOT NULL,
    target_id VARCHAR(255) NOT NULL,
    action_attempt_id VARCHAR(128) NOT NULL,
    observation_id VARCHAR(255),
    normalized_url VARCHAR(2048) NOT NULL,
    application_ids JSONB NOT NULL,
    entries JSONB,
    result JSONB,
    captured_at TIMESTAMPTZ,
    status VARCHAR(32) NOT NULL DEFAULT 'authorized',
    error_code VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_browser_review_receipts PRIMARY KEY (
        review_id,
        target_id,
        action_attempt_id
    ),
    CONSTRAINT uq_browser_review_receipts_key UNIQUE (
        review_id,
        target_id,
        action_attempt_id
    )
);

CREATE INDEX IF NOT EXISTS ix_browser_review_receipts_status
    ON browser_review_receipts (status);
