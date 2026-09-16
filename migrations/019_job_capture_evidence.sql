ALTER TABLE job_snapshots
    ADD COLUMN IF NOT EXISTS capture_evidence JSON NOT NULL DEFAULT '{}';
