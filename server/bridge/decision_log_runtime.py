"""Bounded, optional slot diagnostics. File IPC is local to the device owner.

Inference owns recording; the telemetry agent owns compression and upload. No network I/O
or compression runs in the inference loop. Pending archives are never evicted for space.
"""

import fcntl
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.parse
import uuid

MAX_SESSION = 128 * 1024**2
MAX_SPOOL = 256 * 1024**2
MAX_PACKED = 64 * 1024**2
RECORDING_BUDGET = MAX_SPOOL - MAX_PACKED - 1024**2  # reserve compression space
RESERVE = 256 * 1024**2
ID = re.compile(r'^[a-f0-9]{32}$')


def atomic(path, value, durable=True):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('x') as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, allow_nan=False)
        stream.flush()
        if durable:
            os.fsync(stream.fileno())
    os.replace(temp, path)


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def options(value):
    if not isinstance(value, dict) or set(value) - {'duration_s', 'auto_upload'}:
        raise ValueError('duration_s and auto_upload are the supported options')
    duration = value.get('duration_s', 1800)
    if type(duration) is not int or not 1 <= duration <= 7200:
        raise ValueError('duration_s must be an integer between 1 and 7200')
    auto = value.get('auto_upload', True)
    if type(auto) is not bool:
        raise ValueError('auto_upload must be boolean')
    return {'duration_s': duration, 'auto_upload': auto}


def spool_size(root):
    total = 0
    for p in root.rglob('*'):
        try:
            if p.is_file():
                total += p.stat().st_size
        except FileNotFoundError:
            pass  # an uploaded archive can expire concurrently
    return total


class SlotRecorder:
    def __init__(self, root, camera, identity):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / 'requests').mkdir(exist_ok=True, mode=0o700)
        (self.root / 'replies').mkdir(exist_ok=True, mode=0o700)
        self.guard = (self.root / 'recorder.lock').open('a')
        fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.camera, self.identity = camera, identity
        self.stream = None
        self.meta = None
        self.error = None
        self.next_poll = 0
        # A process restart seals interrupted sessions but never resumes logging implicitly.
        for path in self.root.glob('*/session.json'):
            meta = read(path)
            if meta and meta.get('state') == 'recording':
                data = path.parent / 'slots.jsonl'
                if data.exists():
                    with data.open('rb') as stream:
                        meta['records'] = sum(1 for line in stream if line.endswith(b'\n'))
                    meta['raw_bytes'] = data.stat().st_size
                meta.update(state='closed', ended_at=time.time(), reason='runtime_restarted')
                atomic(path, meta)
        self.publish()

    def publish(self):
        public = {k: v for k, v in self.meta.items() if k != 'identity'} if self.meta else None
        atomic(
            self.root / 'status.json',
            {
                'recording': bool(self.stream),
                'session': public,
                'error': self.error,
                'updated_at': time.time(),
                'runtime_run_id': self.identity['run_id'],
                'limits': {'session_bytes': MAX_SESSION, 'spool_bytes': MAX_SPOOL, 'max_duration_s': 7200},
            },
            durable=False,
        )

    def start(self, params, parent=None):
        if self.stream:
            raise ValueError('already recording; stop or collect the current session first')
        params = options(params)
        if spool_size(self.root) >= RECORDING_BUDGET or shutil.disk_usage(self.root).free < RESERVE:
            raise ValueError('insufficient log storage; collect pending logs before starting')
        sid = uuid.uuid4().hex
        folder = self.root / sid
        folder.mkdir(mode=0o700)
        now = time.time()
        self.meta = {
            'session_id': sid,
            'camera_id': self.camera,
            'run_id': self.identity['run_id'],
            'identity': self.identity,
            'started_at': now,
            'ended_at': None,
            'state': 'recording',
            'options': params,
            'ends_at': now + params['duration_s'],
            'records': 0,
            'raw_bytes': 0,
            'parent_session_id': parent,
        }
        self.stream = (folder / 'slots.jsonl').open('a', buffering=1)
        os.chmod(folder / 'slots.jsonl', 0o600)
        self.deadline = time.monotonic() + params['duration_s']
        self.error = None
        atomic(folder / 'session.json', self.meta)
        self.publish()

    def finish(self, reason, collect=False):
        if not self.stream:
            return None
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        self.stream = None
        self.meta.update(state='closed', ended_at=time.time(), reason=reason)
        if collect:
            self.meta['options']['auto_upload'] = True
        atomic(self.root / self.meta['session_id'] / 'session.json', self.meta)
        self.publish()
        return self.meta['session_id']

    def tick(self, row=None):
        try:
            mono = time.monotonic()
            if self.stream and mono >= self.deadline:
                self.finish('duration_limit')
            if mono >= self.next_poll:
                self.next_poll = mono + 0.5
                if self.stream and (
                    spool_size(self.root) >= RECORDING_BUDGET or shutil.disk_usage(self.root).free < RESERVE
                ):
                    self.error = 'log storage limit reached'
                    self.finish('storage_limit')
                for path in sorted((self.root / 'requests').glob('*.json')):
                    reply = self.root / 'replies' / path.name
                    if reply.exists():
                        path.unlink()
                        continue
                    request = read(path, {})
                    ok = False
                    message = ''
                    try:
                        if time.time() > request.get('expires_at', 0):
                            raise ValueError('command expired')
                        name, params = request.get('command'), request.get('params', {})
                        if name == 'decision_log_start':
                            self.start(params)
                        elif name == 'decision_log_stop':
                            self.finish('operator_stop', params.get('collect', False))
                        elif name == 'decision_log_collect':
                            if self.stream:
                                remaining = max(1, int(self.deadline - mono))
                                auto = self.meta['options']['auto_upload']
                                sid = self.finish('collection_snapshot', True)
                                self.start({'duration_s': remaining, 'auto_upload': auto}, parent=sid)
                            else:
                                for f in self.root.glob('*/session.json'):
                                    value = read(f)
                                    if value and value['state'] == 'closed':
                                        value['options']['auto_upload'] = True
                                        atomic(f, value)
                        else:
                            raise ValueError('unsupported log command')
                        ok = True
                        message = 'applied'
                    except (ValueError, OSError) as exc:
                        message = str(exc)
                    atomic(reply, {'ok': ok, 'message': message, 'status': read(self.root / 'status.json')})
                    path.unlink(missing_ok=True)
                self.publish()
            if row is not None and self.stream:
                value = dict(
                    row, session_id=self.meta['session_id'], camera_id=self.camera, run_id=self.identity['run_id']
                )
                line = json.dumps(value, allow_nan=False) + '\n'
                size = len(line.encode())
                if self.meta['raw_bytes'] + size > MAX_SESSION:
                    self.finish('size_limit')
                    return
                self.stream.write(line)
                self.meta['records'] += 1
                self.meta['raw_bytes'] += size
        except (OSError, ValueError) as exc:
            # Optional diagnostics must not bring down fall detection.
            self.error = str(exc)[:200]
            if self.stream:
                try:
                    self.finish('write_error')
                except OSError:
                    self.stream.close()
                    self.stream = None

    def close(self):
        try:
            self.finish('runtime_stop')
        finally:
            self.guard.close()


class LogAgent:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stop = threading.Event()
        self.thread = None

    def status(self):
        value = read(self.root / 'status.json', {})
        value['stale'] = time.time() - value.get('updated_at', 0) > 10
        value['sessions'] = []
        paths = []
        for f in self.root.glob('*/session.json'):
            try:
                paths.append((f.stat().st_mtime, f))
            except FileNotFoundError:
                pass
        for _, f in sorted(paths, reverse=True)[:20]:
            meta = read(f, {})
            upload = read(f.parent / 'upload.json', {})
            if not meta:
                continue
            value['sessions'].append(
                {
                    k: meta.get(k)
                    for k in ('session_id', 'started_at', 'ended_at', 'state', 'records', 'raw_bytes', 'reason')
                }
                | {'upload': upload}
            )
        return value

    def execute(self, command, params, command_id, expires_at):
        requests = self.root / 'requests'
        requests.mkdir(exist_ok=True, mode=0o700)
        replies = self.root / 'replies'
        replies.mkdir(exist_ok=True, mode=0o700)
        name = str(int(command_id)) + '.json'
        cached = read(replies / name)
        if cached is not None:
            return cached['ok'], json.dumps(cached)
        atomic(requests / name, {'command': command, 'params': params, 'expires_at': min(expires_at, time.time() + 8)})
        until = time.monotonic() + 8
        while time.monotonic() < until:
            answer = read(replies / name)
            if answer is not None:
                return answer['ok'], json.dumps(answer)
            time.sleep(0.1)
        return False, 'runtime did not confirm the log command; check actual device log status'

    def run(self, server, device, headers):
        self.thread = threading.Thread(
            target=self._loop, args=(server, device, headers), daemon=True, name='decision-log-upload'
        )
        self.thread.start()

    def _loop(self, server, device, headers):
        while not self.stop.wait(5):
            replies = sorted((self.root / 'replies').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
            for index, path in enumerate(replies):
                if index >= 200 or time.time() - path.stat().st_mtime > 7 * 86400:
                    path.unlink(missing_ok=True)
            for path in self.root.glob('*/session.json'):
                try:
                    meta = read(path, {})
                    uploaded = read(path.parent / 'upload.json', {})
                    if uploaded.get('state') == 'ready':
                        if (
                            time.time() - uploaded['uploaded_at'] > 7 * 86400
                            or spool_size(self.root) > MAX_SPOOL - MAX_SESSION
                        ):
                            shutil.rmtree(path.parent)
                        continue
                    if meta.get('state') != 'closed' or not meta['options']['auto_upload']:
                        continue
                    if uploaded.get('next_attempt', 0) > time.time():
                        continue
                    self.upload(path, server, device, headers, uploaded.get('attempts', 0) + 1)
                except Exception as exc:
                    # Retry state survives telemetry agent restarts.
                    attempts = read(path.parent / 'upload.json', {}).get('attempts', 1)
                    try:
                        atomic(
                            path.parent / 'upload.json',
                            {
                                'state': 'failed',
                                'attempts': attempts,
                                'error': type(exc).__name__ + ': ' + str(exc)[:160],
                                'next_attempt': time.time() + min(300, 5 * 2 ** min(attempts, 6)),
                            },
                        )
                    except OSError:
                        pass

    def upload(self, path, server, device, headers, attempts):
        meta = read(path)
        folder = path.parent
        packed = folder / 'slots.jsonl.gz'
        atomic(folder / 'upload.json', {'state': 'uploading', 'attempts': attempts, 'started_at': time.time()})
        if not packed.exists():
            temp = folder / 'compressing.tmp'
            temp.unlink(missing_ok=True)  # interrupted compression is reproducible
            if (
                spool_size(self.root) + min(meta['raw_bytes'], MAX_PACKED) + 1024**2 > MAX_SPOOL
                or shutil.disk_usage(self.root).free < RESERVE
            ):
                raise OSError('insufficient compression space; collect or remove verified uploaded logs')
            try:
                with (folder / 'slots.jsonl').open('rb') as src, gzip.open(temp, 'wb', compresslevel=1) as dest:
                    for chunk in iter(lambda: src.read(128 * 1024), b''):
                        dest.write(chunk)
                        if temp.stat().st_size > MAX_PACKED:
                            raise OSError('compressed archive exceeds 64 MiB')
                if temp.stat().st_size > MAX_PACKED:
                    raise OSError('compressed archive exceeds 64 MiB')
                os.replace(temp, packed)
            finally:
                temp.unlink(missing_ok=True)
        digest = hashlib.sha256()
        with packed.open('rb') as src:
            for chunk in iter(lambda: src.read(256 * 1024), b''):
                digest.update(chunk)
        # JSON preamble avoids putting metadata or identities in URL/access logs.
        manifest = dict(meta, device_id=device, size=packed.stat().st_size, sha256=digest.hexdigest())
        preamble = (json.dumps(manifest, allow_nan=False) + '\n').encode()
        if len(preamble) > 32768:
            raise ValueError('log manifest too large')
        target = urllib.parse.urlsplit(server)
        cls = http.client.HTTPSConnection if target.scheme == 'https' else http.client.HTTPConnection
        connection = cls(target.hostname, target.port, timeout=60)
        try:
            connection.putrequest('POST', target.path.rstrip('/') + '/api/decision_logs/upload')
            for key, value in headers.items():
                if key.lower() != 'content-type':
                    connection.putheader(key, value)
            connection.putheader('Content-Type', 'application/octet-stream')
            connection.putheader('X-Log-Camera', meta['camera_id'])
            connection.putheader('Content-Length', str(len(preamble) + manifest['size']))
            connection.endheaders()
            connection.send(preamble)
            with packed.open('rb') as src:
                for chunk in iter(lambda: src.read(64 * 1024), b''):
                    if self.stop.is_set():
                        raise OSError('agent stopping')
                    start = time.monotonic()
                    connection.send(chunk)
                    self.stop.wait(max(0, len(chunk) / (512 * 1024) - (time.monotonic() - start)))
            response = connection.getresponse()
            result = json.loads(response.read(8192))
            if (
                response.status != 200
                or result.get('sha256') != manifest['sha256']
                or result.get('session_id') != meta['session_id']
            ):
                raise OSError(
                    'server did not confirm verified archive: '
                    + str(response.status)
                    + ' '
                    + str(result.get('error', ''))[:120]
                )
        finally:
            connection.close()
        atomic(
            folder / 'upload.json',
            {
                'state': 'ready',
                'attempts': attempts,
                'uploaded_at': time.time(),
                'sha256': manifest['sha256'],
                'size': manifest['size'],
            },
        )
