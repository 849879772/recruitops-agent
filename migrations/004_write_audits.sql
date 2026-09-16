-- Durable audit records for approval-gated writes.

CREATE TABLE IF NOT EXISTS write_audits (
    execution_id VARCHAR(128) PRIMARY KEY,
    token_id VARCHAR(128) NOT NULL,
    task_id VARCHAR(128) NOT NULL,
    operation VARCHAR(128) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    operator VARCHAR(200) NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_digest VARCHAR(64),
    before_diff JSONB,
    after_diff JSONB,
    backup_ref VARCHAR(2048),
    rollback_payload JSONB,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    success BOOLEAN NOT NULL,
    error_code VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_write_audits_idempotency_key UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_write_audits_task_id ON write_audits (task_id);
CREATE INDEX IF NOT EXISTS ix_write_audits_success ON write_audits (success);
