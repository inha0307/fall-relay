CREATE TABLE outbox (
    event_uid TEXT NOT NULL,
    message_seq INTEGER NOT NULL CHECK (message_seq>=1),
    kind TEXT NOT NULL CHECK (kind IN ('open','update','close')),
    payload TEXT NOT NULL,
    semantic_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL,
    acked_at REAL,
    last_error TEXT,
    PRIMARY KEY (event_uid,message_seq)
);
CREATE INDEX ix_outbox_pending ON outbox(next_attempt) WHERE acked_at IS NULL;
CREATE TABLE edge_active_events (
    camera_id TEXT PRIMARY KEY,
    event_uid TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    last_message_seq INTEGER NOT NULL,
    decision_state TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
