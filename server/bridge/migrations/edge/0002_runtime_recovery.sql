ALTER TABLE outbox ADD COLUMN blocked_at REAL;
ALTER TABLE outbox ADD COLUMN receipt_json TEXT;
CREATE TABLE edge_runtime_state (
    camera_id TEXT PRIMARY KEY,
    cooldown_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE edge_event_counters (
    run_id TEXT PRIMARY KEY,
    event_seq INTEGER NOT NULL
);
