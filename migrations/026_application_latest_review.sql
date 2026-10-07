-- One bounded latest receipt per application, independent of expendable diagnostics.
ALTER TABLE application_snapshots ADD COLUMN IF NOT EXISTS last_review JSON;
