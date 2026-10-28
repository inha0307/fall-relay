"""Camera registry: the cameras the bridge accepts, editable at runtime.

Cameras live in the `cameras` table of the bridge database. The `cameras` list in
server_config.json is only a seed: ids missing from the table are inserted at start-up and
nothing is ever overwritten or deleted from it, so edits made in /system survive restarts.
Cameras are disabled rather than deleted because stored events refer to their ids.

Each camera can have a push token. Only its SHA-256 is stored; the token itself is returned
once when issued. A camera with a token must send it as `X-Camera-Token` with every frame and
result. A camera without a token is accepted unauthenticated unless `require_tokens` is set.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time

CAMERA_ID = re.compile(r'^[A-Za-z0-9_-]{1,32}$')
MAX_TEXT = 64

AUTH_OK = 'token'  # valid token
AUTH_OPEN = 'none'  # no token configured and tokens are not required
AUTH_MISSING = 'missing'  # a token is required but none was sent
AUTH_INVALID = 'invalid'  # wrong token
AUTH_UNKNOWN = 'unknown'  # camera not registered or disabled
REJECTED = (AUTH_MISSING, AUTH_INVALID, AUTH_UNKNOWN)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _text(value, name: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise ValueError(f'{name} must be a string of at most {MAX_TEXT} characters')
    return value.strip()


class CameraRegistry:
    def __init__(self, seed: list[dict], require_tokens: bool = False, *, connection, lock):
        """`connection` and `lock` are the event store's database facade and transaction scope."""
        self.require_tokens = bool(require_tokens)
        self._mutation_lock = threading.RLock()
        self._lock = lock
        self._db = connection
        self._cache: dict[str, dict] = {}
        with self._mutation_lock, self._lock:
            now = time.time()
            next_order = self._db.execute('SELECT COALESCE(MAX(sort_order), -1) + 1 FROM cameras').fetchone()[0]
            for camera in seed:
                cursor = self._db.execute(
                    'INSERT INTO cameras(id, label, location, sort_order, created_at, updated_at) '
                    'VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                    (
                        camera['id'],
                        camera.get('label') or f"CAM {camera['id']}",
                        camera.get('location') or '',
                        next_order,
                        now,
                        now,
                    ),
                )
                next_order += cursor.rowcount
            self._db.commit()
            self._reload()

    def close(self) -> None:
        """The connection belongs to the event store; nothing to release here."""

    def _reload(self) -> None:
        rows = self._db.execute('SELECT * FROM cameras ORDER BY sort_order, id').fetchall()
        self._cache = {row['id']: dict(row) for row in rows}

    @staticmethod
    def _public(row: dict) -> dict:
        return {
            'id': row['id'],
            'label': row['label'],
            'location': row['location'],
            'enabled': bool(row['enabled']),
            'sort_order': row['sort_order'],
            'token_set': row['token_hash'] is not None,
            'token_created_at': row['token_created_at'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
        }

    def list(self, include_disabled: bool = True) -> list[dict]:
        rows = list(self._cache.values())
        return [self._public(r) for r in rows if include_disabled or r['enabled']]

    def enabled(self) -> list[dict]:
        return self.list(include_disabled=False)

    def get(self, camera_id: str) -> dict | None:
        row = self._cache.get(camera_id)
        return self._public(row) if row else None

    def verify(self, camera_id: str, supplied: str | None) -> str:
        """Check a camera's push credentials (cheap: in-memory, constant-time compare)."""
        row = self._cache.get(camera_id)
        if row is None or not row['enabled']:
            return AUTH_UNKNOWN
        expected = row['token_hash']
        if expected is None:
            if self.require_tokens:
                return AUTH_MISSING
            return AUTH_OPEN
        if not supplied:
            return AUTH_MISSING
        return AUTH_OK if hmac.compare_digest(_hash(supplied), expected) else AUTH_INVALID

    def create(self, camera_id: str, label: str = '', location: str = '') -> dict:
        camera_id = str(camera_id)
        if not CAMERA_ID.match(camera_id):
            raise ValueError('id must be 1-32 letters, digits, _ or -')
        label = _text(label, 'label') or f'CAM {camera_id}'
        location = _text(location, 'location')
        now = time.time()
        with self._mutation_lock, self._lock:
            if camera_id in self._cache:
                raise KeyError(f'camera {camera_id} already exists')
            order = self._db.execute('SELECT COALESCE(MAX(sort_order), -1) + 1 FROM cameras').fetchone()[0]
            self._db.execute(
                'INSERT INTO cameras(id, label, location, sort_order, created_at, updated_at) VALUES (?,?,?,?,?,?)',
                (camera_id, label, location, order, now, now),
            )
            self._db.commit()
            self._reload()
            return self.get(camera_id)

    def update(self, camera_id: str, changes: dict) -> dict | None:
        fields = {}
        if 'label' in changes:
            fields['label'] = _text(changes['label'], 'label') or f'CAM {camera_id}'
        if 'location' in changes:
            fields['location'] = _text(changes['location'], 'location')
        if 'enabled' in changes:
            if not isinstance(changes['enabled'], bool):
                raise ValueError('enabled must be true or false')
            fields['enabled'] = int(changes['enabled'])
        if 'sort_order' in changes:
            if not isinstance(changes['sort_order'], int) or isinstance(changes['sort_order'], bool):
                raise ValueError('sort_order must be an integer')
            fields['sort_order'] = changes['sort_order']
        if not fields:
            raise ValueError('nothing to update (label, location, enabled, sort_order)')
        with self._mutation_lock, self._lock:
            if camera_id not in self._cache:
                return None
            assignments = ', '.join(f'{k} = ?' for k in fields)
            self._db.execute(
                f'UPDATE cameras SET {assignments}, updated_at = ? WHERE id = ?',
                (*fields.values(), time.time(), camera_id),
            )
            self._db.commit()
            self._reload()
            return self.get(camera_id)

    def issue_token(self, camera_id: str) -> str | None:
        """Create (or replace) the camera's push token. The plain token is returned only here."""
        token = secrets.token_urlsafe(32)
        with self._mutation_lock, self._lock:
            if camera_id not in self._cache:
                return None
            now = time.time()
            self._db.execute(
                'UPDATE cameras SET token_hash = ?, token_created_at = ?, updated_at = ? WHERE id = ?',
                (_hash(token), now, now, camera_id),
            )
            self._db.commit()
            self._reload()
        return token

    def clear_token(self, camera_id: str) -> bool:
        with self._mutation_lock, self._lock:
            if camera_id not in self._cache:
                return False
            self._db.execute(
                'UPDATE cameras SET token_hash = NULL, token_created_at = NULL, updated_at = ? WHERE id = ?',
                (time.time(), camera_id),
            )
            self._db.commit()
            self._reload()
        return True
