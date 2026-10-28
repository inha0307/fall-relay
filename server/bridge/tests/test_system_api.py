#!/usr/bin/env python3
"""Checks for the /system figures: per-camera traffic and viewer counts, /api/system, and
names of disabled cameras for labelling old events. ASGI-level, temporary files only.
  python test_system_api.py
"""

from __future__ import annotations

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
from _pg import ENV, Schema
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = BRIDGE
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('fall_relay_server', HERE / 'video_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

os.environ['FALLRELAY_WRITE_TOKEN'] = 'w'
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 10_000 + b'\xff\xd9'


def check(name, condition, detail=None):
    if not condition:
        raise AssertionError(f'{name}: {detail}')
    print(f'PASS {name}')


def scope(method, path, query='', headers=()):
    return {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1',
        'method': method,
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode(),
        'query_string': query.encode(),
        'headers': list(headers) + proxy_token(method, path, headers),
        'client': ('127.0.0.1', 1),
        'server': ('127.0.0.1', 5001),
        'root_path': '',
    }


def proxy_token(method, path, headers):
    """Reads carry the server token, as the web server's proxy does."""
    if method == 'GET' and path.startswith('/api/') and not any(k == b'x-write-token' for k, _ in headers):
        return [(b'x-write-token', os.environ['FALLRELAY_WRITE_TOKEN'].encode())]
    return []


async def call(app, method, path, query='', body=b'', headers=()):
    messages, sent = [], False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        return {'type': 'http.disconnect'}

    async def send(message):
        messages.append(message)

    await app(scope(method, path, query, headers), receive, send)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    return start['status'], json.loads(
        b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body') or b'{}'
    )


async def stream(app, path, query, seconds):
    disconnect = asyncio.Event()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await disconnect.wait()
        return {'type': 'http.disconnect'}

    async def send(_message):
        pass

    task = asyncio.create_task(app(scope('GET', path, query), receive, send))
    await asyncio.sleep(seconds)
    disconnect.set()
    try:
        await asyncio.wait_for(task, 3)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


async def main(tmp: str) -> None:
    clip_dir = Path(tmp) / 'clips'
    clip_dir.mkdir()
    (clip_dir / 'fall_00000001_cam1_20260101_000000.webm').write_bytes(b'x' * 5000)
    config = {
        'database_url_env': ENV,
        'archive_dir': str(Path(tmp) / 'decision_logs'),
        'metrics_interval_s': 10.0,
        'metrics_retention_days': 30,
        'write_token_env': 'FALLRELAY_WRITE_TOKEN',
        'host': '127.0.0.1',
        'port': 5001,
        'cors_origins': [],
        'clip_dir': str(clip_dir),
        'clip_retention_days': 30,
        'edge_stale_after_s': 3.0,
        'video_stale_after_s': 3.0,
        'cameras': [{'id': '1', 'label': 'Room 1', 'location': '1F'}, {'id': '2', 'label': 'Old cam', 'location': ''}],
    }
    app = bridge.create_app(config)
    channel = app.state.channels.get('1')
    channel.sample_traffic()

    async def push(seconds):
        for _ in range(int(seconds * 20)):
            await call(app, 'POST', '/push_frame', 'cam=1', JPEG, [(b'content-type', b'image/jpeg')])
            await asyncio.sleep(0.05)

    pusher = asyncio.create_task(push(1.5))
    await asyncio.sleep(0.2)
    viewer = asyncio.create_task(stream(app, '/video_grid', 'cams=1&fps=10', 1.0))
    await asyncio.sleep(0.5)
    _, conn = await call(app, 'GET', '/api/connection/1')
    check('connection counts the live viewer', conn['stream_clients'] == 1, conn['stream_clients'])
    await viewer
    await pusher
    channel.sample_traffic()
    _, conn = await call(app, 'GET', '/api/connection/1')
    # ~20 frames/s x 10 KB in; ~10 frames/s x 10 KB out for one second of the window
    check('ingress bandwidth measured', conn['video_in_kbps'] > 500, conn['video_in_kbps'])
    check('egress bandwidth measured', conn['video_out_kbps'] > 100, conn['video_out_kbps'])
    check(
        'last frame age reported',
        conn['last_frame_age_s'] is not None and conn['last_frame_age_s'] < 2,
        conn['last_frame_age_s'],
    )
    check('viewer count drops after disconnect', conn['stream_clients'] == 0, conn['stream_clients'])

    _, system = await call(app, 'GET', '/api/system')
    for key in (
        'uptime_s',
        'cpu_pct',
        'rss_bytes',
        'threads',
        'db_bytes',
        'clip_count',
        'clip_bytes',
        'stream_clients',
        'video_in_kbps',
        'video_out_kbps',
        'camera_count',
    ):
        if key not in system:
            raise AssertionError(f'missing /api/system key {key}')
    check('system API reports process memory and DB size', system['rss_bytes'] > 0 and system['db_bytes'] > 0, system)
    check('system API totals per-camera traffic', system['video_in_kbps'] == conn['video_in_kbps'] + 0.0, system)

    code, _ = await call(
        app,
        'PATCH',
        '/api/admin/cameras/2',
        body=json.dumps({'enabled': False}).encode(),
        headers=[(b'content-type', b'application/json'), (b'x-write-token', b'w')],
    )
    _, plain = await call(app, 'GET', '/api/cameras')
    _, everything = await call(app, 'GET', '/api/cameras', 'include_disabled=true')
    check('disabled camera hidden by default', [c['id'] for c in plain['cameras']] == ['1'], plain)
    names = {c['id']: (c['label'], c['enabled']) for c in everything['cameras']}
    check(
        'include_disabled keeps names of disabled cameras',
        code == 200 and names == {'1': ('Room 1', True), '2': ('Old cam', False)},
        names,
    )
    app.state.registry.close()
    app.state.store.close()
    print('All system API checks passed.')


with tempfile.TemporaryDirectory() as tmp, Schema():
    asyncio.run(main(tmp))
