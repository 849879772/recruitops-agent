-- Durable evidence for every read-only mailbox synchronization attempt.

CREATE TABLE IF NOT EXISTS recruitment_mail_sync_runs (
    operation_id VARCHAR(128) PRIMARY KEY,
    run_id VARCHAR(128) NOT NULL,
    account_key VARCHAR(128) NOT NULL,
    mailbox VARCHAR(256) NOT NULL,
    status VARCHAR(32) NOT NULL,
    fetched INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    reused INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 1,
    cursor_before TEXT,
    cursor TEXT,
    error VARCHAR(512),
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS ix_recruitment_mail_sync_runs_run_id
    ON recruitment_mail_sync_runs (run_id);
CREATE INDEX IF NOT EXISTS ix_recruitment_mail_sync_runs_started_at
    ON recruitment_mail_sync_runs (started_at);

CREATE TABLE IF NOT EXISTS recruitment_mail_sync_items (
    id VARCHAR(128) PRIMARY KEY,
    operation_id VARCHAR(128) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    mail_record_id VARCHAR(128) NOT NULL,
    disposition VARCHAR(16) NOT NULL,
    evidence_ref VARCHAR(512) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_mail_sync_item UNIQUE (operation_id, mail_record_id)
);

CREATE INDEX IF NOT EXISTS ix_recruitment_mail_sync_items_operation_id
    ON recruitment_mail_sync_items (operation_id);
