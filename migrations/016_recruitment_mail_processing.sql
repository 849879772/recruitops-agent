-- Keep mailbox state and Agent processing state independent.
ALTER TABLE recruitment_emails
    ADD COLUMN IF NOT EXISTS mailbox_read BOOLEAN;

ALTER TABLE recruitment_mail_cursors
    ADD COLUMN IF NOT EXISTS uid_validity VARCHAR(128);
