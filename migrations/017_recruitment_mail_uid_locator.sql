ALTER TABLE recruitment_emails
    ADD COLUMN IF NOT EXISTS imap_uid VARCHAR(64);

ALTER TABLE recruitment_emails
    ADD COLUMN IF NOT EXISTS uid_validity VARCHAR(128);
