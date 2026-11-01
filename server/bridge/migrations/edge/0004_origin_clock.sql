-- Whether the device's wall clock was synchronised (kernel STA_UNSYNC clear) when the event opened. Sent with each
-- message of the event as X-Fall-Relay-Decided-Clock: synced, so that decided_at can be used as is after a reboot
-- that left the wall clock unset (the send-time calibration is then useless).
ALTER TABLE edge_event_origin ADD COLUMN wall_synced INTEGER NOT NULL DEFAULT 0;
