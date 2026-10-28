"""Durable scoped alert queries and atomic responder transitions."""

import json
import time
from db_backend import lock_key

FIELDS = """a.*,e.late_arrival,e.edge_state,e.clock_usable,
    CASE WHEN e.source='edge_event' AND e.clock_usable=1
        THEN e.decided_at+e.clock_offset_s ELSE NULL END AS event_decided_at,
    COALESCE(e.received_at,a.created_at) AS server_received_at"""


class AlertError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class AlertStore:
    def __init__(self, events):
        self.db, self.lock = events._db, events._lock

    @staticmethod
    def scope(cameras, prefix='a.'):
        if cameras is None:
            return '1=1', []
        if not cameras:
            return '0=1', []
        return prefix + 'camera_id IN (' + ','.join('?' for _ in cameras) + ')', list(cameras)

    def _get(self, id, cameras):
        clause, args = self.scope(cameras)
        row = self.db.execute(
            'SELECT ' + FIELDS + ' FROM alerts a LEFT JOIN events e ON e.id=a.event_id WHERE a.id=? AND ' + clause,
            [id, *args],
        ).fetchone()
        if row is None:
            raise AlertError(404, 'alert not found')
        return dict(row)

    def get(self, id, cameras):
        with self.lock:
            return self._get(id, cameras)

    def snapshot(self, cameras, live_only=False):
        with self.lock:
            self.db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            clause, args = self.scope(cameras)
            if live_only:
                clause += (
                    " AND a.notification_mode='live' AND COALESCE(e.late_arrival,0)=0 AND a.suppression_reason IS NULL"
                )
            rows = self.db.execute(
                'SELECT '
                + FIELDS
                + ' FROM alerts a LEFT JOIN events e ON e.id=a.event_id WHERE '
                + clause
                + " ORDER BY CASE WHEN a.state IN ('resolved','expired') THEN 2 WHEN a.notification_mode='live' THEN 0 ELSE 1 END,a.id DESC LIMIT 500",
                args,
            ).fetchall()
            cursor = self.db.execute('SELECT COALESCE(MAX(id),0) FROM alert_log').fetchone()[0]
            total = self.db.execute(
                'SELECT COUNT(*) FROM alerts a LEFT JOIN events e ON e.id=a.event_id WHERE ' + clause, args
            ).fetchone()[0]
            return {
                'alerts': [dict(r) for r in rows],
                'cursor': cursor,
                'limit': 500,
                'total': total,
                'truncated': total > 500,
            }

    def changes(self, cameras, after, live_only=False):
        with self.lock:
            clause, args = self.scope(cameras)
            if live_only:
                clause += (
                    " AND a.notification_mode='live' AND COALESCE(e.late_arrival,0)=0 AND a.suppression_reason IS NULL"
                )
            rows = self.db.execute(
                'SELECT l.id AS cursor,l.alert_id FROM alert_log l JOIN alerts a ON a.id=l.alert_id LEFT JOIN events e ON e.id=a.event_id WHERE l.id>? AND '
                + clause
                + ' ORDER BY l.id LIMIT 500',
                [after, *args],
            ).fetchall()
            return [{'cursor': r['cursor'], 'alert': self._get(r['alert_id'], cameras)} for r in rows]

    def delivery(self, id, cameras, actor, device):
        with self.lock:
            self._get(id, cameras)
            with self.db:
                lock_key(self.db, 'alert:' + str(id))
                row = self.db.execute(
                    "SELECT id FROM alert_deliveries WHERE alert_id=? AND recipient=? AND device_id=? AND channel='sse' ORDER BY id DESC LIMIT 1",
                    (id, actor, device),
                ).fetchone()
                if row:
                    return row[0]
                return self.db.execute(
                    "INSERT INTO alert_deliveries(alert_id,escalation_level,channel,recipient,device_id,attempted_at,result) VALUES (?,0,'sse',?,?,?,'broadcast')",
                    (id, actor, device, time.time()),
                ).lastrowid

    def transition(self, id, cameras, actor, action, body):
        now = time.time()
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                lock_key(self.db, 'alert-log')
                lock_key(self.db, 'alert:' + str(id))
                row = self._get(id, cameras)
                old = row['state']
                new = old
                if action == 'received':
                    device = body.get('device_id')
                    delivery = body.get('delivery_id')
                    if (
                        not isinstance(device, str)
                        or not device
                        or len(device) > 64
                        or not isinstance(delivery, int)
                        or isinstance(delivery, bool)
                        or delivery < 1
                    ):
                        raise AlertError(422, 'valid delivery and device required')
                    d = self.db.execute(
                        "SELECT * FROM alert_deliveries WHERE id=? AND alert_id=? AND recipient=? AND device_id=? AND channel='sse'",
                        (delivery, id, actor, device),
                    ).fetchone()
                    if not d:
                        raise AlertError(403, 'delivery does not belong to this actor and device')
                    self.db.execute(
                        'INSERT INTO alert_receipts(delivery_id,device_id,actor,received_at) VALUES (?,?,?,?) ON CONFLICT(delivery_id,device_id) DO NOTHING',
                        (delivery, device, actor, now),
                    )
                    self.db.execute(
                        'UPDATE alert_deliveries SET delivered_at=COALESCE(delivered_at,?) WHERE id=?', (now, delivery)
                    )
                    self.db.execute(
                        'UPDATE alerts SET first_delivered_at=COALESCE(first_delivered_at,?),first_sent_at=COALESCE(first_sent_at,?) WHERE id=?',
                        (now, d['attempted_at'], id),
                    )
                    self.db.execute(
                        "UPDATE alert_dispatch_jobs SET state='done',leased_until=NULL WHERE alert_id=? AND channel='sse' AND state IN ('queued','processing','retry')",
                        (id,),
                    )
                    if old in ('pending', 'sent'):
                        new = 'delivered'
                elif action == 'ack':
                    if old not in ('pending', 'sent', 'delivered'):
                        raise AlertError(
                            409,
                            'already confirmed by '
                            + str(row['acknowledged_by'] or row['resolved_by'] or 'another responder'),
                        )
                    new = 'acknowledged'
                    self.db.execute(
                        'UPDATE alerts SET acknowledged_at=?,acknowledged_by=? WHERE id=?', (now, actor, id)
                    )
                elif action == 'resolve':
                    if old != 'acknowledged':
                        raise AlertError(409, 'confirm the alert before resolving')
                    labels = (
                        ('fall', 'fa_sitting', 'fa_lying', 'fa_bending', 'fa_other', 'uncertain')
                        if row['type'] == 'fall'
                        else ('recovered', 'device_other', 'uncertain')
                    )
                    label = body.get('label')
                    note = body.get('note', '')
                    if label not in labels or not isinstance(note, str) or len(note) > 2000:
                        raise AlertError(422, 'invalid resolution or note')
                    new = 'resolved'
                    self.db.execute(
                        'UPDATE alerts SET resolved_at=?,resolved_by=?,resolution=?,note=? WHERE id=?',
                        (now, actor, label, note, id),
                    )
                    if row['event_id']:
                        self.db.execute(
                            'UPDATE events SET review_status=?,note=?,reviewed_at=?,reviewed_by=? WHERE id=?',
                            (label, note, now, actor, row['event_id']),
                        )
                else:
                    raise AlertError(422, 'invalid action')
                if new != old:
                    self.db.execute('UPDATE alerts SET state=? WHERE id=?', (new, id))
                    self.db.execute(
                        'INSERT INTO alert_log(alert_id,at,actor,action,from_state,to_state,detail) VALUES (?,?,?,?,?,?,?)',
                        (
                            id,
                            now,
                            actor,
                            {'received': 'delivered', 'ack': 'acknowledged', 'resolve': 'resolved'}[action],
                            old,
                            new,
                            json.dumps({'label': body.get('label'), 'note': body.get('note', '')}, ensure_ascii=False),
                        ),
                    )
                result = self._get(id, cameras)
                self.db.commit()
                return result
            except Exception:
                self.db.rollback()
                raise
