-- Persist Edge bridge presence without retaining authentication material.

CREATE TABLE IF NOT EXISTS browser_bridge_devices (
    device_id VARCHAR(128) PRIMARY KEY,
    connected BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMP NULL
);

CREATE INDEX IF NOT EXISTS ix_browser_bridge_devices_connected
    ON browser_bridge_devices (connected);
