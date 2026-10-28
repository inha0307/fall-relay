"""Explicit Pi events: atomic ordered receipts, one alert per episode, durable dispatch intent."""

import json
import time
from edge_event_contract import semantic_hash, validate_edge_event
from db_backend import has_table, lock_key
from notification_policy import classify


class EventConflict(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class EdgeEventStore:
    def __init__(self, events, max_live_age_s=120, delivery_enabled=False):
        self.db, self.lock = events._db, events._lock
        self.max_live_age_s = max_live_age_s
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

    def receive(self, payload, now=None):
        payload = validate_edge_event(payload)
        digest = semantic_hash(payload)
        now = time.time() if now is None else now
        camera, run, uid, seq = (payload[key] for key in ('camera_id', 'run_id', 'event_uid', 'message_seq'))
        corrected = payload['decided_at'] + payload['clock']['offset_s']
        usable = payload['clock']['rtt_s'] <= 2 and abs(payload['clock']['offset_s']) <= 86400 and corrected <= now + 2
        late = not usable or now - corrected > self.max_live_age_s
        mode, suppression_reason = classify(clock_usable=usable, late=late)
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
                    started = corrected if usable else now
                    cursor = self.db.execute(
                        '''INSERT INTO events(camera_id,track_id,started_at,max_score,model_run_id,event_uid,source,
                        run_id,edge_state,onset_sensor_ns,decided_sensor_ns,decided_at,clock_offset_s,clock_rtt_s,received_at,last_received_at,
                        last_message_seq,clock_usable,late_arrival) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
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
                            int(usable),
                            int(late),
                        ),
                    )
                    event_id = cursor.lastrowid
                    alert_id = self.db.execute(
                        "INSERT INTO alerts(event_id,type,camera_id,created_at,notification_mode,suppression_reason) VALUES (?,'fall',?,?,?,?)",
                        (event_id, camera, now, mode, suppression_reason),
                    ).lastrowid
                    detail = json.dumps(
                        {
                            'event_uid': uid,
                            'late_arrival': late,
                            'clock_usable': usable,
                            'decision_time': corrected if usable else None,
                            'delivery_enabled': bool(self.delivery_enabled and mode == 'live'),
                            'notification_mode': mode,
                            'suppression_reason': suppression_reason,
                        }
                    )
                    self.db.execute(
                        "INSERT INTO alert_log(alert_id,at,actor,action,to_state,detail) VALUES (?,?,'edge','created','pending',?)",
                        (alert_id, now, detail),
                    )
                    self.db.execute(
                        "INSERT INTO alert_dispatch_jobs(job_key,alert_id,escalation_level,channel,state,next_attempt_at,created_at,last_error) VALUES (?,?,0,'sse',?,?,?,?)",
                        (
                            f'alert:{alert_id}:sse:0',
                            alert_id,
                            'cancelled' if late else 'queued',
                            now,
                            now,
                            'late or uncalibrated event: manual review' if late else None,
                        ),
                    )
                    self.db.execute(
                        'INSERT INTO edge_clip_jobs VALUES (?,?,?,?,?,?)',
                        (
                            event_id,
                            camera,
                            started,
                            'unavailable' if late else 'queued',
                            'late or uncalibrated event' if late else None,
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
                    alert_id = self.db.execute(
                        "SELECT id FROM alerts WHERE event_id=? AND type='fall'", (event_id,)
                    ).fetchone()[0]
                    ended = (
                        max(event['started_at'], corrected if usable else now) if payload['kind'] == 'close' else None
                    )
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
                self.db.execute(
                    'INSERT INTO edge_event_receipts(event_uid,message_seq,kind,semantic_sha256,event_id,alert_id,received_at,payload) VALUES (?,?,?,?,?,?,?,?)',
                    (uid, seq, payload['kind'], digest, event_id, alert_id, now, json.dumps(payload, allow_nan=False)),
                )
                self.db.commit()
                return self.receipt(payload, event_id, alert_id, False)
            except Exception:
                self.db.rollback()
                raise

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
