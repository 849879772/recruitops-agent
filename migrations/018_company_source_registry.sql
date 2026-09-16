CREATE TABLE IF NOT EXISTS company_source_records (
    id VARCHAR(64) PRIMARY KEY,
    source VARCHAR(128) NOT NULL,
    source_record_id VARCHAR(512) NOT NULL,
    company_name VARCHAR(255) NOT NULL,
    source_url VARCHAR(2048) NOT NULL DEFAULT '',
    entry_url VARCHAR(2048) NOT NULL DEFAULT '',
    original_entry_url VARCHAR(2048) NOT NULL DEFAULT '',
    final_url VARCHAR(2048) NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    failure_stage VARCHAR(128) NOT NULL DEFAULT '',
    reason_code VARCHAR(128) NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    job_count INTEGER NOT NULL DEFAULT 0,
    jd_pending_count INTEGER NOT NULL DEFAULT 0,
    last_success_job_count INTEGER NOT NULL DEFAULT 0,
    pagination_complete BOOLEAN,
    last_attempt_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_company_source_records_source_record UNIQUE (source, source_record_id),
    CONSTRAINT ck_company_source_records_status
        CHECK (status IN ('pending', 'running', 'complete', 'partial', 'failed', 'unusable'))
);

CREATE INDEX IF NOT EXISTS ix_company_source_records_status
    ON company_source_records (status);
CREATE INDEX IF NOT EXISTS ix_company_source_records_updated_at
    ON company_source_records (updated_at);

CREATE TABLE IF NOT EXISTS company_source_attempts (
    id VARCHAR(64) PRIMARY KEY,
    record_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempted_url VARCHAR(2048) NOT NULL DEFAULT '',
    final_url VARCHAR(2048) NOT NULL DEFAULT '',
    failure_stage VARCHAR(128) NOT NULL DEFAULT '',
    reason_code VARCHAR(128) NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    job_count INTEGER NOT NULL DEFAULT 0,
    jd_pending_count INTEGER NOT NULL DEFAULT 0,
    pagination_complete BOOLEAN,
    attempted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_company_source_attempts_record
        FOREIGN KEY (record_id) REFERENCES company_source_records (id) ON DELETE CASCADE,
    CONSTRAINT ck_company_source_attempts_status
        CHECK (status IN ('pending', 'running', 'complete', 'partial', 'failed', 'unusable'))
);

CREATE INDEX IF NOT EXISTS ix_company_source_attempts_record_attempted
    ON company_source_attempts (record_id, attempted_at);
