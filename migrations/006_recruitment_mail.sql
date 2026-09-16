-- Local-first recruitment mail records. Bodies and parser payloads are redacted by the storage boundary.

CREATE TABLE IF NOT EXISTS recruitment_emails (
    id VARCHAR(128) PRIMARY KEY,
    dedupe_key VARCHAR(128) NOT NULL,
    dedupe_kind VARCHAR(16) NOT NULL,
    mailbox VARCHAR(256) NOT NULL,
    message_id VARCHAR(512) NOT NULL,
    thread_id VARCHAR(512),
    account_ref VARCHAR(512),
    sender VARCHAR(1000),
    recipients JSON NOT NULL,
    subject VARCHAR(2000) NOT NULL,
    body_text VARCHAR(200000) NOT NULL,
    received_at TIMESTAMP WITH TIME ZONE,
    content_digest VARCHAR(64) NOT NULL,
    raw_metadata JSON NOT NULL,
    parsed_result JSON NOT NULL,
    category VARCHAR(64) NOT NULL,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    pending_confirmation_reasons JSON NOT NULL,
    safety_flags JSON NOT NULL,
    redacted_fields JSON NOT NULL,
    requires_confirmation BOOLEAN NOT NULL,
    application_id VARCHAR(255),
    job_id VARCHAR(255),
    company_id VARCHAR(255),
    processing_status VARCHAR(64) NOT NULL DEFAULT 'pending',
    processing_error VARCHAR(512),
    processed_at TIMESTAMP WITH TIME ZONE,
    source VARCHAR(128) NOT NULL DEFAULT 'local',
    source_ref VARCHAR(512),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_recruitment_emails_dedupe_key UNIQUE (dedupe_key)
);

CREATE INDEX IF NOT EXISTS ix_recruitment_emails_mailbox_message_id
    ON recruitment_emails (mailbox, message_id);
CREATE INDEX IF NOT EXISTS ix_recruitment_emails_received_at
    ON recruitment_emails (received_at);
CREATE INDEX IF NOT EXISTS ix_recruitment_emails_category
    ON recruitment_emails (category);
CREATE INDEX IF NOT EXISTS ix_recruitment_emails_processing_status
    ON recruitment_emails (processing_status);

CREATE TABLE IF NOT EXISTS recruitment_mail_cursors (
    account_key VARCHAR(128) NOT NULL,
    mailbox VARCHAR(256) NOT NULL,
    token TEXT,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_key, mailbox)
);
