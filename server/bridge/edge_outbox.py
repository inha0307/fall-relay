"""Durable event messages, ordered retries and atomic active-event recovery. Standard library only."""

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from edge_event_contract import CONTRACT, semantic_hash, validate_edge_event
from schema_migrations import scripts, statements

# Delivery context sent beside every message, outside the semantic hash (headers, not body).
AGE_HEADER = 'X-Fall-Relay-Event-Age-Ms'
BACKLOG_HEADER = 'X-Fall-Relay-Event-Backlog'
CLOCK_HEADER = 'X-Fall-Relay-Decided-Clock'  # 'synced' when the wall clock was synchronised at the event's open
# The WAL is fsynced by a background thread this often (seconds), so a commit never waits for the storage and a
# power loss costs at most the commits of the last interval plus the time the card took to sync (trial D3).
SYNC_INTERVAL_S = 1.0
CHECKPOINT_BYTES = 4 * 1024 * 1024  # the WAL size at which the sync thread checkpoints (SQLite's default: 1000 pages)


def wall_clock_synced():
    """1 when the kernel reports the wall clock synchronised (adjtimex: STA_UNSYNC clear, state not TIME_ERROR);
    0 when it is not or cannot be asked. Then a decided_at taken now stays meaningful after a reboot."""
    try:
        import ctypes

        class Timex(ctypes.Structure):
            _fields_ = [
                ('modes', ctypes.c_uint),
                ('offset', ctypes.c_long),
                ('freq', ctypes.c_long),
                ('maxerror', ctypes.c_long),
                ('esterror', ctypes.c_long),
                ('status', ctypes.c_int),
                ('_rest', ctypes.c_char * 512),
            ]

        tx = Timex()
        state = ctypes.CDLL(None, use_errno=True).adjtimex(ctypes.byref(tx))
        return int(state >= 0 and state != 5 and not tx.status & 0x40)
    except (AttributeError, OSError, ValueError):
        return 0


def boot_identity():
    """The kernel's boot identifier; a fresh value per process elsewhere (then ages survive restarts, not reboots)."""
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return uuid.uuid4().hex


def boot_clock():
    """Seconds since boot, including suspend; independent of the wall clock and of its calibration."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


class EdgeOutbox:
    def __init__(self, path, max_pending=10000, boot_id=None, boot_clock_fn=None, clock_synced_fn=None):
        self.boot_id = boot_id or boot_identity()
        self.boot_clock = boot_clock_fn or boot_clock
        self.clock_synced = clock_synced_fn or wall_clock_synced
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
            # NORMAL keeps commits off the storage: FULL stalled the inference loop for 9.7 s when the SD card held
            # an fsync (trial D1 pilot). Durability comes from the sync thread below, which fsyncs the WAL every
            # SYNC_INTERVAL_S; bare NORMAL lost the last ~30 s of commits in the power-loss trial (D3, cut 1).
            self.db.execute('PRAGMA synchronous=NORMAL')
            self.db.execute('PRAGMA busy_timeout=3000')
            # No automatic checkpoint: by default SQLite runs one (with its fsync) inside the commit that pushes the
            # WAL past 1000 pages, which would make that commit wait for the storage. The sync thread checkpoints.
            self.db.execute('PRAGMA wal_autocheckpoint=0')
            self._dirty = threading.Event()
            self._closing = threading.Event()
            self.last_sync = None
            self._migrate()
            # The sync thread's own connection, used only for checkpoints, so that it never holds `self.lock`
            # while the storage is busy.
            self._sync_db = sqlite3.connect(self.path, check_same_thread=False)
            self._sync_db.execute('PRAGMA busy_timeout=3000')
            self._syncer = threading.Thread(target=self._sync_loop, name='outbox-sync', daemon=True)
            self._syncer.start()
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
            self._commit()
        except Exception:
            self.db.rollback()
            raise

    def close(self):
        self._closing.set()
        if getattr(self, '_syncer', None) and self._syncer.is_alive() and threading.current_thread() is not self._syncer:
            self._syncer.join(timeout=10)
        self.sync_now()
        with self.lock:
            if getattr(self, '_sync_db', None):
                self._sync_db.close()
            self.db.close()
            self.guard.close()

    def _commit(self):
        self.db.commit()
        self._dirty.set()

    def _sync_loop(self):
        while not self._closing.wait(SYNC_INTERVAL_S):
            if self._dirty.is_set():
                self.sync_now()

    def sync_now(self):
        """fsync the WAL and the database file: what was committed before the call survives a power loss. When the
        WAL has grown past CHECKPOINT_BYTES, a passive checkpoint (which blocks no writer) folds it into the
        database first, on this thread, so that a commit never pays for a checkpoint."""
        self._dirty.clear()
        try:
            if os.path.getsize(str(self.path) + '-wal') > CHECKPOINT_BYTES and getattr(self, '_sync_db', None):
                self._sync_db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        except (OSError, sqlite3.Error):
            pass
        for suffix in ('-wal', ''):
            try:
                fd = os.open(str(self.path) + suffix, os.O_RDONLY)
            except OSError:
                continue
            try:
                os.fsync(fd)
            except OSError:
                self._dirty.set()
            finally:
                os.close(fd)
        self.last_sync = time.time()

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
                    # Where and when the event opened by the boot clock (the age sent with each of its messages),
                    # and whether the wall clock was synchronised then (so decided_at can be trusted after a reboot).
                    self.db.execute(
                        'INSERT INTO edge_event_origin(event_uid,boot_id,opened_boot_s,wall_synced) VALUES (?,?,?,?)',
                        (uid, self.boot_id, self.boot_clock(), int(bool(self.clock_synced()))),
                    )
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
                self._commit()
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
                self._commit()
                raise RuntimeError('stored event semantic hash mismatch')
            self.db.execute(
                'UPDATE outbox SET attempts=attempts+1 WHERE event_uid=? AND message_seq=?',
                (row['event_uid'], row['message_seq']),
            )
            self._commit()
            payload['attempt'] = row['attempts'] + 1
            payload['sent_at'] = now
            return payload

    def transport(self, payload, boot_now=None):
        """Headers for one send of `payload`: the event's age by the device's boot clock (omitted after a reboot,
        when it cannot be known) and how many later messages of the same event are still pending."""
        boot_now = self.boot_clock() if boot_now is None else boot_now
        with self.lock:
            origin = self.db.execute(
                'SELECT boot_id,opened_boot_s,wall_synced FROM edge_event_origin WHERE event_uid=?',
                (payload['event_uid'],),
            ).fetchone()
            backlog = self.db.execute(
                'SELECT COUNT(*) FROM outbox WHERE event_uid=? AND message_seq>? AND acked_at IS NULL',
                (payload['event_uid'], payload['message_seq']),
            ).fetchone()[0]
        headers = {BACKLOG_HEADER: str(backlog)}
        if origin and origin['boot_id'] == self.boot_id and boot_now >= origin['opened_boot_s']:
            headers[AGE_HEADER] = str(int((boot_now - origin['opened_boot_s']) * 1000))
        if origin and origin['wall_synced']:
            headers[CLOCK_HEADER] = 'synced'
        return headers

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
            self._commit()

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
            self._commit()

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
            'outbox_synced_ago_s': round(now - self.last_sync, 3) if self.last_sync else None,
            'outbox_unsynced': self._dirty.is_set(),
        }

    def prune(self, now):
        with self.lock:
            self.db.execute(
                'DELETE FROM outbox WHERE rowid IN (SELECT rowid FROM outbox WHERE acked_at IS NOT NULL AND acked_at<? LIMIT 1000)',
                (now - 7 * 86400,),
            )
            self.db.execute(
                'DELETE FROM edge_event_origin WHERE event_uid NOT IN (SELECT event_uid FROM outbox) AND event_uid NOT IN (SELECT event_uid FROM edge_active_events)'
            )
            self._commit()

    def retry_blocked(self):
        with self.lock:
            self.db.execute('UPDATE outbox SET blocked_at=NULL,next_attempt=0 WHERE acked_at IS NULL')
            self._commit()


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
