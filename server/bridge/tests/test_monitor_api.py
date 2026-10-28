#!/usr/bin/env python3
"""Checks for the /monitor APIs: /api/live, /video_grid (multiplexed cameras) and the
optional fps limit on /video_feed/<cam>. ASGI-level, no network. Run with the server venv:
  python test_monitor_api.py
"""

from __future__ import annotations

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import atexit
import tempfile
from _pg import ENV, Schema

_schema = Schema().__enter__()
atexit.register(_schema.__exit__, None, None, None)
import asyncio
import importlib.util
import json
import sys

HERE = BRIDGE
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('fall_relay_server', HERE / 'video_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

CONFIG = {
    'database_url_env': ENV,
    'archive_dir': tempfile.mkdtemp(prefix='fall-relay-archives-'),
    'metrics_interval_s': 10.0,
    'metrics_retention_days': 30,
    'write_token_env': 'FALLRELAY_WRITE_TOKEN',
    'host': '127.0.0.1',
    'port': 5001,
    'cors_origins': [],
    'clip_dir': None,
    'edge_stale_after_s': 3.0,
    'video_stale_after_s': 3.0,
    'cameras': [{'id': str(i), 'label': f'CAM {i}', 'location': ''} for i in range(1, 4)],
}
app = bridge.create_app(CONFIG)


def jpeg(tag: int) -> bytes:
    return b'\xff\xd8\xff\xe0' + str(tag).encode().rjust(16, b'0') + b'\xff\xd9'


def check(name, condition, detail=None):
    if not condition:
        raise AssertionError(f'{name}: {detail}')
    print(f'PASS {name}')


def scope(method: str, path: str, query: str = '', content_type: str | None = None) -> dict:
    return {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1',
        'method': method,
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode(),
        'query_string': query.encode(),
        'headers': [(b'content-type', content_type.encode())] if content_type else [],
        'client': ('127.0.0.1', 12345),
        'server': ('127.0.0.1', 5001),
        'root_path': '',
    }


async def call(method: str, path: str, query: str = '', body: bytes = b'', content_type: str = 'application/json'):
    messages, sent = [], False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        return {'type': 'http.disconnect'}

    async def send(message):
        messages.append(message)

    await app(scope(method, path, query, content_type if body else None), receive, send)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    return start['status'], b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')


async def stream_for(path: str, query: str, seconds: float) -> tuple[int, bytes]:
    """Read a streaming response for `seconds`, then disconnect like a closed browser tab."""
    chunks, status = [], None
    disconnect = asyncio.Event()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await disconnect.wait()
        return {'type': 'http.disconnect'}

    async def send(message):
        nonlocal status
        if message['type'] == 'http.response.start':
            status = message['status']
        elif message['type'] == 'http.response.body':
            chunks.append(message.get('body', b''))

    task = asyncio.create_task(app(scope('GET', path, query), receive, send))
    await asyncio.sleep(seconds)
    disconnect.set()
    try:
        await asyncio.wait_for(task, timeout=3)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
    return status, b''.join(chunks)


def parse_grid(data: bytes) -> list[tuple[str, bytes]]:
    parts, pos = [], 0
    while True:
        start = data.find(b'--frame\r\n', pos)
        if start < 0:
            break
        header_end = data.find(b'\r\n\r\n', start)
        if header_end < 0:
            break
        headers = dict(line.split(': ', 1) for line in data[start + 9 : header_end].decode().split('\r\n'))
        length = int(headers['Content-Length'])
        body = data[header_end + 4 : header_end + 4 + length]
        if len(body) < length:
            break
        parts.append((headers['X-Camera'], body))
        pos = header_end + 4 + length
    return parts


async def push(cams: list[str], seconds: float, hz: float = 20.0) -> None:
    for i in range(int(seconds * hz)):
        for cam in cams:
            await call('POST', '/push_frame', f'cam={cam}', jpeg(i), 'image/jpeg')
        await asyncio.sleep(1.0 / hz)


async def main() -> None:
    code, raw = await call('GET', '/api/live')
    live = json.loads(raw)
    check(
        'live lists every configured camera',
        code == 200 and [c['id'] for c in live['cameras']] == ['1', '2', '3'],
        live,
    )
    check(
        'live reports nothing connected yet', live['video_connected_count'] == 0 and live['edge_connected_count'] == 0
    )

    code, _ = await call('GET', '/video_grid', 'cams=1,9')
    check('grid rejects unknown camera', code == 404, code)
    code, _ = await call('GET', '/video_grid', 'cams=1,1')
    check('grid rejects duplicate cameras', code == 400, code)
    code, _ = await call('GET', '/video_grid', 'cams=' + ','.join(str(i) for i in range(1, 18)))
    check('grid rejects more than 16 cameras', code == 400, code)

    pusher = asyncio.create_task(push(['1', '2'], 2.5))
    await asyncio.sleep(0.3)
    status, data = await stream_for('/video_grid', 'cams=1,2,3&fps=5', 2.0)
    await pusher
    parts = parse_grid(data)
    per_cam = {cam: sum(1 for c, _ in parts if c == cam) for cam in ('1', '2', '3')}
    check('grid stream responds 200', status == 200, status)
    check(
        'grid parts are valid JPEGs with lengths',
        parts and all(b.startswith(b'\xff\xd8') and b.endswith(b'\xff\xd9') for _, b in parts),
    )
    check('grid sends about fps frames per live camera', all(7 <= per_cam[c] <= 12 for c in ('1', '2')), per_cam)
    check('grid skips a camera without video', per_cam['3'] == 0, per_cam)

    code, raw = await call('GET', '/api/live')
    live = json.loads(raw)
    check('live sees pushed video', live['video_connected_count'] == 2, live['video_connected_count'])

    pusher = asyncio.create_task(push(['1'], 2.5))
    await asyncio.sleep(0.3)
    status, data = await stream_for('/video_feed/1', 'fps=4', 2.0)
    await pusher
    frames = data.count(b'--frame\r\n')
    check('video_feed fps limit drops frames', status == 200 and 6 <= frames <= 10, frames)

    pusher = asyncio.create_task(push(['1'], 1.5))
    await asyncio.sleep(0.3)
    status, data = await stream_for('/video_feed/1', '', 1.0)
    await pusher
    frames = data.count(b'--frame\r\n')
    check('video_feed without fps keeps every frame', frames >= 15, frames)

    code, raw = await call('GET', '/api/cameras')
    clients = {c['id']: c['stream_clients'] for c in json.loads(raw)['cameras']}
    check('stream client counters return to zero after disconnect', clients == {'1': 0, '2': 0, '3': 0}, clients)
    print('All monitor API checks passed.')


if __name__ == '__main__':
    asyncio.run(main())
