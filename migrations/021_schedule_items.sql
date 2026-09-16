-- One item backs both the pending list and calendar. Preserve existing events.
ALTER TABLE schedule_event_snapshots ALTER COLUMN event_date DROP NOT NULL;
ALTER TABLE schedule_event_snapshots ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'pending';
ALTER TABLE schedule_event_snapshots ADD COLUMN time_kind VARCHAR(16) NOT NULL DEFAULT 'appointment';
ALTER TABLE schedule_event_snapshots ADD CONSTRAINT ck_schedule_item_status CHECK (status IN ('pending', 'completed', 'ignored'));
ALTER TABLE schedule_event_snapshots ADD CONSTRAINT ck_schedule_time_kind CHECK (time_kind IN ('appointment', 'deadline', 'unspecified'));
