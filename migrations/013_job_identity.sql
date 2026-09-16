ALTER TABLE company_snapshots
    ADD COLUMN IF NOT EXISTS organization_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS recruitment_unit_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS source_identity VARCHAR(512);

ALTER TABLE job_snapshots
    ADD COLUMN IF NOT EXISTS organization_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS recruitment_unit_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS recruitment_campaign_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS source_platform VARCHAR(128),
    ADD COLUMN IF NOT EXISTS source_tenant VARCHAR(255),
    ADD COLUMN IF NOT EXISTS native_job_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS normalized_detail_url VARCHAR(2048),
    ADD COLUMN IF NOT EXISTS business_key VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_company_snapshots_organization
    ON company_snapshots (organization_id);
CREATE INDEX IF NOT EXISTS ix_job_snapshots_business_key
    ON job_snapshots (business_key);
CREATE INDEX IF NOT EXISTS ix_job_snapshots_organization_unit
    ON job_snapshots (organization_id, recruitment_unit_id);
