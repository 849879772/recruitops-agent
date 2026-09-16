-- Persist exact analysis reuse keys and model usage in Agent-owned PostgreSQL.

ALTER TABLE job_analysis_snapshots
    ADD COLUMN IF NOT EXISTS analysis_version VARCHAR(128),
    ADD COLUMN IF NOT EXISTS prompt_version VARCHAR(128),
    ADD COLUMN IF NOT EXISTS content_fingerprint VARCHAR(64),
    ADD COLUMN IF NOT EXISTS profile_fingerprint VARCHAR(64),
    ADD COLUMN IF NOT EXISTS input_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS output_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS filter_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS refusal_reason TEXT,
    ADD COLUMN IF NOT EXISTS error_code VARCHAR(128);

CREATE INDEX IF NOT EXISTS ix_job_analysis_snapshots_reuse
    ON job_analysis_snapshots (
        analysis_version,
        prompt_version,
        content_fingerprint,
        profile_fingerprint
    );
