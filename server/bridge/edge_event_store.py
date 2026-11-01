"""Explicit Pi events: atomic ordered receipts, one alert per episode, durable dispatch intent."""

import json
import time
from edge_event_contract import semantic_hash, validate_edge_event
from db_backend import has_table, lock_key
from notification_policy import (
    CLASSIFYING,
    HOLD_TIMEOUT,
    LIVE,
    MANUAL_REVIEW,
    SUPPRESSION,
    VERSION,
    classify,
    decide_now,
)


class EventConflict(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class EdgeEventStore:
    def __init__(
        self, events, max_live_age_s=120, delivery_enabled=False, stale_age_s=3600, hold_s=5, review_reminder_s=600
    ):
        self.db, self.lock = events._db, events._lock
        self.max_live_age_s = max_live_age_s
        self.stale_age_s = stale_age_s
        self.hold_s = hold_s
        self.review_reminder_s = review_reminder_s
        self.delivery_enabled = delivery_enabled
        self.has_models = has_table(self.db, 'model_runs')
        with self.lock:
            self.db.execute(
                "UPDATE edge_clip_jobs SET state='unavailable',error='server restarted during clip scheduling' WHERE state='claimed'"
            )
            self.db.commit()

    def bind_protocol(self, camera, run, protocol, now):
        lock_key(self.db, 'run:' + run)
        if self.has_models:
            bound = self.db.execute('SELECT camera_id FROM model_runs WHERE run_id=?', (run,)).fetchone()
            if bound and bound['camera_id'] != camera:
                raise EventConflict('run_camera_changed', 'run identity belongs to another camera')
        old = self.db.execute(
            'SELECT protocol FROM edge_protocol_runs WHERE camera_id=? AND run_id=?', (camera, run)
        ).fetchone()
        if old and old['protocol'] != protocol:
            raise EventConflict('protocol_changed', 'a runtime cannot switch event protocol')
        foreign = self.db.execute(
            'SELECT camera_id FROM edge_protocol_runs WHERE run_id=? AND camera_id<>?', (run, camera)
        ).fetchone()
        if foreign:
            raise EventConflict('run_camera_changed', 'run_id belongs to another camera')
        self.db.execute(
            'INSERT INTO edge_protocol_runs VALUES (?,?,?,?) ON CONFLICT(camera_id,run_id) DO NOTHING',
            (camera, run, protocol, now),
        )

    def heartbeat(self, camera, run, now=None):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.bind_protocol(camera, run, 'edge_event', now or time.time())
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    @staticmethod
    def receipt(payload, event_id, alert_id, duplicate):
        return {
            'ok': True,
            'event_uid': payload['event_uid'],
            'message_seq': payload['message_seq'],
            'event_id': event_id,
            'alert_id': alert_id,
            'duplicate': duplicate,
        }

    def receive(self, payload, now=None, transport=None):
        """Store one event message atomically and return its receipt.

        `transport` is the delivery context the device sends as headers (edge_outbox.transport): the event's age by
        the device's boot clock and how many later messages of the event are still pending. Both are optional; an
        older device sends neither."""
        payload = validate_edge_event(payload)
        digest = semantic_hash(payload)
        now = time.time() if now is None else now
        transport = transport or {}
        camera, run, uid, seq = (payload[key] for key in ('camera_id', 'run_id', 'event_uid', 'message_seq'))
        # The wall-clock calibration is kept for display; it no longer decides the notification mode.
        corrected = payload['decided_at'] + payload['clock']['offset_s']
        trusted = payload['clock']['rtt_s'] <= 2 and abs(payload['clock']['offset_s']) <= 86400 and corrected <= now + 2
        # The device vouches that its wall clock was synchronised when the event opened: decided_at is then right
        # even if the clock is unset now (resend after a reboot), and the send-time calibration is ignored.
        synced = bool(transport.get('decided_clock_synced')) and now - 30 * 86400 <= payload['decided_at'] <= now + 2
        if 'event_age_ms' in transport:
            age, age_source = transport['event_age_ms'] / 1000, 'device_boot'
        elif synced:
            corrected, trusted = payload['decided_at'], True
            age, age_source = max(0.0, now - corrected), 'decided_wall'
        elif trusted:
            age, age_source = max(0.0, now - corrected), 'wall_clock'
        else:
            age, age_source = None, None
        backlog = transport.get('event_backlog')
        late = age is None or age > self.max_live_age_s
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                lock_key(self.db, 'alert-log')
                self.bind_protocol(camera, run, 'edge_event', now)
                if self.has_models:
                    model = self.db.execute('SELECT identity_json FROM model_runs WHERE run_id=?', (run,)).fetchone()
                    if model:
                        declaration = json.loads(model[0]).get('event_decision')
                        if not declaration or payload['confirm'] != {
                            key: declaration['effective'][key] for key in ('k', 'n', 'm')
                        }:
                            raise EventConflict(
                                'decision_changed', 'event disagrees with declared run decision parameters'
                            )
                known = self.db.execute(
                    'SELECT * FROM edge_event_receipts WHERE event_uid=? AND message_seq=?', (uid, seq)
                ).fetchone()
                if known:
                    if known['semantic_sha256'] != digest:
                        raise EventConflict('message_changed', 'message sequence reused with different meaning')
                    result = self.receipt(payload, known['event_id'], known['alert_id'], True)
                    self.db.commit()
                    return result
                event = self.db.execute('SELECT * FROM events WHERE event_uid=?', (uid,)).fetchone()
                if payload['kind'] == 'open':
                    if event:
                        raise EventConflict('duplicate_open', 'event is already open; send its next sequence')
                    started = now - age if age is not None else now
                    if decide_now(age_s=age, backlog=backlog, late_threshold_s=self.max_live_age_s):
                        mode, reason = classify(latest_kind='open', close_reason=None, age_s=age)
                        classified_at, hold_until = now, None
                    else:
                        mode, reason, classified_at, hold_until = CLASSIFYING, None, None, now + self.hold_s
                    cursor = self.db.execute(
                        '''INSERT INTO events(camera_id,track_id,started_at,max_score,model_run_id,event_uid,source,
                        run_id,edge_state,onset_sensor_ns,decided_sensor_ns,decided_at,clock_offset_s,clock_rtt_s,received_at,last_received_at,
                        last_message_seq,clock_usable,late_arrival,event_age_on_arrival_ms,age_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (
                            camera,
                            payload['track_id'],
                            started,
                            payload['max_score'],
                            run,
                            uid,
                            'edge_event',
                            run,
                            payload['state'],
                            payload['onset_sensor_ns'],
                            payload['decided_sensor_ns'],
                            payload['decided_at'],
                            payload['clock']['offset_s'],
                            payload['clock']['rtt_s'],
                            now,
                            now,
                            seq,
                            int(trusted),
                            int(late),
                            None if age is None else int(round(age * 1000)),
                            age_source,
                        ),
                    )
                    event_id = cursor.lastrowid
                    alert_id = self.db.execute(
                        '''INSERT INTO alerts(event_id,type,camera_id,created_at,notification_mode,suppression_reason,
                        classification_reason,classified_at,hold_until,policy_version) VALUES (?,'fall',?,?,?,NULL,?,?,?,?)''',
                        (event_id, camera, now, mode, reason, classified_at, hold_until, VERSION),
                    ).lastrowid
                    detail = json.dumps(
                        {
                            'event_uid': uid,
                            'late_arrival': late,
                            'clock_usable': trusted,
                            'event_age_s': age,
                            'age_source': age_source,
                            'event_backlog': backlog,
                            'decision_time': started,
                            'delivery_enabled': bool(self.delivery_enabled and mode == LIVE),
                            'notification_mode': mode,
                            'classification_reason': reason,
                            'policy_version': VERSION,
                        }
                    )
                    self.db.execute(
                        "INSERT INTO alert_log(alert_id,at,actor,action,to_state,detail) VALUES (?,?,'edge','created','pending',?)",
                        (alert_id, now, detail),
                    )
                    # The browser stream carries classifying and live alerts alike; a manual-review decision cancels.
                    self.db.execute(
                        "INSERT INTO alert_dispatch_jobs(job_key,alert_id,escalation_level,channel,state,next_attempt_at,created_at) VALUES (?,?,0,'sse','queued',?,?)",
                        (f'alert:{alert_id}:sse:0', alert_id, now, now),
                    )
                    # Clip frames exist on the device only for a few seconds around a recent decision.
                    self.db.execute(
                        'INSERT INTO edge_clip_jobs VALUES (?,?,?,?,?,?)',
                        (
                            event_id,
                            camera,
                            started,
                            'unavailable' if late else 'queued',
                            'late or undated event' if late else None,
                            now,
                        ),
                    )
                else:
                    if not event:
                        raise EventConflict('sequence_gap', 'open must be received before later messages')
                    if (
                        event['camera_id'] != camera
                        or event['run_id'] != run
                        or event['track_id'] != payload['track_id']
                        or event['onset_sensor_ns'] != payload['onset_sensor_ns']
                    ):
                        raise EventConflict('identity_changed', 'event identity changed')
                    if event['ended_at'] is not None:
                        raise EventConflict('event_closed', 'closed event cannot be reopened')
                    if seq != event['last_message_seq'] + 1:
                        raise EventConflict('sequence_gap', 'event message sequence has a gap')
                    if (
                        payload['decided_sensor_ns'] < event['decided_sensor_ns']
                        or payload['max_score'] < event['max_score']
                    ):
                        raise EventConflict('event_regressed', 'event time or maximum score regressed')
                    original = self.db.execute(
                        'SELECT payload FROM edge_event_receipts WHERE event_uid=? AND message_seq=1', (uid,)
                    ).fetchone()
                    if original and json.loads(original[0])['confirm'] != payload['confirm']:
                        raise EventConflict('decision_changed', 'decision parameters changed inside the event')
                    event_id = event['id']
                    alert = self.db.execute(
                        "SELECT id,notification_mode FROM alerts WHERE event_id=? AND type='fall'", (event_id,)
                    ).fetchone()
                    alert_id = alert['id']
                    # End time: the corrected device clock when it is trusted; otherwise the event's duration by the
                    # sensor clock, which needs no calibration.
                    if payload['kind'] != 'close':
                        ended = None
                    elif trusted:
                        ended = max(event['started_at'], corrected)
                    else:
                        ended = event['started_at'] + max(0, payload['decided_sensor_ns'] - event['onset_sensor_ns']) / 1e9
                    self.db.execute(
                        '''UPDATE events SET edge_state=?,decided_sensor_ns=?,last_received_at=?,last_message_seq=?,max_score=?,ended_at=?,close_reason=? WHERE id=?''',
                        (
                            payload['state'],
                            payload['decided_sensor_ns'],
                            now,
                            seq,
                            payload['max_score'],
                            ended,
                            payload['close_reason'],
                            event_id,
                        ),
                    )
                    if alert['notification_mode'] == CLASSIFYING and (backlog == 0 or payload['kind'] == 'close'):
                        # This is the latest state the device holds for the event: classify from it.
                        if age_source == 'device_boot':
                            age_now = age
                        elif event['event_age_on_arrival_ms'] is not None:
                            age_now = event['event_age_on_arrival_ms'] / 1000 + max(0.0, now - event['received_at'])
                        else:
                            age_now = None
                        mode, reason = classify(
                            latest_kind=payload['kind'],
                            close_reason=payload['close_reason'],
                            age_s=age_now,
                            late_threshold_s=self.max_live_age_s,
                            stale_threshold_s=self.stale_age_s,
                        )
                        self._classify(alert_id, mode, reason, now, {'latest_kind': payload['kind'], 'close_reason': payload['close_reason'], 'event_age_s': age_now})
                self.db.execute(
                    'INSERT INTO edge_event_receipts(event_uid,message_seq,kind,semantic_sha256,event_id,alert_id,received_at,payload) VALUES (?,?,?,?,?,?,?,?)',
                    (uid, seq, payload['kind'], digest, event_id, alert_id, now, json.dumps(payload, allow_nan=False)),
                )
                self.db.commit()
                return self.receipt(payload, event_id, alert_id, False)
            except Exception:
                self.db.rollback()
                raise

    def _classify(self, alert_id, mode, reason, now, detail):
        """Settle a classifying alert (inside the caller's transaction). A settled alert is never changed again."""
        suppression = SUPPRESSION.get(reason) if mode == MANUAL_REVIEW else None
        changed = self.db.execute(
            "UPDATE alerts SET notification_mode=?,suppression_reason=?,classification_reason=?,classified_at=?,hold_until=NULL WHERE id=? AND notification_mode=?",
            (mode, suppression, reason, now, alert_id, CLASSIFYING),
        ).rowcount
        if not changed:
            return False
        self.db.execute(
            "INSERT INTO alert_log(alert_id,at,actor,action,detail) VALUES (?,?,'bridge','classified',?)",
            (alert_id, now, json.dumps({**detail, 'notification_mode': mode, 'classification_reason': reason, 'policy_version': VERSION})),
        )
        if mode == MANUAL_REVIEW:
            self.db.execute(
                "UPDATE alert_dispatch_jobs SET state='cancelled',last_error='manual review' WHERE alert_id=? AND state IN ('queued','processing','retry')",
                (alert_id,),
            )
        return True

    def tick(self, now=None):
        """Periodic work: settle holds that ran out (the latest state did not arrive) and send the one quiet
        reminder for manual-review alerts nobody has acknowledged. Safe to call every second and after a restart."""
        now = time.time() if now is None else now
        settled = reminded = 0
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                lock_key(self.db, 'alert-log')
                for row in self.db.execute(
                    "SELECT id FROM alerts WHERE notification_mode=? AND hold_until<=?", (CLASSIFYING, now)
                ).fetchall():
                    settled += self._classify(row['id'], LIVE, HOLD_TIMEOUT, now, {'hold_s': self.hold_s})
                if self.review_reminder_s > 0:
                    for row in self.db.execute(
                        '''SELECT id FROM alerts WHERE notification_mode=? AND state IN ('pending','sent','delivered')
                        AND review_reminded_at IS NULL AND classified_at IS NOT NULL AND classified_at+?<=?''',
                        (MANUAL_REVIEW, self.review_reminder_s, now),
                    ).fetchall():
                        self.db.execute('UPDATE alerts SET review_reminded_at=? WHERE id=?', (now, row['id']))
                        self.db.execute(
                            "INSERT INTO alert_log(alert_id,at,actor,action,detail) VALUES (?,?,'bridge','review_reminded',?)",
                            (row['id'], now, json.dumps({'after_s': self.review_reminder_s})),
                        )
                        reminded += 1
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return {'settled': settled, 'reminded': reminded}

    def event_for_clip(self, camera, run, onset_sensor_ns):
        """The stored explicit event a device clip belongs to (identity: camera, run, onset), or None."""
        with self.lock:
            row = self.db.execute(
                "SELECT id,started_at,track_id FROM events WHERE source='edge_event' AND camera_id=? AND run_id=? AND onset_sensor_ns=?",
                (camera, run, onset_sensor_ns),
            ).fetchone()
        return dict(row) if row else None

    def schedule_clips(self, recorder, labels, now=None):
        now = time.time() if now is None else now
        with self.lock:
            rows = [
                dict(row)
                for row in self.db.execute(
                    "SELECT j.*,e.track_id FROM edge_clip_jobs j JOIN events e ON e.id=j.event_id WHERE j.state='queued' ORDER BY j.event_id LIMIT 10"
                )
            ]
        for row in rows:
            with self.lock:
                changed = self.db.execute(
                    "UPDATE edge_clip_jobs SET state='claimed' WHERE event_id=? AND state='queued'", (row['event_id'],)
                ).rowcount
                self.db.commit()
            if not changed:
                continue
            accepted = False
            error = None
            try:
                if recorder and 0 <= now - row['trigger_at'] <= recorder.post_s:
                    accepted = recorder.trigger(
                        row['event_id'],
                        row['camera_id'],
                        row['trigger_at'],
                        row['track_id'],
                        labels.get(row['camera_id'], ''),
                    )
                if not accepted:
                    error = 'source clip buffer unavailable or clip time window elapsed'
            except Exception:
                error = 'clip scheduling failed'
            with self.lock:
                self.db.execute(
                    'UPDATE edge_clip_jobs SET state=?,error=? WHERE event_id=?',
                    ('started' if accepted else 'unavailable', error, row['event_id']),
                )
                self.db.commit()
