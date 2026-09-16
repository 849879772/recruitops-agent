-- Preserve stable conversational order even when multiple messages share a timestamp.

ALTER TABLE conversation_messages
    ADD COLUMN IF NOT EXISTS sequence_no INTEGER;

WITH ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY thread_id
            ORDER BY created_at, id
        ) AS assigned_sequence
    FROM conversation_messages
    WHERE sequence_no IS NULL
)
UPDATE conversation_messages AS message
SET sequence_no = ranked.assigned_sequence
FROM ranked
WHERE message.id = ranked.id;

ALTER TABLE conversation_messages
    ALTER COLUMN sequence_no SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_messages_thread_sequence
    ON conversation_messages (thread_id, sequence_no);
