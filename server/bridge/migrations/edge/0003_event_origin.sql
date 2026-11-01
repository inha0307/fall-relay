CREATE TABLE edge_event_origin (
    event_uid TEXT PRIMARY KEY,
    boot_id TEXT NOT NULL,
    opened_boot_s REAL NOT NULL
);
