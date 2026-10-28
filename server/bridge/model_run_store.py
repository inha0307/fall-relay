"""Persist immutable, edge-reported run identities (not remote attestation)."""

import json
import time
from model_contract import validate_identity
from db_backend import lock_key


class ModelRunStore:
    def __init__(self, events):
        self.db, self.lock = events._db, events._lock

    def observe(self, camera_id, item, now=None):
        item = validate_identity(item)
        now = time.time() if now is None else now
        encoded = json.dumps(item, sort_keys=True, allow_nan=False)
        with self.lock:
            lock_key(self.db, 'run:' + item['run_id'])
            old = self.db.execute(
                'SELECT camera_id,identity_json,last_seen FROM model_runs WHERE run_id=?', (item['run_id'],)
            ).fetchone()
            if old:
                if old['camera_id'] != camera_id or old['identity_json'] != encoded:
                    raise ValueError('run identity changed; a new runtime must use a new run_id')
                if now - old['last_seen'] >= 5:
                    self.db.execute('UPDATE model_runs SET last_seen=? WHERE run_id=?', (now, item['run_id']))
                    self.db.commit()
            else:
                self.db.execute(
                    'INSERT INTO model_runs VALUES (?,?,?,?,?,?,?,?)',
                    (
                        item['run_id'],
                        camera_id,
                        item['bundle_id'],
                        item['bundle_sha256'],
                        item['config_sha256'],
                        now,
                        now,
                        encoded,
                    ),
                )
                self.db.commit()
        return item['run_id']

    def resolve(self, camera_id, run_id):
        """Resolve a compact report using the durable, camera-bound registration."""
        if not isinstance(run_id, str):
            raise ValueError('invalid model_run_id')
        with self.lock:
            row = self.db.execute('SELECT camera_id,identity_json FROM model_runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            return None
        if row['camera_id'] != camera_id:
            raise ValueError('run identity belongs to another camera')
        return json.loads(row['identity_json'])

    def listing(self, limit=50):
        with self.lock:
            rows = self.db.execute('SELECT * FROM model_runs ORDER BY first_seen DESC LIMIT ?', (limit,)).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data['identity'] = json.loads(data.pop('identity_json'))
            result.append(data)
        return result
