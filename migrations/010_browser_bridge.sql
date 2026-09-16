-- Durable Edge active browser-bridge operation log and at-least-once outbox.

CREATE TABLE IF NOT EXISTS browser_operations (
    operation_id VARCHAR(128) PRIMARY KEY,
    idempotency_key VARCHAR(255) NOT NULL,
    operation VARCHAR(128) NOT NULL,
    device_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'CONNECTING',
    command JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error_code VARCHAR(128),
    last_event_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_event_sequence >= 0),
    last_outbox_sequence INTEGER,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_browser_operations_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT ck_browser_operations_status CHECK (
        status IN (
            'CONNECTING', 'DISPATCHED', 'NAVIGATING', 'WAITING_FOR_LOGIN',
            'EXTRACTING', 'VALIDATING', 'UPDATING', 'SUCCEEDED',
            'STATE_UNCLEAR', 'FAILED', 'CANCELLED'
        )
    )
);

CREATE INDEX IF NOT EXISTS ix_browser_operations_device_status
    ON browser_operations (device_id, status);
CREATE INDEX IF NOT EXISTS ix_browser_operations_updated_at
    ON browser_operations (updated_at);

CREATE TABLE IF NOT EXISTS browser_operation_events (
    event_id VARCHAR(128) PRIMARY KEY,
    operation_id VARCHAR(128) NOT NULL
        REFERENCES browser_operations (operation_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    status VARCHAR(32) NOT NULL,
    event_type VARCHAR(64) NOT NULL DEFAULT 'state',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_browser_operation_events_operation_sequence
        UNIQUE (operation_id, sequence),
    CONSTRAINT ck_browser_operation_events_status CHECK (
        status IN (
            'CONNECTING', 'DISPATCHED', 'NAVIGATING', 'WAITING_FOR_LOGIN',
            'EXTRACTING', 'VALIDATING', 'UPDATING', 'SUCCEEDED',
            'STATE_UNCLEAR', 'FAILED', 'CANCELLED'
        )
    )
);

CREATE INDEX IF NOT EXISTS ix_browser_operation_events_operation_sequence
    ON browser_operation_events (operation_id, sequence);

CREATE TABLE IF NOT EXISTS browser_outbox_cursors (
    device_id VARCHAR(128) PRIMARY KEY,
    next_sequence BIGINT NOT NULL DEFAULT 1 CHECK (next_sequence > 0)
);

CREATE TABLE IF NOT EXISTS browser_outbox (
    outbox_id VARCHAR(128) PRIMARY KEY,
    device_id VARCHAR(128) NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    operation_id VARCHAR(128) NOT NULL
        REFERENCES browser_operations (operation_id) ON DELETE CASCADE,
    message_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    ack_id VARCHAR(128),
    ack_payload JSONB,
    acked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_browser_outbox_device_sequence UNIQUE (device_id, sequence)
);

CREATE INDEX IF NOT EXISTS ix_browser_outbox_pending
    ON browser_outbox (device_id, acked_at, sequence);
CREATE INDEX IF NOT EXISTS ix_browser_outbox_operation
    ON browser_outbox (operation_id);
