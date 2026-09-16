-- Persist local Agent conversations independently from browser storage.

CREATE TABLE IF NOT EXISTS conversation_threads (
    id VARCHAR(128) PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_conversation_threads_updated_at
    ON conversation_threads (updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id VARCHAR(128) PRIMARY KEY,
    thread_id VARCHAR(128) NOT NULL
        REFERENCES conversation_threads(id) ON DELETE CASCADE,
    role VARCHAR(16) NOT NULL,
    body TEXT NOT NULL,
    task_id VARCHAR(128),
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_conversation_messages_thread_created
    ON conversation_messages (thread_id, created_at);
