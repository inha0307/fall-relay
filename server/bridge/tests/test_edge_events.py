#!/usr/bin/env python3
"""Crash-safe outbox, ordered event receipts and the authenticated explicit-event API."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import asyncio
import json
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error

from camera_registry import CameraRegistry
from edge_event_runtime import OutboxWorker
from edge_event_store import EdgeEventStore, EventConflict
from edge_outbox import EdgeOutbox
from event_store import EventStore
from _pg import Schema, url
from test_hardware import call, config
from video_bridge import create_app
from edge_fixtures import DECISION, identity

HERE = BRIDGE
RUN = 'a' * 32


def rejects(fn, error=ValueError):
    try:
        fn()
    except error:
        return
    raise AssertionError('invalid operation accepted')


def messages(wall=100):
    rows = json.loads((FIXTURES / 'edge_event_v1.json').read_text())
    for index, row in enumerate(rows):
        row['run_id'] = RUN
        row['event_uid'] = RUN + ':1'
        row['decided_at'] = wall + index
        row['sent_at'] = wall + index
        row['clock'] = {'offset_s': 0.0, 'rtt_s': 0.01}
    return rows


def service(root):
    events = EventStore(url())
    registry = CameraRegistry([{'id': '1'}], connection=events._db, lock=events._lock)
    return events, registry, EdgeEventStore(events)


def storage(root):
    events, registry, store = service(root)
    rows = messages()
    box = EdgeOutbox(root / 'outbox.sqlite3')
    decision = {key: rows[0]['confirm'][key] for key in ('k', 'n', 'm')}
    for row in rows:
        transition = {
            'kind': row['kind'],
            'state': row['state'],
            'sensor_ns': row['decided_sensor_ns'],
            'decided_at': row['decided_at'],
            'onset_sensor_ns': row['onset_sensor_ns'],
            'track_id': row['track_id'],
            'max_score': row['max_score'],
            'close_reason': row['close_reason'],
        }
        box.record('1', RUN, transition, decision, row['clock'], 30)
    attempts = []
    lose_response = True

    def request(_url, _headers, payload):
        nonlocal lose_response
        attempts.append(payload['message_seq'])
        result = store.receive(payload, now=100 + payload['message_seq'])
        if lose_response:
            lose_response = False
            raise OSError('response lost after commit')
        return result

    worker = OutboxWorker(box, 'http://synthetic.invalid', '1', {}, request)
    worker.send_once(now=100)
    assert box.status(100)['outbox_pending'] == 4
    assert box.next_due(100) is None
    for now in range(101, 110):
        worker.send_once(now=now)
    assert attempts[:2] == [1, 1] and attempts[2:] == [2, 3, 4]
    assert box.status(110)['outbox_pending'] == 0
    assert events._db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
    assert events._db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0] == 1
    assert events._db.execute('SELECT COUNT(*) FROM alert_dispatch_jobs').fetchone()[0] == 1
    modified = {**rows[0], 'max_score': 0.999}
    rejects(lambda: store.receive(modified, now=110), EventConflict)
    after_close = {**rows[-1], 'message_seq': 5, 'kind': 'update', 'state': 'POST_FALL', 'close_reason': None}
    rejects(lambda: store.receive(after_close, now=110), EventConflict)
    box.close()
    events.close()
    registry.close()
    # Explicit open events must survive a bridge restart until the Pi closes them.
    events, registry, store = service(root)
    second = {**rows[0], 'event_uid': RUN + ':2'}
    receipt = store.receive(second, now=120)
    events.close()
    registry.close()
    events, registry, store = service(root)
    assert events._db.execute('SELECT ended_at FROM events WHERE id=?', (receipt['event_id'],)).fetchone()[0] is None
    assert store.receive(second, now=121)['duplicate']
    final = {**rows[-1], 'event_uid': RUN + ':2', 'message_seq': 2}
    store.receive(final, now=125)
    events.close()
    registry.close()
    print(
        'PASS ordered multiple updates, response-loss retry, one event/alert/dispatch, semantic conflict, terminal state and bridge restart'
    )


def crash_and_outage(root):
    script = '''import json,signal,sys\nfrom edge_outbox import EdgeOutbox\nDECISION={'k': 3, 'n': 5, 'm': 3}\nbox=EdgeOutbox(sys.argv[1])\ntransition={'kind':'open','state':'ALERT','sensor_ns':2000000000,'decided_at':100.,'onset_sensor_ns':1000000000,'track_id':4,'max_score':.99,'close_reason':None}\nbox.record('1','a'*32,transition,DECISION,{'offset_s':0.,'rtt_s':.01},30)\nprint('committed',flush=True)\nsignal.pause()\n'''
    path = root / 'crash.sqlite3'
    child = subprocess.Popen(
        [sys.executable, '-u', '-c', script, str(path)], cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        assert select.select([child.stdout], [], [], 5)[0], 'child commit timed out'
        assert child.stdout.readline().strip() == b'committed'
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()
    box = EdgeOutbox(path)
    rejects(lambda: EdgeOutbox(path), RuntimeError)
    assert box.status(101)['outbox_pending'] == 1
    assert box.recover('1', 101, 30) == 30
    assert box.status(101)['outbox_pending'] == 2
    events, registry, store = service(root)
    online = False

    def request(_url, _headers, payload):
        if not online:
            raise OSError('simulated link outage')
        return store.receive(payload, now=710)

    worker = OutboxWorker(box, 'http://synthetic.invalid', '1', {}, request)
    for now in range(100, 701):
        worker.send_once(now)
    assert (
        box.status(700)['outbox_pending'] == 2 and events._db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0
    )
    online = True
    for now in range(710, 780):
        worker.send_once(now)
    assert box.status(780)['outbox_pending'] == 0
    assert events._db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
    assert events._db.execute('SELECT close_reason FROM events').fetchone()[0] == 'runtime_stop'
    assert events._db.execute('SELECT late_arrival FROM events').fetchone()[0] == 1
    assert events._db.execute('SELECT state FROM alert_dispatch_jobs').fetchone()[0] == 'cancelled'
    box.close()
    events.close()
    registry.close()
    print(
        'PASS actual host SIGKILL recovery and simulated 600-second outage; no lost/duplicate events, late delivery suppressed'
    )


def blocked_and_capacity(root):
    row = messages()[0]
    transition = {
        'kind': 'open',
        'state': 'ALERT',
        'sensor_ns': row['decided_sensor_ns'],
        'decided_at': 100.0,
        'onset_sensor_ns': row['onset_sensor_ns'],
        'track_id': 4,
        'max_score': 0.97,
        'close_reason': None,
    }
    box = EdgeOutbox(root / 'bounded.sqlite3', max_pending=1)
    payload = box.record('1', RUN, transition, DECISION, row['clock'], 30)
    update = {**transition, 'kind': 'update', 'decided_at': 101.0, 'sensor_ns': 3000000000}
    rejects(lambda: box.record('1', RUN, update, DECISION, row['clock'], 30), RuntimeError)

    def forbidden(*_args):
        raise urllib.error.HTTPError('http://synthetic.invalid', 403, 'forbidden', None, None)

    worker = OutboxWorker(box, 'http://synthetic.invalid', '1', {}, forbidden)
    worker.send_once(now=100)
    assert box.status(100)['outbox_blocked'] == 1 and box.next_due(200) is None
    rejects(
        lambda: box.complete(
            payload,
            {'ok': True, 'event_uid': payload['event_uid'], 'message_seq': 999, 'event_id': 1, 'alert_id': 1},
            100,
        )
    )
    box.retry_blocked()
    assert box.next_due(200)['message_seq'] == 1
    close = {**transition, 'kind': 'close', 'state': 'COOLDOWN', 'close_reason': 'runtime_stop', 'decided_at': 201.0}
    box.record('1', RUN, close, DECISION, row['clock'], 30)
    assert box.status(201)['outbox_pending'] == 2
    box.close()
    print(
        'PASS permanent auth failures retained/blocked, explicit retry, invalid receipt refusal and reserved close under queue capacity'
    )


async def api(root):
    cfg = config(root)
    cfg.update(
        cameras=[{'id': '1'}, {'id': '2'}],
        clip_dir=str(root / 'clips'),
        clip_pre_s=3.0,
        clip_post_s=2.0,
        clip_retention_days=3,
    )
    app = create_app(cfg)
    token = app.state.registry.issue_token('1')
    headers = {'X-Camera-Token': token}
    rows = messages(time.time() - 4)
    assert (await call(app, 'POST', '/api/edge_event', body=rows[0]))[0] == 403
    for row in rows:
        code, result = await call(app, 'POST', '/api/edge_event', body=row, headers=headers)
        assert code == 200, (code, result)
    code, result = await call(app, 'POST', '/api/edge_event', body=rows[0], headers=headers)
    assert code == 200 and result['duplicate']
    bad = {**rows[0], 'max_score': 0.999}
    assert (await call(app, 'POST', '/api/edge_event', body=bad, headers=headers))[0] == 409
    assert (await call(app, 'POST', '/api/edge_event', body={**rows[0], 'message_seq': True}, headers=headers))[
        0
    ] == 422
    assert (await call(app, 'GET', '/api/edge_event/context', query='cam=1', headers=headers))[0] == 200
    legacy = {
        'cam_id': '1',
        'seq': 1,
        'score': 0.99,
        'threshold': 0.932,
        'consecutive_high': 8,
        'confirm_slots': 8,
        'input_valid': True,
        'window_eligible': True,
        'window_valid_slots': 20,
        'window_min_valid_slots': 10,
        'people': 1,
        'track_id': 4,
        'bbox': [0, 0, 10, 10],
        'status': 'FALL',
    }
    assert (await call(app, 'POST', '/api/edge_result', body=legacy, headers=headers))[0] == 409
    explicit = {
        **legacy,
        'event_protocol': 'edge_event',
        'contract': 'fallscope-edge-event-v1',
        'run_id': RUN,
        'decision_state': 'ALERT',
        'heartbeat_seq': 1,
        'outbox_pending': 0,
        'outbox_oldest_s': 0,
        'outbox_blocked': 0,
        'outbox_error': None,
        'decision_overridden': False,
        'decision_high_slots': 8,
        'decision_valid_slots': 10,
    }
    assert (await call(app, 'POST', '/api/edge_result', body=explicit, headers=headers))[0] == 200
    missing = {
        **explicit,
        'input_valid': False,
        'window_eligible': False,
        'window_valid_slots': 8,
        'bbox': None,
        'decision_valid_slots': 9,
    }
    assert (await call(app, 'POST', '/api/edge_result', body=missing, headers=headers))[0] == 200
    assert app.state.channels['1'].connection()['last_edge_result']['status'] == 'FALL'
    declared = {**DECISION, 'theta': 0.932, 'k': 8, 'n': 10, 'm': 8}  # matches the fixture's confirm block
    settings = {
        'inference_hz': 10,
        'window_slots': 20,
        'min_valid_pose_slots': 10,
        'fall_threshold': 0.932,
        'fall_confirm_slots': 8,
    }
    proof = identity(RUN, settings=settings, decision=declared)
    verified = {**explicit, 'model_identity': proof}
    assert (await call(app, 'POST', '/api/edge_result', body=verified, headers=headers))[0] == 200
    assert (await call(app, 'POST', '/api/edge_result', body={**verified, 'threshold': 0.5}, headers=headers))[0] == 400
    assert (
        await call(app, 'POST', '/api/edge_result', body={**verified, 'decision_overridden': True}, headers=headers)
    )[0] == 400
    assert app.state.store._db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
    assert (await call(app, 'POST', '/api/edge_result', body={**legacy, 'cam_id': '2'}))[0] == 409
    assert app.state.store._db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
    # skeleton-only device clip: matched to the stored event by (camera, run, onset), then encoded once
    import base64, cv2, numpy as np

    jpeg = base64.b64encode(cv2.imencode('.jpg', np.full((90, 160, 3), 80, np.uint8))[1].tobytes()).decode()
    upload = {
        'camera_id': '1',
        'run_id': RUN,
        'onset_sensor_ns': rows[0]['onset_sensor_ns'],
        'frames': [{'t': rows[0]['decided_at'] - 3 + i * 0.2, 'jpeg': jpeg} for i in range(25)],
    }
    assert (await call(app, 'POST', '/api/edge_clip', body=upload))[0] == 403
    assert (
        await call(
            app,
            'POST',
            '/api/edge_clip',
            body={**upload, 'onset_sensor_ns': upload['onset_sensor_ns'] + 1},
            headers=headers,
        )
    )[0] == 409
    for broken in (
        {**upload, 'frames': []},
        {**upload, 'frames': [{'t': 1, 'jpeg': 'bm90IGpwZWc='}]},
        {**upload, 'run_id': 'x'},
        {**upload, 'frames': upload['frames'][::-1]},
    ):
        assert (await call(app, 'POST', '/api/edge_clip', body=broken, headers=headers))[0] == 422
    code, result = await call(app, 'POST', '/api/edge_clip', body=upload, headers=headers)
    assert code == 200 and result['ok'] and result['frames'] == 25, (code, result)
    recorder = app.state.recorder
    event_id = result['event_id']
    for _ in range(100):
        if recorder.clip_status(event_id) == 'ready':
            break
        await asyncio.sleep(0.1)
    assert recorder.clip_status(event_id) == 'ready'
    code, again = await call(app, 'POST', '/api/edge_clip', body=upload, headers=headers)
    assert code == 200 and again['clip_status'] == 'ready'
    assert len(list((root / 'clips').glob('*.webm'))) == 1
    recorder.close()
    app.state.hardware.close()
    app.state.registry.close()
    app.state.store.close()
    print('PASS device clip upload: auth, event matching, validation, one encoded clip, idempotent resend')
    print(
        'PASS authenticated explicit-event API, schema/sequence conflicts, context, heartbeats never create events, heartbeats without the event protocol refused'
    )


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as folder, Schema():
        storage(Path(folder))
    with tempfile.TemporaryDirectory() as folder, Schema():
        crash_and_outage(Path(folder))
    with tempfile.TemporaryDirectory() as folder, Schema():
        blocked_and_capacity(Path(folder))
    with tempfile.TemporaryDirectory() as folder, Schema():
        asyncio.run(api(Path(folder)))
