#!/usr/bin/env python3
"""Checks for the camera registry and per-camera push tokens: seeding, admin API (write
token), live add/disable without restart, token issue/verify/clear, require_camera_tokens,
and persistence across restarts. Temporary files only. Run with the server venv:
  python test_camera_registry.py
"""

from __future__ import annotations

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
from edge_fixtures import heartbeat
import asyncio
import importlib.util
import json
import os
import sys
import tempfile

HERE = BRIDGE
sys.path.insert(0, str(HERE))
from _pg import ENV, Schema, url  # noqa: E402
from camera_registry import AUTH_INVALID, AUTH_MISSING, AUTH_OK, AUTH_OPEN, AUTH_UNKNOWN, CameraRegistry  # noqa: E402
from event_store import EventStore  # noqa: E402

spec = importlib.util.spec_from_file_location('fall_relay_server', HERE / 'video_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

WRITE = 'admin-write-token'
os.environ['FALLRELAY_WRITE_TOKEN'] = WRITE
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 32 + b'\xff\xd9'


def check(name, condition, detail=None):
    if not condition:
        raise AssertionError(f'{name}: {detail}')
    print(f'PASS {name}')


def config(require: bool = False) -> dict:
    return {
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
        'require_camera_tokens': require,
        'cameras': [{'id': '1', 'label': 'CAM 1', 'location': ''}, {'id': '2', 'label': 'CAM 2', 'location': 'Hall'}],
    }


async def call(app, method, path, *, query='', body=None, raw=None, headers=None):
    data = raw if raw is not None else (b'' if body is None else json.dumps(body).encode())
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if method == 'GET' and path.startswith('/api/') and not any(k == b'x-write-token' for k, _ in hdrs):
        hdrs.append((b'x-write-token', WRITE.encode()))  # reads carry the server token, as the web server's proxy does
    if data:
        hdrs.append((b'content-type', b'image/jpeg' if raw is not None else b'application/json'))
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1',
        'method': method,
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode(),
        'query_string': query.encode(),
        'headers': hdrs,
        'client': ('127.0.0.1', 1),
        'server': ('127.0.0.1', 5001),
        'root_path': '',
    }
    messages, sent = [], False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': data, 'more_body': False}
        return {'type': 'http.disconnect'}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    payload = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    return start['status'], json.loads(payload or b'{}')


def result(cam):
    return heartbeat(
        cam,
        threshold=0.932,
        confirm_slots=8,
        bbox=[1, 2, 3, 4],
        detector_score=0.9,
        visible_joints=12,
        mean_keypoint_confidence=0.8,
    )


ADMIN = {'X-Write-Token': WRITE}


async def bridge_checks() -> None:
    app = bridge.create_app(config())
    _, live = await call(app, 'GET', '/api/live')
    check('seeded cameras are live', [c['id'] for c in live['cameras']] == ['1', '2'], live)

    code, _ = await call(app, 'GET', '/api/admin/cameras', headers={'X-Write-Token': ''})
    check('admin list needs the write token', code == 403, code)
    code, _ = await call(app, 'POST', '/api/admin/cameras/1/token', headers={'X-Write-Token': 'wrong'})
    check('token issue needs the write token', code == 403, code)

    code, body = await call(app, 'POST', '/api/admin/cameras', body={'id': '3', 'label': 'Ward 3'}, headers=ADMIN)
    check('camera added', code == 201 and body['camera']['id'] == '3', body)
    code, _ = await call(app, 'POST', '/api/admin/cameras', body={'id': '3'}, headers=ADMIN)
    check('duplicate camera refused', code == 409, code)
    code, _ = await call(app, 'POST', '/api/admin/cameras', body={'id': 'bad id!'}, headers=ADMIN)
    check('invalid camera id refused', code == 400, code)
    code, _ = await call(app, 'POST', '/push_frame', query='cam=3', raw=JPEG)
    check('new camera accepts frames without restart', code == 200, code)
    _, live = await call(app, 'GET', '/api/live')
    check('new camera appears in live view', [c['id'] for c in live['cameras']] == ['1', '2', '3'])

    code, _ = await call(app, 'POST', '/push_frame', query='cam=1', raw=JPEG)
    _, conn = await call(app, 'GET', '/api/connection/1')
    check(
        'tokenless camera is accepted and marked unauthenticated',
        code == 200 and conn['push_auth'] == 'none',
        conn.get('push_auth'),
    )

    code, issued = await call(app, 'POST', '/api/admin/cameras/1/token', headers=ADMIN)
    token = issued.get('token', '')
    check('token issued once in plain text', code == 200 and len(token) >= 40, issued)
    _, listing = await call(app, 'GET', '/api/admin/cameras', headers=ADMIN)
    cam1 = next(c for c in listing['cameras'] if c['id'] == '1')
    check(
        'admin list shows token_set but never the token', cam1['token_set'] and token not in json.dumps(listing), cam1
    )

    code, _ = await call(app, 'POST', '/push_frame', query='cam=1', raw=JPEG)
    check('frame without token refused once a token exists', code == 403, code)
    code, _ = await call(app, 'POST', '/push_frame', query='cam=1', raw=JPEG, headers={'X-Camera-Token': 'nope'})
    check('frame with wrong token refused', code == 403, code)
    code, _ = await call(app, 'POST', '/api/edge_result', body=result('1'))
    check('edge result without token refused', code == 403, code)
    code, _ = await call(app, 'POST', '/push_frame', query='cam=1', raw=JPEG, headers={'X-Camera-Token': token})
    code2, _ = await call(app, 'POST', '/api/edge_result', body=result('1'), headers={'X-Camera-Token': token})
    check('frame and result with the token accepted', code == 200 and code2 == 200, (code, code2))
    _, conn = await call(app, 'GET', '/api/connection/1')
    check('connection reports token auth and rejects', conn['push_auth'] == 'token' and conn['auth_rejects'] == 3, conn)
    code, _ = await call(app, 'POST', '/push_frame', query='cam=2', raw=JPEG, headers={'X-Camera-Token': token})
    check("one camera's token does not open another camera", code == 200, code)  # cam 2 has no token: open
    _, issued2 = await call(app, 'POST', '/api/admin/cameras/2/token', headers=ADMIN)
    code, _ = await call(app, 'POST', '/push_frame', query='cam=2', raw=JPEG, headers={'X-Camera-Token': token})
    check("camera 1's token is rejected by camera 2", code == 403, code)

    code, _ = await call(app, 'PATCH', '/api/admin/cameras/2', body={'enabled': False}, headers=ADMIN)
    _, live = await call(app, 'GET', '/api/live')
    code2, _ = await call(
        app, 'POST', '/push_frame', query='cam=2', raw=JPEG, headers={'X-Camera-Token': issued2['token']}
    )
    check(
        'disabled camera leaves the live view and is refused',
        code == 200 and [c['id'] for c in live['cameras']] == ['1', '3'] and code2 == 400,
        (code, live['cameras'], code2),
    )
    code, body = await call(
        app, 'PATCH', '/api/admin/cameras/1', body={'label': 'Room 101', 'location': '1F'}, headers=ADMIN
    )
    _, live = await call(app, 'GET', '/api/live')
    check(
        'label and location edits reach the live view',
        live['cameras'][0]['label'] == 'Room 101' and live['cameras'][0]['location'] == '1F',
        live['cameras'][0],
    )
    code, _ = await call(app, 'PATCH', '/api/admin/cameras/1', body={'enabled': 'yes'}, headers=ADMIN)
    check('invalid update refused', code == 400, code)
    code, _ = await call(app, 'PATCH', '/api/admin/cameras/9', body={'label': 'x'}, headers=ADMIN)
    check('update of unknown camera is 404', code == 404, code)

    code, _ = await call(app, 'DELETE', '/api/admin/cameras/3/token', headers=ADMIN)
    check('clearing a token works', code == 200, code)
    app.state.registry.close()
    app.state.store.close()

    # restart on the same database: edits survive, the seed does not override them
    app = bridge.create_app(config())
    _, listing = await call(app, 'GET', '/api/admin/cameras', headers=ADMIN)
    by_id = {c['id']: c for c in listing['cameras']}
    check(
        'registry survives a restart without being re-seeded',
        by_id['1']['label'] == 'Room 101' and not by_id['2']['enabled'] and '3' in by_id and by_id['1']['token_set'],
        by_id,
    )
    code, _ = await call(app, 'POST', '/push_frame', query='cam=1', raw=JPEG, headers={'X-Camera-Token': token})
    check('issued token still valid after restart', code == 200, code)
    app.state.registry.close()
    app.state.store.close()

    app = bridge.create_app(config(require=True))
    code, _ = await call(app, 'POST', '/push_frame', query='cam=3', raw=JPEG)
    check('require_camera_tokens refuses tokenless cameras', code == 403, code)
    app.state.registry.close()
    app.state.store.close()


with Schema():
    events = EventStore(url())
    reg = CameraRegistry(
        [{'id': 'a', 'label': 'A'}, {'id': 'b', 'label': ''}], connection=events._db, lock=events._lock
    )
    check('seed fills default labels', [c['label'] for c in reg.list()] == ['A', 'CAM b'])
    check('tokenless camera verifies as open', reg.verify('a', None) == AUTH_OPEN)
    check('unknown camera verifies as unknown', reg.verify('zzz', None) == AUTH_UNKNOWN)
    tok = reg.issue_token('a')
    check(
        'token verification',
        reg.verify('a', tok) == AUTH_OK
        and reg.verify('a', tok + 'x') == AUTH_INVALID
        and reg.verify('a', '') == AUTH_MISSING,
    )
    reg.update('b', {'enabled': False})
    check('disabled camera verifies as unknown', reg.verify('b', None) == AUTH_UNKNOWN)
    reg.close()
    reg = CameraRegistry(
        [{'id': 'b', 'label': 'B again'}, {'id': 'c', 'label': 'C'}], connection=events._db, lock=events._lock
    )
    labels = {c['id']: (c['label'], c['enabled']) for c in reg.list()}
    check(
        're-seed only adds missing cameras',
        labels == {'a': ('A', True), 'b': ('CAM b', False), 'c': ('C', True)},
        labels,
    )
    reg.close()
    events.close()
with Schema():
    asyncio.run(bridge_checks())

print('All camera registry checks passed.')
