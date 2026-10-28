"""Durable event messages, ordered retries and atomic active-event recovery. Standard library only."""

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from edge_event_contract import CONTRACT, semantic_hash, validate_edge_event
from schema_migrations import scripts, statements


class EdgeOutbox:
    def __init__(self, path, max_pending=10000):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.guard = (self.path.with_suffix(self.path.suffix + '.lock')).open('a')
        os.chmod(self.guard.name, 0o600)
        try:
            fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.guard.close()
            raise RuntimeError('another runtime owns this outbox')
        self.lock = threading.RLock()
        self.max_pending = max_pending
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            self.path.chmod(0o600)
            self.db = sqlite3.connect(self.path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=NORMAL')
            self.db.execute('PRAGMA busy_timeout=3000')
            self._migrate()
        except Exception:
            if hasattr(self, 'db'):
                self.db.close()
            self.guard.close()
            raise

    def _migrate(self):
        available = scripts(Path(__file__).parent / 'migrations' / 'edge')
        exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='edge_schema_migrations'").fetchone()
        if not exists and self.db.execute("SELECT 1 FROM sqlite_master WHERE name='outbox'").fetchone():
            raise ValueError('unversioned outbox requires explicit migration review')
        applied = (
            {row[0]: row[1] for row in self.db.execute('SELECT version,checksum FROM edge_schema_migrations')}
            if exists
            else {}
        )
        if sorted(applied) != list(range(1, len(applied) + 1)):
            raise ValueError('edge migration ledger has gaps')
        known = {row[0]: row[2] for row in available}
        if any(known.get(version) != checksum for version, checksum in applied.items()):
            raise ValueError('edge migration checksum mismatch')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute(
                'CREATE TABLE IF NOT EXISTS edge_schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL)'
            )
            for version, _, checksum, sql in available:
                if version in applied:
                    continue
                for statement in statements(sql):
                    self.db.execute(statement)
                self.db.execute('INSERT INTO edge_schema_migrations VALUES (?,?)', (version, checksum))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def close(self):
        with self.lock:
            self.db.close()
            self.guard.close()

    def _put(self, payload, now):
        payload = validate_edge_event(payload)
        self.db.execute(
            'INSERT INTO outbox(event_uid,message_seq,kind,payload,semantic_sha256,created_at,next_attempt) VALUES (?,?,?,?,?,?,?)',
            (
                payload['event_uid'],
                payload['message_seq'],
                payload['kind'],
                json.dumps(payload, allow_nan=False),
                semantic_hash(payload),
                now,
                now,
            ),
        )

    def record(self, camera, run_id, transition, decision, clock, cooldown_s):
        now = transition['decided_at']
        kind = transition['kind']
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                active = self.db.execute('SELECT * FROM edge_active_events WHERE camera_id=?', (camera,)).fetchone()
                pending = self.db.execute('SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL').fetchone()[0]
                if pending >= self.max_pending and kind != 'close':
                    raise RuntimeError('outbox capacity reached; refusing to drop event data')
                if kind == 'open':
                    if active:
                        raise ValueError('an event is already active for this camera')
                    self.db.execute(
                        'INSERT INTO edge_event_counters VALUES (?,1) ON CONFLICT(run_id) DO UPDATE SET event_seq=event_seq+1',
                        (run_id,),
                    )
                    counter = self.db.execute(
                        'SELECT event_seq FROM edge_event_counters WHERE run_id=?', (run_id,)
                    ).fetchone()[0]
                    uid = f'{run_id}:{counter}'
                    seq = 1
                else:
                    if not active or active['run_id'] != run_id:
                        raise ValueError('active event identity changed')
                    uid = active['event_uid']
                    seq = active['last_message_seq'] + 1
                payload = {
                    'contract': CONTRACT,
                    'event_uid': uid,
                    'message_seq': seq,
                    'camera_id': camera,
                    'run_id': run_id,
                    'kind': kind,
                    'state': transition['state'],
                    'onset_sensor_ns': transition['onset_sensor_ns'],
                    'decided_sensor_ns': transition['sensor_ns'],
                    'decided_at': now,
                    'sent_at': now,
                    'clock': dict(clock),
                    'max_score': transition['max_score'],
                    'confirm': {key: decision[key] for key in ('k', 'n', 'm')},
                    'track_id': transition['track_id'],
                    'close_reason': transition['close_reason'],
                    'attempt': 1,
                }
                self._put(payload, now)
                if kind == 'close':
                    self.db.execute('DELETE FROM edge_active_events WHERE camera_id=?', (camera,))
                    self.db.execute(
                        'INSERT INTO edge_runtime_state VALUES (?,?) ON CONFLICT(camera_id) DO UPDATE SET cooldown_until=excluded.cooldown_until',
                        (camera, now + cooldown_s),
                    )
                else:
                    self.db.execute(
                        'INSERT INTO edge_active_events VALUES (?,?,?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET last_message_seq=excluded.last_message_seq,decision_state=excluded.decision_state,payload=excluded.payload,updated_at=excluded.updated_at',
                        (camera, uid, run_id, seq, transition['state'], json.dumps(payload, allow_nan=False), now),
                    )
                self.db.commit()
                return payload
            except Exception:
                self.db.rollback()
                raise

    def recover(self, camera, now, cooldown_s):
        """A new process closes the old run's episode, never adopts it under a new run UUID."""
        with self.lock:
            active = self.db.execute('SELECT payload FROM edge_active_events WHERE camera_id=?', (camera,)).fetchone()
            if active:
                previous = json.loads(active['payload'])
                transition = {
                    'kind': 'close',
                    'state': 'COOLDOWN',
                    'sensor_ns': previous['decided_sensor_ns'],
                    'decided_at': now,
                    'onset_sensor_ns': previous['onset_sensor_ns'],
                    'track_id': previous['track_id'],
                    'max_score': previous['max_score'],
                    'close_reason': 'runtime_stop',
                }
                self.record(camera, previous['run_id'], transition, previous['confirm'], previous['clock'], cooldown_s)
            row = self.db.execute(
                'SELECT cooldown_until FROM edge_runtime_state WHERE camera_id=?', (camera,)
            ).fetchone()
            return min(cooldown_s, max(0, row['cooldown_until'] - now)) if row else 0

    def next_due(self, now):
        with self.lock:
            row = self.db.execute(
                '''SELECT o.* FROM outbox o WHERE o.acked_at IS NULL AND o.blocked_at IS NULL AND o.next_attempt<=?
                AND NOT EXISTS(SELECT 1 FROM outbox p WHERE p.event_uid=o.event_uid AND p.message_seq<o.message_seq AND p.acked_at IS NULL)
                ORDER BY o.created_at,o.event_uid,o.message_seq LIMIT 1''',
                (now,),
            ).fetchone()
            if not row:
                return None
            payload = json.loads(row['payload'])
            if semantic_hash(payload) != row['semantic_sha256']:
                self.db.execute(
                    'UPDATE outbox SET blocked_at=?,last_error=? WHERE event_uid=? AND message_seq=?',
                    (now, 'stored event semantic hash mismatch', row['event_uid'], row['message_seq']),
                )
                self.db.commit()
                raise RuntimeError('stored event semantic hash mismatch')
            self.db.execute(
                'UPDATE outbox SET attempts=attempts+1 WHERE event_uid=? AND message_seq=?',
                (row['event_uid'], row['message_seq']),
            )
            self.db.commit()
            payload['attempt'] = row['attempts'] + 1
            payload['sent_at'] = now
            return payload

    def complete(self, payload, receipt, now):
        if (
            not isinstance(receipt, dict)
            or receipt.get('ok') is not True
            or receipt.get('event_uid') != payload['event_uid']
            or receipt.get('message_seq') != payload['message_seq']
            or any(type(receipt.get(key)) is not int or receipt[key] < 1 for key in ('event_id', 'alert_id'))
        ):
            raise ValueError('invalid event receipt; message retained')
        with self.lock:
            self.db.execute(
                'UPDATE outbox SET acked_at=?,last_error=NULL,receipt_json=? WHERE event_uid=? AND message_seq=?',
                (now, json.dumps(receipt), payload['event_uid'], payload['message_seq']),
            )
            self.db.commit()

    def fail(self, payload, now, error, permanent=False):
        delay = min(60, 2 ** min(6, max(0, payload['attempt'] - 1)))
        with self.lock:
            self.db.execute(
                'UPDATE outbox SET next_attempt=?,last_error=?,blocked_at=? WHERE event_uid=? AND message_seq=?',
                (
                    now + delay,
                    str(error)[:200],
                    now if permanent else None,
                    payload['event_uid'],
                    payload['message_seq'],
                ),
            )
            self.db.commit()

    def status(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            row = self.db.execute(
                'SELECT COUNT(*) pending,MIN(created_at) oldest,SUM(blocked_at IS NOT NULL) blocked FROM outbox WHERE acked_at IS NULL'
            ).fetchone()
            error = self.db.execute(
                'SELECT last_error FROM outbox WHERE acked_at IS NULL AND last_error IS NOT NULL ORDER BY created_at LIMIT 1'
            ).fetchone()
        return {
            'outbox_pending': row['pending'],
            'outbox_oldest_s': max(0, now - row['oldest']) if row['oldest'] is not None else 0,
            'outbox_blocked': row['blocked'] or 0,
            'outbox_error': error[0] if error else None,
        }

    def prune(self, now):
        with self.lock:
            self.db.execute(
                'DELETE FROM outbox WHERE rowid IN (SELECT rowid FROM outbox WHERE acked_at IS NOT NULL AND acked_at<? LIMIT 1000)',
                (now - 7 * 86400,),
            )
            self.db.commit()

    def retry_blocked(self):
        with self.lock:
            self.db.execute('UPDATE outbox SET blocked_at=NULL,next_attempt=0 WHERE acked_at IS NULL')
            self.db.commit()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--retry-blocked', action='store_true')
    args = parser.parse_args()
    box = EdgeOutbox(args.db)
    try:
        if args.retry_blocked:
            box.retry_blocked()
        print(json.dumps(box.status(), indent=2))
    finally:
        box.close()
