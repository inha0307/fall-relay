-- Notification policy version 2: classification from the event's latest state, with a short hold while the
-- device's remaining messages of a late event arrive. Existing rows keep their values; new columns are nullable.

-- Event age as the device reported it (boot clock) or as derived from the wall clock, and which it was.
ALTER TABLE events ADD COLUMN event_age_on_arrival_ms bigint;
ALTER TABLE events ADD COLUMN age_source text CHECK (age_source IN ('device_boot','decided_wall','wall_clock'));

-- The classification record of an alert.
ALTER TABLE alerts ADD COLUMN classification_reason text;
ALTER TABLE alerts ADD COLUMN classified_at double precision;
ALTER TABLE alerts ADD COLUMN hold_until double precision;
ALTER TABLE alerts ADD COLUMN policy_version bigint;
ALTER TABLE alerts ADD COLUMN review_reminded_at double precision;

-- notification_mode gains 'classifying'; manual review gains the version-2 suppression reasons.
DO $$
DECLARE name text;
BEGIN
    FOR name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'alerts'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%notification_mode%'
    LOOP
        EXECUTE format('ALTER TABLE alerts DROP CONSTRAINT %I', name);
    END LOOP;
    FOR name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'alert_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%action%'
    LOOP
        EXECUTE format('ALTER TABLE alert_log DROP CONSTRAINT %I', name);
    END LOOP;
END $$;

ALTER TABLE alerts ADD CONSTRAINT alerts_notification_mode_check
    CHECK (notification_mode IN ('classifying','live','manual_review'));
ALTER TABLE alerts ADD CONSTRAINT alerts_classification_check
    CHECK ((notification_mode IN ('classifying','live') AND suppression_reason IS NULL)
        OR (notification_mode = 'manual_review' AND suppression_reason IS NOT NULL
            AND suppression_reason IN ('event_too_old','clock_unreliable','late_recovered','stale_unknown')));
ALTER TABLE alert_log ADD CONSTRAINT alert_log_action_check
    CHECK (action IN ('created','classified','review_reminded','sent','delivered','acknowledged','resolved','escalated','expired','send_failed'));

CREATE INDEX ix_alerts_hold ON alerts(hold_until) WHERE notification_mode = 'classifying';
