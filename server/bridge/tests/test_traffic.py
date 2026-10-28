#!/usr/bin/env python3
"""Checks for the per-camera ingress byte accounting (traffic.py): what is counted, by message kind, once per
request, only after the camera token is accepted, and how /api/dashboard turns it into kbit/s.
  python test_traffic.py
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
import traffic

os.environ['FALLRELAY_WRITE_TOKEN'] = 'w'
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 4000 + b'\xff\xd9'


def check(name, condition, detail=None):
    if not condition:
        raise AssertionError(f'{name}: {detail}')
    print(f'PASS {name}')


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

    await app(
        {
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
        },
        receive,
        send,
    )
    start = next(m for m in messages if m['type'] == 'http.response.start')
    return start['status'], json.loads(
        b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body') or b'{}'
    )


def headers(body, token=None, kind=b'application/json'):
    out = [(b'content-type', kind), (b'content-length', str(len(body)).encode())]
    return out + ([(b'x-camera-token', token)] if token else [])


async def main(tmp):
    config = {
        'database_url_env': ENV,
        'archive_dir': str(Path(tmp) / 'decision_logs'),
        'metrics_interval_s': 10.0,
        'metrics_retention_days': 30,
        'write_token_env': 'FALLRELAY_WRITE_TOKEN',
        'host': '127.0.0.1',
        'port': 5001,
        'cors_origins': [],
        'edge_stale_after_s': 3.0,
        'video_stale_after_s': 3.0,
        'require_camera_tokens': True,
        'cameras': [
            {
                'id': '1',
                'label': 'Room 1',
                'location': '',
            },
            {'id': '2', 'label': 'Room 2', 'location': ''},
        ],
    }
    app = bridge.create_app(config)
    secret = app.state.registry.issue_token('1').encode()
    for _ in range(3):
        await call(app, 'POST', '/push_frame', 'cam=1', JPEG, headers(JPEG, secret, b'image/jpeg'))
    refused, _ = await call(app, 'POST', '/push_frame', 'cam=1', JPEG, headers(JPEG, b'wrong', b'image/jpeg'))
    bad = json.dumps({'cam_id': '1', 'event_protocol': 'bogus'}).encode()  # accepted token, invalid result: still sent
    code, _ = await call(app, 'POST', '/api/edge_result', '', bad, headers(bad, secret))
    health = json.dumps({'camera_id': '1', 'device_id': 'pi'}).encode()
    await call(app, 'POST', '/api/device_health', '', health, headers(health, secret))
    rows = {(cam, kind): (n, nbytes) for cam, kind, n, nbytes in app.state.traffic.drain()}
    check('refused push is not counted', refused == 403 and rows[('1', 'video')][0] == 3, (refused, rows))
    check(
        'video bytes include the JPEG body and headers',
        3 * len(JPEG) < rows[('1', 'video')][1] < 3 * (len(JPEG) + 300),
        rows,
    )
    check(
        'a rejected result is counted once the token is accepted', code == 409 and rows[('1', 'result')][0] == 1, rows
    )
    check(
        'device telemetry counted as health',
        rows[('1', 'health')][0] == 1 and rows[('1', 'health')][1] > len(health),
        rows,
    )
    check('drain empties the meter', app.state.traffic.drain() == [], None)

    store = app.state.store
    for i in range(6):  # one minute observed for camera 1; camera 2 never reported
        store.record_metrics('1', 1000 + 10 * i, False, True, 0, 10, [], 10, 0)
    traffic.record(store._db, store._lock, [('1', 'result', 600, 75000), ('1', 'health', 6, 15000)], 1030, 10.0, 30)
    out = {c['camera_id']: c for c in traffic.summary(store._db, store._lock, 900, 2000, ['1', '2'], 10.0)}
    check(
        'kbit/s over the observed minute',
        out['1']['kbps'] == 12.0 and out['1']['by_kind']['result']['kbps'] == 10.0,
        out['1'],
    )
    check('camera without observation has no rate', out['2']['kbps'] is None and out['2']['bytes'] == 0, out['2'])
    from starlette.requests import Request

    request = Request(
        {
            'type': 'http',
            'method': 'POST',
            'path': '/api/edge_event',
            'query_string': b'',
            'headers': [(b'content-length', b'100')],
        }
    )
    meter = traffic.TrafficMeter()
    meter.count('1', request)
    meter.count('1', request)
    check('a request checked twice is counted once', [r[2] for r in meter.drain()] == [1], None)
    check(
        'unknown paths are not counted',
        traffic.kind_of('/api/cameras') is None and traffic.kind_of('/api/edge_event/context') == 'context',
        None,
    )
    hardware = app.state.hardware
    for i, (tx, rx) in enumerate([(1000, 0), (51000, 500), (101000, 1000), (40, 0), (5040, 100), (9e9, 0)]):
        t = 1000 + 5 * i if i < 5 else 2000  # the 4th sample follows a reboot, the last a long gap
        item = {
            'device_id': 'pi',
            'camera_id': '1',
            'interval_s': 5,
            'network': {'eth0': {'tx_bytes': int(tx), 'rx_bytes': rx}},
        }
        hardware.db.execute(
            'INSERT INTO hardware_samples(device_id,received_at,payload) VALUES (?,?,?)', ('pi', t, json.dumps(item))
        )
    net = traffic.device_network(hardware.db, hardware.lock, 0, 3000)
    check(
        'device send rate skips reboots and gaps',
        len(net) == 1 and net[0]['observed_s'] == 15 and net[0]['tx_bytes'] == 105000 and net[0]['tx_kbps'] == 56.0,
        net,
    )
    _, dash = await call(app, 'GET', '/api/dashboard')
    check(
        'dashboard carries per-camera traffic and device counters',
        [c['camera_id'] for c in dash['traffic']] == ['1', '2'] and 'network' in dash,
        dash.get('traffic'),
    )
    app.state.registry.close()
    store.close()
    print('All traffic checks passed.')


with tempfile.TemporaryDirectory() as tmp, Schema():
    asyncio.run(main(tmp))
