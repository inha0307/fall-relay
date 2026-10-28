"""Isolated diagnostic tests; generated rows here are never performance measurements."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
from _pg import Schema
import asyncio
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import socket
import urllib.request
import uvicorn

import decision_log_runtime as runtime
from decision_log_runtime import SlotRecorder, LogAgent, atomic, read
from test_hardware import config, sample, call
from video_bridge import create_app


def recorder_checks(root):
    identity = {'run_id': 'a' * 32, 'settings': {'inference_hz': 10}}
    recorder = SlotRecorder(root, '1', identity)
    agent = LogAgent(root)
    assert not read(root / 'status.json')['recording']

    def command(id, name, params=None, expired=False):
        result = []
        t = threading.Thread(
            target=lambda: result.append(agent.execute(name, params or {}, id, time.time() + (-1 if expired else 60)))
        )
        t.start()
        while t.is_alive():
            recorder.next_poll = 0
            recorder.tick()
            time.sleep(0.01)
        return result[0]

    assert command(1, 'decision_log_start', {'duration_s': 30, 'auto_upload': False})[0]
    sid = recorder.meta['session_id']
    recorder.tick({'wall': time.time(), 'latency_ms': 1})
    assert recorder.meta['records'] == 1
    assert command(2, 'decision_log_collect')[0]
    assert recorder.meta['session_id'] != sid and read(root / sid / 'session.json')['options']['auto_upload']
    recorder.tick({'wall': time.time(), 'latency_ms': 2})
    assert command(3, 'decision_log_stop')[0] and not recorder.stream
    assert command(1, 'decision_log_start', {'duration_s': 30})[0] and not recorder.stream
    assert not command(4, 'decision_log_start', expired=True)[0] and not recorder.stream
    assert command(5, 'decision_log_start', {'duration_s': 1})[0]
    time.sleep(1.05)
    recorder.tick()
    assert not recorder.stream
    assert command(6, 'decision_log_start', {'duration_s': 30})[0]
    original = runtime.MAX_SESSION
    runtime.MAX_SESSION = 1
    recorder.tick({'wall': time.time()})
    runtime.MAX_SESSION = original
    assert not recorder.stream and recorder.meta['reason'] == 'size_limit'
    original_size = runtime.spool_size
    runtime.spool_size = lambda _: runtime.RECORDING_BUDGET
    assert not command(7, 'decision_log_start', {'duration_s': 30})[0] and not recorder.stream
    runtime.spool_size = original_size
    recorder.close()
    # Simulate abrupt process exit without a final metadata update.
    recorder = SlotRecorder(root, '1', identity)
    recorder.start({'duration_s': 30})
    sid = recorder.meta['session_id']
    recorder.tick({'wall': time.time()})
    recorder.stream.close()
    recorder.stream = None
    recorder.guard.close()
    recorder = SlotRecorder(root, '1', identity)
    assert read(root / sid / 'session.json')['records'] == 1 and not recorder.stream
    recorder.close()
    print(
        'PASS dynamic start/stop, snapshot continuation, duplicate/expired commands, duration and size limits, restart seals session and defaults off'
    )


async def request(app, method, path, data=b'', headers=None):
    headers = dict(headers or {})
    headers['Content-Length'] = str(len(data))
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1',
        'method': method,
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode(),
        'query_string': b'',
        'headers': [(k.lower().encode(), str(v).encode()) for k, v in headers.items()],
        'client': ('127.0.0.1', 1),
        'server': ('127.0.0.1', 5001),
        'root_path': '',
    }
    sent = False
    messages = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': data, 'more_body': False}
        return {'type': 'http.disconnect'}

    async def send(item):
        messages.append(item)

    try:
        await asyncio.wait_for(app(scope, receive, send), 5)
    except asyncio.TimeoutError as exc:
        raise AssertionError('ASGI request timed out: ' + method + ' ' + path) from exc
    start = next(m for m in messages if m['type'] == 'http.response.start')
    return start['status'], b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')


async def api_checks(root):
    cfg = config(root)
    cfg['require_camera_tokens'] = True
    app = create_app(cfg)
    token = app.state.registry.issue_token('1')
    device = {'X-Camera-Token': token, 'X-Log-Camera': '1'}
    admin = {'X-Write-Token': 'hardware-test-write', 'X-Actor': 'U-0001'}
    await call(
        app,
        'POST',
        '/api/device_health',
        body={**sample(), 'control': {'commands': ['decision_log_start', 'decision_log_stop', 'decision_log_collect']}},
        headers=device,
    )
    status, body = await call(
        app,
        'POST',
        '/api/devices/pi-test/commands',
        body={'command': 'decision_log_start', 'params': {'duration_s': 7201}},
        headers=admin,
    )
    assert status == 422, (status, body)
    status, body = await call(
        app,
        'POST',
        '/api/devices/pi-test/commands',
        body={'command': 'decision_log_start', 'params': {'duration_s': 30, 'auto_upload': True}},
        headers=admin,
    )
    assert status == 200, (status, body)
    sid = 'b' * 32
    run = 'a' * 32
    raw = (
        json.dumps({'session_id': sid, 'camera_id': '1', 'run_id': run, 'wall': time.time(), 'score': 0.5}) + '\n'
    ).encode()
    packed = gzip.compress(raw)
    meta = {
        'session_id': sid,
        'run_id': run,
        'device_id': 'pi-test',
        'camera_id': '1',
        'size': len(packed),
        'sha256': hashlib.sha256(packed).hexdigest(),
        'raw_bytes': len(raw),
        'records': 1,
        'started_at': time.time() - 1,
        'ended_at': time.time(),
    }

    def body(value=meta, blob=packed):
        return (json.dumps(value) + '\n').encode() + blob

    assert (await request(app, 'POST', '/api/decision_logs/upload', body(), {'X-Log-Camera': '1'}))[0] == 403
    assert (await request(app, 'POST', '/api/decision_logs/upload', body({**meta, 'device_id': 'another'}), device))[
        0
    ] == 422
    assert (await request(app, 'POST', '/api/decision_logs/upload', body({**meta, 'sha256': '0' * 64}), device))[
        0
    ] == 422
    status, value = await request(app, 'POST', '/api/decision_logs/upload', body(), device)
    assert status == 200, (status, value)
    status, value = await request(app, 'POST', '/api/decision_logs/upload', body(), device)
    assert status == 200 and json.loads(value)['duplicate'], value
    assert (await request(app, 'GET', '/api/decision_logs/pi-test'))[0] == 403
    status, value = await request(app, 'GET', '/api/decision_logs/pi-test', headers=admin)
    assert status == 200 and len(json.loads(value)['sessions']) == 1
    assert (await request(app, 'GET', f'/api/decision_logs/another/{sid}/download', headers=admin))[0] == 404
    status, value = await request(app, 'GET', f'/api/decision_logs/pi-test/{sid}/manifest', headers=admin)
    assert status == 200 and json.loads(value)['sha256'] == meta['sha256']
    assert not list((root / 'decision_logs').glob('*.part'))
    # Exercise FileResponse through a real HTTP connection, including streamed bytes.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    dropped = False

    async def lose_first_upload_ack(scope, receive, send):
        nonlocal dropped
        if scope['type'] == 'http' and scope['path'] == '/api/decision_logs/upload' and not dropped:
            dropped = True
            messages = []

            async def capture(message):
                messages.append(message)

            await app(scope, receive, capture)
            assert next(m for m in messages if m['type'] == 'http.response.start')['status'] == 200
            await send(
                {'type': 'http.response.start', 'status': 503, 'headers': [(b'content-type', b'application/json')]}
            )
            await send({'type': 'http.response.body', 'body': b'{"error":"test lost acknowledgement"}'})
        else:
            await app(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(lose_first_upload_ack, host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        until = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < until, 'test HTTP server failed to start'
            time.sleep(0.05)
        req = urllib.request.Request(f'http://127.0.0.1:{port}/api/decision_logs/pi-test/{sid}/download', headers=admin)
        with urllib.request.urlopen(req, timeout=10) as response:
            value = response.read()
            assert response.headers['X-Content-SHA256'] == meta['sha256']
        assert value == packed and gzip.decompress(value) == raw
        spool = root / 'local-spool'
        sid2 = 'c' * 32
        folder = spool / sid2
        folder.mkdir(parents=True)
        raw2 = (json.dumps({'session_id': sid2, 'run_id': run, 'camera_id': '1', 'wall': time.time()}) + '\n').encode()
        (folder / 'slots.jsonl').write_bytes(raw2)
        atomic(folder / 'session.json', {**meta, 'session_id': sid2, 'raw_bytes': len(raw2), 'records': 1})
        agent = LogAgent(spool)
        try:
            agent.upload(folder / 'session.json', f'http://127.0.0.1:{port}', 'pi-test', device, 1)
            raise AssertionError('lost ACK was accepted')
        except OSError:
            pass
        assert (folder / 'slots.jsonl').read_bytes() == raw2 and not (folder / 'upload.json').read_text().find(
            'ready'
        ) >= 0
        agent.upload(folder / 'session.json', f'http://127.0.0.1:{port}', 'pi-test', device, 2)
        assert read(folder / 'upload.json')['state'] == 'ready' and read(folder / 'upload.json')['attempts'] == 2
    finally:
        server.should_exit = True
        thread.join(10)
    print(
        'PASS command options, authentication/device binding, bad hash rejection, protected HTTP download, exact byte/row verification; agent retries after lost server ACK without deleting local data or creating duplicate archives'
    )


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as folder, Schema():
        recorder_checks(Path(folder))
    with tempfile.TemporaryDirectory() as folder, Schema():
        asyncio.run(api_checks(Path(folder)))
