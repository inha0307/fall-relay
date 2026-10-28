#!/usr/bin/env python3
"""Run identities: immutable and camera-bound in the store; registered, referenced compactly, checked against every
heartbeat and attached to events over the API; durable across a bridge restart."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
import uuid

from _pg import Schema, url
from event_store import EventStore
from edge_fixtures import DECISION, identity
from model_contract import validate_identity
from model_run_store import ModelRunStore
from test_hardware import call, config as api_config
from video_bridge import create_app


def reject(fn, error=ValueError):
    try:
        fn()
    except error:
        return
    raise AssertionError('invalid operation accepted')


def store_checks(root):
    item = identity(uuid.uuid4().hex)
    events = EventStore(url())
    runs = ModelRunStore(events)
    runs.observe('1', item, now=10)
    runs.observe('1', item, now=12)
    changed = copy.deepcopy(item)
    changed['settings']['fall_threshold'] = 0.1
    reject(lambda: runs.observe('1', changed, now=13))
    reject(lambda: runs.observe('2', item, now=14))
    runs.observe('1', identity(uuid.uuid4().hex), now=15)
    assert len(runs.listing()) == 2
    events.close()
    print('PASS run identities are immutable and bound to one camera')


def heartbeat(run, **changes):
    """An explicit-event heartbeat whose settings agree with edge_fixtures.SETTINGS and DECISION."""
    payload = {
        'cam_id': '1',
        'seq': 1,
        'status': 'NORMAL',
        'input_valid': True,
        'window_eligible': True,
        'bbox': [0, 0, 10, 10],
        'track_id': 1,
        'score': 0.1,
        'threshold': 0.95,
        'confirm_slots': 3,
        'window_min_valid_slots': 10,
        'window_valid_slots': 20,
        'event_protocol': 'edge_event',
        'contract': 'fallscope-edge-event-v1',
        'run_id': run,
        'decision_state': 'IDLE',
        'heartbeat_seq': 1,
        'outbox_pending': 0,
        'outbox_oldest_s': 0,
        'outbox_blocked': 0,
        'outbox_error': None,
        'decision_overridden': False,
        'decision_high_slots': 0,
        'decision_valid_slots': 5,
    }
    payload.update(changes)
    return payload


def open_message(run, now):
    row = json.loads((FIXTURES / 'edge_event_v1.json').read_text())[0]
    row.update(run_id=run, event_uid=run + ':1', decided_at=now, sent_at=now, clock={'offset_s': 0.0, 'rtt_s': 0.01})
    row['confirm'] = {key: DECISION[key] for key in ('k', 'n', 'm')}
    return row


async def api_checks(root):
    run = uuid.uuid4().hex
    item = identity(run, decision=DECISION)
    cfg = api_config(root)
    app = create_app(cfg)
    headers = {'X-Camera-Token': app.state.registry.issue_token('1')}
    full = heartbeat(run, model_identity=item)
    code, response = await call(app, 'POST', '/api/edge_result', body=full, headers=headers)
    assert code == 200, (code, response)
    code, history = await call(app, 'GET', '/api/model_runs')
    assert code == 200 and history['runs'][0]['run_id'] == run
    compact = heartbeat(run, model_run_id=run)
    code, reply = await call(app, 'POST', '/api/edge_result', body=compact, headers=headers)
    assert code == 200 and reply['model_run_id'] == run, (code, reply)
    for changes in ({'threshold': 0.1}, {'confirm_slots': 8}, {'window_valid_slots': 7}, {'decision_overridden': True}):
        code, _ = await call(app, 'POST', '/api/edge_result', body={**compact, **changes}, headers=headers)
        assert code == 400, (changes, code)
    code, reply = await call(
        app, 'POST', '/api/edge_result', body={**compact, 'model_run_id': uuid.uuid4().hex}, headers=headers
    )
    assert code == 409 and reply['code'] == 'model_identity_required'
    code, _ = await call(
        app, 'POST', '/api/edge_result', body={**full, 'model_run_id': uuid.uuid4().hex}, headers=headers
    )
    assert code == 400
    for bad in (None, True, '0.5', -1, 2, 10**1000, float('nan'), float('inf')):
        code, _ = await call(app, 'POST', '/api/edge_result', body={**compact, 'score': bad}, headers=headers)
        assert code == 400, (bad, code)
    code, _ = await call(
        app, 'POST', '/api/edge_result', body={k: v for k, v in compact.items() if k != 'score'}, headers=headers
    )
    assert code == 400
    changed = copy.deepcopy(item)
    changed['settings']['fall_threshold'] = 0.1
    code, _ = await call(app, 'POST', '/api/edge_result', body={**full, 'model_identity': changed}, headers=headers)
    assert code == 400
    no_decision = identity(uuid.uuid4().hex)
    code, _ = await call(
        app,
        'POST',
        '/api/edge_result',
        body=heartbeat(no_decision['run_id'], model_identity=no_decision),
        headers=headers,
    )
    assert code == 400  # an explicit-event run must declare its decision block
    code, _ = await call(app, 'POST', '/api/edge_result', body={**compact, 'event_protocol': None}, headers=headers)
    assert code == 409  # heartbeats without the event protocol are refused
    code, receipt = await call(app, 'POST', '/api/edge_event', body=open_message(run, time.time()), headers=headers)
    assert code == 200, (code, receipt)
    code, listing = await call(app, 'GET', '/api/events')
    assert listing['events'][0]['model_run_id'] == run
    restored = ModelRunStore(app.state.store)  # a new store object models a process restart
    assert restored.resolve('1', run) == validate_identity(item)
    reject(lambda: restored.resolve('2', run))
    print('PASS identity registration, compact references, settings and decision checks, score validation, attribution')
    for close in (app.state.hardware.close, app.state.registry.close, app.state.store.close):
        close()
    app = create_app(cfg)
    code, reply = await call(app, 'POST', '/api/edge_result', body=compact, headers=headers)
    assert code == 200 and reply['model_run_id'] == run, (code, reply)
    for close in (app.state.hardware.close, app.state.registry.close, app.state.store.close):
        close()
    print('PASS compact heartbeats survive a bridge restart without re-registration')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as tmp, Schema():
        store_checks(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp, Schema():
        asyncio.run(api_checks(Path(tmp)))
    print('All model management checks passed.')
