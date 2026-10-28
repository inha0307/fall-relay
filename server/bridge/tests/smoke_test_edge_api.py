#!/usr/bin/env python3
"""ASGI-level smoke test for the multi-camera Pi-to-server bridge (no network, no live Pi).

Checks per-camera isolation of decisions and video, the window-eligibility contract,
stale-result expiry, unknown-camera rejection and the camera listing API.
Run with the server virtual environment:  python smoke_test_edge_api.py
"""

from __future__ import annotations

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import atexit
import tempfile
from _pg import ENV, Schema

_schema = Schema().__enter__()
atexit.register(_schema.__exit__, None, None, None)
from edge_fixtures import event_messages, heartbeat
import asyncio
import importlib.util
import json
import sys

HERE = BRIDGE
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('fall_relay_server', HERE / 'video_bridge.py')
if spec is None or spec.loader is None:
    raise RuntimeError('Could not load video_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

import os

os.environ['FALLRELAY_WRITE_TOKEN'] = 'test-token'
STALE_S = 0.3
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
    'edge_stale_after_s': STALE_S,
    'video_stale_after_s': STALE_S,
    'cameras': [{'id': '1', 'label': 'CAM 1', 'location': ''}, {'id': '2', 'label': 'CAM 2', 'location': 'Hallway'}],
}
app = bridge.create_app(CONFIG)
TOKENS = {cam: {'X-Camera-Token': app.state.registry.issue_token(cam)} for cam in ('1', '2')}
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 64 + b'\xff\xd9'


async def request(
    method: str,
    path: str,
    *,
    query: str = '',
    json_body=None,
    raw: bytes | None = None,
    content_type: str = 'application/json',
    extra_headers: dict | None = None,
) -> tuple[int, dict]:
    body = raw if raw is not None else (b'' if json_body is None else json.dumps(json_body).encode())
    headers = [(b'content-type', content_type.encode())] if body else []
    headers += [(k.lower().encode(), v.encode()) for k, v in (extra_headers or {}).items()]
    if method == 'GET' and path.startswith('/api/') and not any(k == b'x-write-token' for k, _ in headers):
        headers.append(
            (b'x-write-token', os.environ['FALLRELAY_WRITE_TOKEN'].encode())
        )  # reads carry the server token, as the web server's proxy does
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1',
        'method': method,
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode(),
        'query_string': query.encode(),
        'headers': headers,
        'client': ('127.0.0.1', 12345),
        'server': ('127.0.0.1', 5001),
    }
    sent = False
    messages = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        return {'type': 'http.disconnect'}

    async def send(message):
        messages.append(message)

    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=5.0)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f'ASGI request timed out: {method} {path}; check worker-thread/event-loop execution restrictions'
        ) from exc
    start = next(m for m in messages if m['type'] == 'http.response.start')
    data = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    return start['status'], json.loads(data.decode() or '{}')


def result(cam: str, status: str, *, eligible: bool = True, score: float = 0.90, track: int = 17) -> dict:
    return heartbeat(
        cam,
        status,
        run=cam * 32,
        seq=1,
        score=score,
        threshold=0.932,
        consecutive_high=8 if status == 'FALL' else 0,
        confirm_slots=8,
        input_valid=True,
        window_valid_slots=20 if eligible else 7,
        window_min_valid_slots=10,
        window_eligible=eligible,
        people=1,
        track_id=track,
        bbox=[10, 20, 110, 220],
        detector_score=0.91,
        visible_joints=12,
        mean_keypoint_confidence=0.8,
        latency_ms=41.2,
    )


def check(label: str, condition: bool, detail=None) -> None:
    assert condition, (label, detail)
    print(f'PASS {label}')


async def main() -> None:
    code, body = await request(
        'POST', '/api/edge_result', json_body=result('1', 'NORMAL', eligible=False), extra_headers=TOKENS['1']
    )
    check('underfilled window is forced to WAIT', code == 200 and body['fall_status'] == 'WAIT', body)

    code, body = await request('POST', '/api/edge_result', json_body=result('1', 'NORMAL'), extra_headers=TOKENS['1'])
    check('Pi NORMAL decision is preserved on cam 1', body.get('fall_status') == 'NORMAL', body)

    code, body = await request(
        'POST', '/api/edge_result', json_body=result('2', 'FALL', score=0.97, track=3), extra_headers=TOKENS['2']
    )
    check('Pi FALL decision is preserved on cam 2', body.get('fall_status') == 'FALL', body)

    _, s1 = await request('GET', '/api/status/1')
    _, s2 = await request('GET', '/api/status/2')
    check(
        'cam 1 status holds only cam 1 decision',
        [t['fall_status'] for t in s1['tracks']] == ['NORMAL'] and s1['cam_id'] == '1',
        s1,
    )
    check(
        'cam 2 status holds only cam 2 decision',
        [t['fall_status'] for t in s2['tracks']] == ['FALL'] and s2['tracks'][0]['track_id'] == 3,
        s2,
    )

    code, body = await request('POST', '/api/edge_result', json_body=result('9', 'NORMAL'))
    check('unknown camera result is rejected', code == 400, body)
    code, body = await request(
        'POST', '/api/edge_result', json_body={**result('1', 'NORMAL'), 'score': 2}, extra_headers=TOKENS['1']
    )
    check('out-of-range score is rejected', code == 400, body)
    code, body = await request(
        'POST', '/api/edge_result', json_body={**result('1', 'NORMAL'), 'status': 'MAYBE'}, extra_headers=TOKENS['1']
    )
    check('invalid status is rejected', code == 400, body)

    code, body = await request(
        'POST',
        '/push_frame',
        query='cam=2&location=Room%202',
        raw=JPEG,
        content_type='image/jpeg',
        extra_headers=TOKENS['2'],
    )
    check('cam 2 frame accepted', code == 200 and body.get('ok') is True, body)
    code, body = await request('POST', '/push_frame', query='cam=9', raw=JPEG, content_type='image/jpeg')
    check('unknown camera frame is rejected', code == 400, body)
    code, body = await request(
        'POST', '/push_frame', query='cam=1', raw=b'not a jpeg', content_type='image/jpeg', extra_headers=TOKENS['1']
    )
    check('non-JPEG frame is rejected', code == 400, body)

    _, cams = await request('GET', '/api/cameras')
    by_id = {c['id']: c for c in cams['cameras']}
    check('camera list exposes both configured cameras', set(by_id) == {'1', '2'}, cams)
    check(
        'only cam 2 has live video',
        by_id['2']['video_connected'] and not by_id['1']['video_connected'] and cams['active'] == 1,
        cams,
    )
    check('frame location updates camera location', by_id['2']['location'] == 'Room 2', cams)

    _, conn = await request('GET', '/api/connection')
    check(
        'connection API reports both edges connected',
        conn['edge_connected_count'] == 2 and conn['cameras']['2']['last_edge_result']['status'] == 'FALL',
        conn,
    )
    check(
        'legacy top-level fields describe default camera',
        conn['camera_id'] == '1' and conn['fall_threshold'] == 0.932,
        conn,
    )
    code, _ = await request('GET', '/api/status/9')
    check('unknown camera status returns 404', code == 404)
    code, _ = await request('GET', '/api/live', extra_headers={'X-Write-Token': ''})
    check('reads without the server token are refused', code == 403)

    await asyncio.sleep(STALE_S + 0.1)
    _, s2 = await request('GET', '/api/status/2')
    _, c2 = await request('GET', '/api/connection/2')
    check(
        'stale FALL decision is no longer shown as a live track',
        s2['tracks'] == [] and not s2['edge_inference_connected'],
        s2,
    )
    check(
        'stale result is kept only as a flagged diagnostic',
        c2['last_edge_result_stale'] is True and not c2['edge_inference_connected'],
        c2,
    )

    _, ev = await request('GET', '/api/events')
    check('a FALL heartbeat alone creates no event', ev['total'] == 0, ev)
    code, receipt = await request(
        'POST', '/api/edge_event', json_body=event_messages('2', run='2' * 32)[0], extra_headers=TOKENS['2']
    )
    check('explicit open accepted', code == 200 and receipt['ok'], receipt)
    _, ev = await request('GET', '/api/events')
    check(
        'the open message created exactly one ongoing event',
        ev['total'] == 1 and ev['events'][0]['camera_id'] == '2' and ev['events'][0]['ongoing'],
        ev,
    )
    code, _ = await request(
        'PATCH',
        f"/api/events/{ev['events'][0]['id']}",
        json_body={'review_status': 'fall'},
        extra_headers={'X-Write-Token': 'test-token'},
    )
    check('research review labels are no longer editable through the bridge', code in (404, 405), code)
    code, _ = await request('GET', '/api/events', query='review_status=bogus')
    check('invalid review_status filter is rejected', code == 400)
    code, _ = await request('GET', '/api/metrics', query='hours=1')
    check('metrics API responds', code == 200)
    print('All bridge checks passed.')


if __name__ == '__main__':
    asyncio.run(main())
