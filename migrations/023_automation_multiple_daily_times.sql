-- Allow one task and target to run at multiple daily wall-clock times.
-- Existing schedules and execution history are preserved.

ALTER TABLE automation_schedules
    DROP CONSTRAINT IF EXISTS uq_automation_schedules_task_target;

ALTER TABLE automation_schedules
    ADD CONSTRAINT uq_automation_schedules_task_target_time
    UNIQUE (task_id, target_key, start_time, timezone_name);
