"""Authenticated bounded diagnostic archives, separate from fall events and clips."""

import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import threading
import shutil

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

MAX_PACKED = 64 * 1024**2
MAX_RAW = 128 * 1024**2
MAX_SERVER = 5 * 1024**3
RETENTION = 30 * 86400
SID = re.compile(r'^[a-f0-9]{32}$')


class ArchiveError(ValueError):
    pass


class DecisionLogStore:
    def __init__(self, store, root):
        self.db, self.lock = store._db, store._lock
        self.file_lock = threading.RLock()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def prune(self):
        now = time.time()
        with self.file_lock, self.lock:
            for row in self.db.execute(
                "SELECT session_id,received_at FROM decision_log_archives WHERE state='ready' ORDER BY received_at"
            ).fetchall():
                used = sum(f.stat().st_size for f in self.root.glob('*.jsonl.gz'))
                if now - row['received_at'] < RETENTION and used <= MAX_SERVER:
                    continue
                (self.root / (row['session_id'] + '.jsonl.gz')).unlink(missing_ok=True)
                self.db.execute(
                    "UPDATE decision_log_archives SET state='expired' WHERE session_id=?", (row['session_id'],)
                )
            self.db.commit()
        for path in self.root.glob('*.part'):
            if now - path.stat().st_mtime > 7200:
                path.unlink(missing_ok=True)

    def accept(self, manifest, temp):
        sid = manifest['session_id']
        digest = hashlib.sha256()
        count = 0
        expanded = 0
        with open(temp, 'rb') as src:
            for chunk in iter(lambda: src.read(256 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != manifest['sha256']:
            raise ArchiveError('SHA-256 mismatch')
        try:
            with gzip.open(temp, 'rb') as src:
                while True:
                    line = src.readline(65537)
                    if not line:
                        break
                    expanded += len(line)
                    if len(line) > 65536 or expanded > MAX_RAW:
                        raise ArchiveError('expanded archive exceeds limit')
                    value = json.loads(line)
                    if (
                        not isinstance(value, dict)
                        or value.get('session_id') != sid
                        or value.get('run_id') != manifest['run_id']
                        or value.get('camera_id') != manifest['camera_id']
                    ):
                        raise ArchiveError('record identity mismatch')
                    count += 1
        except (OSError, EOFError, ValueError) as exc:
            raise ArchiveError('invalid gzip/JSONL archive: ' + str(exc)[:120]) from exc
        if count != manifest['records'] or expanded != manifest['raw_bytes']:
            raise ArchiveError('record count or raw byte count mismatch')
        self.prune()
        with self.file_lock, self.lock:
            old = self.db.execute('SELECT * FROM decision_log_archives WHERE session_id=?', (sid,)).fetchone()
            if old:
                if (
                    old['sha256'] != manifest['sha256']
                    or old['device_id'] != manifest['device_id']
                    or old['camera_id'] != manifest['camera_id']
                ):
                    raise ArchiveError('session identity or archive changed')
                if old['state'] == 'ready' and (self.root / (sid + '.jsonl.gz')).exists():
                    return {'ok': True, 'session_id': sid, 'sha256': old['sha256'], 'duplicate': True}
                raise ArchiveError('archive expired; do not reuse a session ID')
            used = sum(f.stat().st_size for f in self.root.glob('*.jsonl.gz'))
            if used + manifest['size'] > MAX_SERVER:
                raise ArchiveError('server archive quota reached')
            final = self.root / (sid + '.jsonl.gz')
            with open(temp, 'rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temp, final)
            os.chmod(final, 0o600)
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                self.db.execute(
                    'INSERT INTO decision_log_archives VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        sid,
                        manifest['device_id'],
                        manifest['camera_id'],
                        manifest['sha256'],
                        manifest['size'],
                        count,
                        manifest['started_at'],
                        manifest['ended_at'],
                        time.time(),
                        'ready',
                        json.dumps(manifest),
                    ),
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            return {'ok': True, 'session_id': sid, 'sha256': manifest['sha256'], 'duplicate': False}

    def listing(self, device, actor):
        self.prune()
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    'SELECT session_id,device_id,camera_id,size,records,started_at,ended_at,received_at,state,sha256 FROM decision_log_archives WHERE device_id=? ORDER BY received_at DESC LIMIT 100',
                    (device,),
                )
            ]


def decision_log_router(archives, authorize_push, channels, device_of, write_authorized, write_refused):
    router = APIRouter()
    active_uploads = set()

    def admin(request):
        if not write_authorized(request):
            return write_refused()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request.headers.get('x-actor', '')):
            return JSONResponse({'error': 'trusted administrator required'}, status_code=403)

    @router.post('/api/decision_logs/upload')
    async def upload(request: Request):
        # Authenticate before consuming an upload body.
        camera = request.headers.get('x-log-camera', '')
        channel = channels.get(camera)
        if channel is None:
            return JSONResponse({'error': 'unknown camera'}, status_code=404)
        refused = authorize_push(channel, request)
        if refused is not None:
            return refused
        length = request.headers.get('content-length', '')
        if not length.isdigit() or int(length) > MAX_PACKED + 32768:
            return JSONResponse({'error': 'upload too large'}, status_code=413)
        if len(active_uploads) >= 2:
            return JSONResponse({'error': 'upload concurrency limit; retry later'}, status_code=429)
        if shutil.disk_usage(archives.root).free < int(length) + 256 * 1024**2:
            return JSONResponse({'error': 'insufficient server space'}, status_code=507)
        fd, name = tempfile.mkstemp(suffix='.part', dir=archives.root)
        os.close(fd)
        active_uploads.add(name)
        manifest = None
        prefix = b''
        size = 0
        try:
            with open(name, 'wb') as stream:
                async for chunk in request.stream():
                    if manifest is None:
                        prefix += chunk
                        if b'\n' not in prefix:
                            if len(prefix) > 32768:
                                raise ArchiveError('manifest too large')
                            continue
                        header, chunk = prefix.split(b'\n', 1)
                        prefix = b''
                        if len(header) > 32768:
                            raise ArchiveError('manifest too large')
                        manifest = json.loads(header)
                        if not isinstance(manifest, dict) or not SID.fullmatch(str(manifest.get('session_id', ''))):
                            raise ArchiveError('invalid session')
                        device = await run_in_threadpool(device_of, str(manifest.get('device_id', '')))
                        if not device or device['camera_id'] != camera or manifest.get('camera_id') != camera:
                            raise ArchiveError('device/camera mismatch')
                        if not SID.fullmatch(str(manifest.get('run_id', ''))):
                            raise ArchiveError('invalid run')
                        if not re.fullmatch('[a-f0-9]{64}', str(manifest.get('sha256', ''))):
                            raise ArchiveError('invalid hash')
                        for key, maximum in [('size', MAX_PACKED), ('raw_bytes', MAX_RAW), ('records', 1000000)]:
                            if type(manifest.get(key)) is not int or not 0 <= manifest[key] <= maximum:
                                raise ArchiveError('invalid ' + key)
                        for key in ('started_at', 'ended_at'):
                            if (
                                isinstance(manifest.get(key), bool)
                                or not isinstance(manifest.get(key), (int, float))
                                or not math.isfinite(manifest[key])
                            ):
                                raise ArchiveError('invalid time')
                        if manifest['ended_at'] < manifest['started_at']:
                            raise ArchiveError('invalid time range')
                    size += len(chunk)
                    if size > MAX_PACKED:
                        raise ArchiveError('archive too large')
                    stream.write(chunk)
            if manifest is None or size != manifest['size']:
                raise ArchiveError('archive size mismatch')
            return await run_in_threadpool(archives.accept, manifest, name)
        except (ValueError, TypeError, KeyError) as exc:
            return JSONResponse({'error': str(exc)[:200]}, status_code=422)
        finally:
            Path(name).unlink(missing_ok=True)
            active_uploads.discard(name)

    @router.get('/api/decision_logs/{device}')
    def listing(device: str, request: Request):
        refused = admin(request)
        if refused is not None:
            return refused
        return {
            'sessions': archives.listing(device, request.headers['x-actor']),
            'retention_days': 30,
            'max_bytes': MAX_SERVER,
        }

    @router.get('/api/decision_logs/{device}/{session}/download')
    def download(device: str, session: str, request: Request):
        refused = admin(request)
        if refused is not None:
            return refused
        if not SID.fullmatch(session):
            return JSONResponse({'error': 'invalid session'}, status_code=404)
        archives.prune()
        with archives.lock:
            row = archives.db.execute(
                'SELECT * FROM decision_log_archives WHERE session_id=? AND device_id=?', (session, device)
            ).fetchone()
            if not row or row['state'] != 'ready':
                return JSONResponse({'error': 'archive unavailable or expired'}, status_code=404)
            path = archives.root / (session + '.jsonl.gz')
            if not path.exists():
                return JSONResponse({'error': 'archive file missing'}, status_code=404)
            archives.db.execute(
                'INSERT INTO decision_log_audit(session_id,actor,action,at) VALUES (?,?,?,?)',
                (session, request.headers['x-actor'], 'download', time.time()),
            )
            archives.db.commit()
        return FileResponse(
            path,
            media_type='application/gzip',
            filename=f'decision-log-{device}-{session}.jsonl.gz',
            headers={'X-Content-SHA256': row['sha256'], 'Cache-Control': 'no-store'},
        )

    @router.get('/api/decision_logs/{device}/{session}/manifest')
    def manifest(device: str, session: str, request: Request):
        refused = admin(request)
        if refused is not None:
            return refused
        with archives.lock:
            row = archives.db.execute(
                'SELECT manifest_json FROM decision_log_archives WHERE device_id=? AND session_id=?', (device, session)
            ).fetchone()
            if row:
                archives.db.execute(
                    'INSERT INTO decision_log_audit(session_id,actor,action,at) VALUES (?,?,?,?)',
                    (session, request.headers['x-actor'], 'manifest', time.time()),
                )
                archives.db.commit()
        return json.loads(row[0]) if row else JSONResponse({'error': 'unknown session'}, status_code=404)

    return router
