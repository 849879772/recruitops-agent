-- Title-first capture metadata. Legacy rows receive unknown capture state;
-- this migration does not infer capture evidence or availability history.
ALTER TABLE job_snapshots
    ADD COLUMN capture_status VARCHAR(16) NOT NULL DEFAULT 'unknown';

ALTER TABLE job_snapshots
    ADD COLUMN capture_failure_reason TEXT NOT NULL DEFAULT '';

ALTER TABLE job_snapshots
    ADD COLUMN availability_status VARCHAR(16) NOT NULL DEFAULT 'active';

ALTER TABLE job_snapshots
    ADD COLUMN title_key VARCHAR(512);

ALTER TABLE company_source_records
    ADD COLUMN company_id VARCHAR(255);

CREATE INDEX IF NOT EXISTS ix_job_snapshots_company_title_key
    ON job_snapshots (company_id, title_key);
CREATE INDEX IF NOT EXISTS ix_job_snapshots_availability_status
    ON job_snapshots (availability_status);
CREATE INDEX IF NOT EXISTS ix_company_source_records_company_id
    ON company_source_records (company_id);
