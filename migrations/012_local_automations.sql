-- Persist active local schedules and each execution result.

CREATE TABLE IF NOT EXISTS automation_schedules (
    id VARCHAR(128) PRIMARY KEY,
    task_id VARCHAR(128) NOT NULL,
    task_label VARCHAR(255) NOT NULL,
    target_kind VARCHAR(64) NOT NULL DEFAULT 'all',
    target_key VARCHAR(255) NOT NULL DEFAULT '*',
    target_id VARCHAR(128),
    target_label VARCHAR(512),
    frequency VARCHAR(32) NOT NULL DEFAULT 'daily',
    start_time TIME NOT NULL,
    timezone_name VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_run_at TIMESTAMP WITH TIME ZONE,
    last_status VARCHAR(32),
    last_error TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_automation_schedules_task_target UNIQUE (task_id, target_key),
    CONSTRAINT ck_automation_schedules_frequency CHECK (frequency = 'daily')
);

CREATE INDEX IF NOT EXISTS ix_automation_schedules_due
    ON automation_schedules (active, next_run_at);

CREATE TABLE IF NOT EXISTS automation_executions (
    id VARCHAR(128) PRIMARY KEY,
    schedule_id VARCHAR(128) NOT NULL REFERENCES automation_schedules(id) ON DELETE CASCADE,
    scheduled_for TIMESTAMP WITH TIME ZONE NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    thread_id VARCHAR(256),
    turn_id VARCHAR(256),
    result_summary TEXT,
    error TEXT,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_automation_executions_schedule_time UNIQUE (schedule_id, scheduled_for),
    CONSTRAINT ck_automation_executions_status
        CHECK (status IN ('running', 'succeeded', 'failed', 'blocked'))
);

CREATE INDEX IF NOT EXISTS ix_automation_executions_schedule_started
    ON automation_executions (schedule_id, started_at);
