-- Prevent one logical job from being reinserted under changing ATS identifiers.
-- Existing databases are deduplicated by scripts/migrate_job_identity.py first.

CREATE UNIQUE INDEX IF NOT EXISTS uq_job_snapshots_business_key
    ON job_snapshots (business_key)
    WHERE business_key IS NOT NULL;
