#!/usr/bin/env python3
"""Checks for fall clip recording: ring buffer, clip window, WebM output, retention, and the
bridge wiring (/push_frame -> explicit fall event -> /api/clips, /api/events clip_url, /clips/<file>).

Uses temporary directories and an in-memory DB only. Run with the server venv:
  python test_clip_recorder.py
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
import time
from pathlib import Path

import cv2
import numpy as np

HERE = BRIDGE
sys.path.insert(0, str(HERE))
from clip_recorder import ClipRecorder  # noqa: E402
from edge_fixtures import event_messages  # noqa: E402


def check(name, condition, detail=None):
    if not condition:
        raise AssertionError(f'{name}: {detail}')
    print(f'PASS {name}')


def frame(index: int) -> bytes:
    image = np.full((180, 320, 3), 40, np.uint8)
    cv2.putText(image, str(index), (20, 120), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 5)
    return cv2.imencode('.jpg', image)[1].tobytes()


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def count_frames(path: Path) -> int:
    capture = cv2.VideoCapture(str(path))
    count = 0
    while capture.read()[0]:
        count += 1
    capture.release()
    return count


with tempfile.TemporaryDirectory() as tmp, Schema():
    rec = ClipRecorder(Path(tmp) / 'clips', pre_s=2.0, post_s=0.3, fps=10.0, retention_days=1)
    check('recorder enabled with OpenCV', rec.enabled, rec.error)
    t0 = time.time() - 10.0
    for i in range(300):  # 30 fps input for 10 s
        rec.add_frame('1', frame(i), t0 + i / 30)
    buffered = len(rec._buffers['1'])
    check('ring buffer is sampled to clip fps and bounded in time', 70 <= buffered <= 75, buffered)

    fall_t = t0 + 9.0
    check('trigger accepted', rec.trigger(7, '1', fall_t, 3, 'CAM 1'))
    check('clip reports recording while collecting', rec.clip_status(7) == 'recording')
    check('clip finished', wait_for(lambda: rec.clip_status(7) == 'ready'), rec.clip_status(7))
    name = rec.clip_name(7)
    check(
        'clip file name carries event and camera',
        name.startswith('fall_00000007_cam1_') and name.endswith('.webm'),
        name,
    )
    sidecar = json.loads((Path(tmp) / 'clips' / name.replace('.webm', '.json')).read_text())
    check(
        'sidecar links the event',
        sidecar['event_id'] == 7 and sidecar['camera_no'] == '1' and sidecar['track_id'] == 3,
        sidecar,
    )
    # 2 s before + 0.3 s after at 10 fps (the buffer already holds frames after fall_t).
    check('clip covers pre and post window', 20 <= sidecar['source_frames'] <= 25, sidecar['source_frames'])
    decoded = count_frames(Path(tmp) / 'clips' / name)
    check('WebM decodes with real-time frame count', 20 <= decoded <= 26, decoded)
    check('no temporary files left', not list((Path(tmp) / 'clips').glob('*.tmp*')))

    check('trigger without pushed video is refused (device uploads its own clip)', not rec.trigger(8, '9', fall_t))
    check('refused clip is reported as none', rec.clip_status(8) is None and rec.clip_name(8) is None)

    # skeleton-only device: the uploaded window (5 fps here) becomes the clip
    ok, jpeg = cv2.imencode('.jpg', np.full((360, 640, 3), 90, np.uint8))
    device = [(fall_t - 3 + i * 0.2, jpeg.tobytes()) for i in range(25)]
    check('device clip accepted', rec.submit(9, '2', fall_t, device, 1, 'CAM 2'))
    check('second submit while encoding is refused', not rec.submit(9, '2', fall_t, device))
    check('device clip saved', wait_for(lambda: rec.clip_status(9) == 'ready'), rec.clip_status(9))
    meta = json.loads((Path(tmp) / 'clips' / rec.clip_name(9).replace('.webm', '.json')).read_text())
    check('device clip keeps every uploaded frame', meta['source_frames'] == 25, meta['source_frames'])
    check(
        'device clip plays in real time at the recorder fps',
        45 <= count_frames(Path(tmp) / 'clips' / rec.clip_name(9)) <= 55,
    )

    old = Path(tmp) / 'clips' / 'fall_00000001_cam1_20200101_000000.webm'
    old.write_bytes(b'x')
    os.utime(old, (time.time() - 3 * 86400,) * 2)
    keep = Path(tmp) / 'clips' / 'notes.txt'
    keep.write_text('not a clip')
    removed = rec.prune()
    check(
        'retention removes only expired clip files',
        removed == 1 and not old.exists() and keep.exists() and rec.clip_name(7),
    )
    rec.close()

spec = importlib.util.spec_from_file_location('fall_relay_server', HERE / 'video_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


async def call(
    app,
    method: str,
    path: str,
    *,
    query: str = '',
    body: bytes = b'',
    content_type: str = 'application/json',
    token: str | None = None,
):
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1',
        'method': method,
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode(),
        'query_string': query.encode(),
        'headers': ([(b'content-type', content_type.encode())] if body else [])
        + ([(b'x-camera-token', token.encode())] if token else []),
        'client': ('127.0.0.1', 12345),
        'server': ('127.0.0.1', 5001),
        'root_path': '',
    }
    messages, sent = [], False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        return {'type': 'http.disconnect'}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    headers = {k.decode().lower(): v.decode() for k, v in start['headers']}
    return start['status'], headers, b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')


async def bridge_checks(tmp: str) -> None:
    config = {
        'database_url_env': ENV,
        'archive_dir': str(Path(tmp) / 'decision_logs'),
        'metrics_interval_s': 10.0,
        'metrics_retention_days': 30,
        'write_token_env': 'FALLRELAY_WRITE_TOKEN',
        'host': '127.0.0.1',
        'port': 5001,
        'cors_origins': [],
        'clip_dir': str(Path(tmp) / 'clips'),
        'clip_pre_s': 1.0,
        'clip_post_s': 0.5,
        'clip_fps': 10.0,
        'clip_retention_days': 30,
        'edge_stale_after_s': 3.0,
        'video_stale_after_s': 3.0,
        'cameras': [{'id': '1', 'label': 'CAM 1', 'location': ''}, {'id': '2', 'label': 'Hall', 'location': ''}],
    }
    app = bridge.create_app(config)
    token = app.state.registry.issue_token('2')
    for i in range(15):  # about 1.5 s of debugging video on camera 2 before the fall
        await call(app, 'POST', '/push_frame', query='cam=2', body=frame(i), content_type='image/jpeg', token=token)
        await asyncio.sleep(0.1)
    opening, update = event_messages('2', run='f' * 32)[:2]
    code, _, _ = await call(app, 'POST', '/api/edge_event', body=json.dumps(opening).encode(), token=token)
    check('explicit open accepted by the bridge', code == 200)
    code, _, _ = await call(app, 'POST', '/api/edge_event', body=json.dumps(update).encode(), token=token)
    check('update of the same event accepted (no second clip)', code == 200)
    for i in range(15, 25):
        await call(app, 'POST', '/push_frame', query='cam=2', body=frame(i), content_type='image/jpeg', token=token)
        await asyncio.sleep(0.1)

    _, _, raw = await call(app, 'GET', '/api/events')
    event = json.loads(raw)['events'][0]
    check('event lists a clip state', event['clip_status'] in ('recording', 'ready'), event)
    recorder = app.state.recorder
    check(
        'bridge clip finished', await asyncio.to_thread(wait_for, lambda: recorder.clip_status(event['id']) == 'ready')
    )

    _, _, raw = await call(app, 'GET', '/api/events')
    event = json.loads(raw)['events'][0]
    check('event links its clip', event['clip_status'] == 'ready' and event['clip_url'].endswith('.webm'), event)
    _, _, raw = await call(app, 'GET', '/api/clips')
    listing = json.loads(raw)
    check('one clip per episode', len(listing['clips']) == 1 and listing['recording_enabled'], listing)
    clip = listing['clips'][0]
    check(
        'clip listing carries event and camera',
        clip['event_id'] == event['id']
        and clip['camera_no'] == '2'
        and clip['camera_label'] == 'Hall'
        and clip['fall_time'],
        clip,
    )
    code, headers, data = await call(app, 'GET', clip['video_url'])
    check(
        'clip is served as video/webm',
        code == 200 and headers.get('content-type', '').startswith('video/webm') and len(data) == clip['size_bytes'],
        (code, headers.get('content-type')),
    )
    recorder.close()


with tempfile.TemporaryDirectory() as tmp, Schema():
    asyncio.run(bridge_checks(tmp))

print('All clip checks passed.')
