-- Agent-owned PostgreSQL schema. The autumn source SQLite/JSON store is read-only.

CREATE TABLE IF NOT EXISTS task_runs (
    id VARCHAR(128) PRIMARY KEY,
    idempotency_key VARCHAR(255),
    task_type VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    user_request TEXT NOT NULL,
    current_step VARCHAR(255),
    step_count INTEGER NOT NULL DEFAULT 0 CHECK (step_count >= 0),
    max_steps INTEGER NOT NULL DEFAULT 12 CHECK (max_steps BETWEEN 1 AND 50),
    error_code VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(128) NOT NULL,
    source_ref VARCHAR(512),
    CONSTRAINT uq_task_runs_idempotency_key UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_task_runs_status ON task_runs (status);

CREATE TABLE IF NOT EXISTS approvals (
    id VARCHAR(128) PRIMARY KEY,
    task_id VARCHAR(128) NOT NULL REFERENCES task_runs (id) ON DELETE CASCADE,
    operation VARCHAR(128) NOT NULL,
    preview JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    idempotency_key VARCHAR(255) NOT NULL,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(128) NOT NULL,
    source_ref VARCHAR(512),
    CONSTRAINT uq_approvals_idempotency_key UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_approvals_task_id ON approvals (task_id);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals (status);

CREATE TABLE IF NOT EXISTS tool_calls (
    id VARCHAR(128) PRIMARY KEY,
    task_id VARCHAR(128) NOT NULL REFERENCES task_runs (id) ON DELETE CASCADE,
    idempotency_key VARCHAR(255),
    tool_name VARCHAR(128) NOT NULL,
    arguments JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_summary TEXT,
    success BOOLEAN,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    error_code VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(128) NOT NULL,
    source_ref VARCHAR(512),
    CONSTRAINT uq_tool_calls_idempotency_key UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_tool_calls_task_id ON tool_calls (task_id);

CREATE TABLE IF NOT EXISTS company_snapshots (
    id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    campus_url VARCHAR(2048),
    crawler_key VARCHAR(128),
    integration_status VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(128) NOT NULL,
    source_ref VARCHAR(512),
    CONSTRAINT uq_company_snapshots_source_ref UNIQUE (source, source_ref)
);

CREATE TABLE IF NOT EXISTS job_snapshots (
    id VARCHAR(255) PRIMARY KEY,
    company_id VARCHAR(255) NOT NULL,
    title VARCHAR(512) NOT NULL,
    city VARCHAR(255),
    detail_url VARCHAR(2048) NOT NULL,
    jd_raw TEXT,
    cohort INTEGER,
    cohort_status VARCHAR(64) NOT NULL,
    batch VARCHAR(64) NOT NULL,
    match_score INTEGER,
    first_seen_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(128) NOT NULL,
    source_ref VARCHAR(512),
    CONSTRAINT uq_job_snapshots_source_ref UNIQUE (source, source_ref)
);

CREATE INDEX IF NOT EXISTS ix_job_snapshots_company_id ON job_snapshots (company_id);
CREATE INDEX IF NOT EXISTS ix_job_snapshots_cohort ON job_snapshots (cohort, cohort_status);

CREATE TABLE IF NOT EXISTS job_analysis_snapshots (
    job_id VARCHAR(255) PRIMARY KEY REFERENCES job_snapshots (id) ON DELETE CASCADE,
    match_score INTEGER,
    advantages TEXT,
    gaps TEXT,
    summary TEXT,
    recommendation TEXT,
    score_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_level VARCHAR(64),
    matched_directions JSONB NOT NULL DEFAULT '[]'::jsonb,
    primary_match_direction VARCHAR(128),
    analysis_status VARCHAR(64),
    model VARCHAR(128),
    analyzed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(128) NOT NULL,
    source_ref VARCHAR(512)
);

CREATE TABLE IF NOT EXISTS application_snapshots (
    id VARCHAR(255) PRIMARY KEY,
    company_name VARCHAR(255) NOT NULL,
    job_title VARCHAR(512) NOT NULL,
    job_id VARCHAR(255),
    record_url VARCHAR(2048),
    stage VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    note TEXT,
    stage_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_stage VARCHAR(64),
    source_status VARCHAR(255),
    source_status_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(128) NOT NULL,
    source_ref VARCHAR(512),
    CONSTRAINT uq_application_snapshots_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT uq_application_snapshots_source_ref UNIQUE (source, source_ref)
);

CREATE INDEX IF NOT EXISTS ix_application_snapshots_stage ON application_snapshots (stage);

CREATE TABLE IF NOT EXISTS schedule_event_snapshots (
    id VARCHAR(255) PRIMARY KEY,
    title VARCHAR(512) NOT NULL,
    event_date DATE NOT NULL,
    event_time TIME,
    event_type VARCHAR(64) NOT NULL,
    company_name VARCHAR(255) NOT NULL,
    job_title VARCHAR(512) NOT NULL,
    application_stage VARCHAR(64) NOT NULL,
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    application_id VARCHAR(255),
    location_or_link VARCHAR(2048),
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(128) NOT NULL,
    source_ref VARCHAR(512),
    CONSTRAINT uq_schedule_event_snapshots_source_ref UNIQUE (source, source_ref)
);

CREATE INDEX IF NOT EXISTS ix_schedule_event_snapshots_event_date
    ON schedule_event_snapshots (event_date);
