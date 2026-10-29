"""Synthetic device messages for the checks: run identities, decision blocks, heartbeats and event messages, in the
form the edge runtime sends them."""

import json
from pathlib import Path
import time

from model_contract import CONTRACT_V2, DECISION_PROVENANCE, IMPLEMENTATIONS

H = 'a' * 64
# The decision block of our fall detector (edge/bundle_manifest.json) with the runtime's update interval.
DECISION = {
    'theta': 0.95,
    'k': 3,
    'n': 5,
    'm': 3,
    'post_fall_after_s': 5.0,
    'recover_hold_s': 3.0,
    'cooldown_s': 0.0,
    'max_event_s': 600.0,
    'track_lost_s': 10.0,
    'update_interval_s': 2.0,
}
SETTINGS = {
    'inference_hz': 10,
    'window_slots': 20,
    'min_valid_pose_slots': 10,
    'fall_threshold': 0.95,
    'fall_confirm_slots': 3,
}


def identity(
    run='0123456789abcdef0123456789abcdef', settings=None, decision=None, bundle='fall-relay-v2-test', **changes
):
    """A valid v2 run identity; `decision` adds the event_decision block of an explicit-event run."""
    item = {
        'schema_version': 1,
        'run_id': run,
        'bundle_id': bundle,
        'bundle_sha256': H,
        'config_sha256': H,
        'runtime_sha256': H,
        'artifacts_sha256': {k: H for k in ('pose_param', 'pose_weights', 'lpn', 'tcn')},
        'implementation_sha256': {k: H for k in IMPLEMENTATIONS},
        'settings': dict(settings or SETTINGS),
        'contract_version': CONTRACT_V2,
        'started_at': time.time(),
        'verification': 'bundle manifest hashes checked at start-up',
    }
    if decision is not None:
        item['event_decision'] = {
            'event_protocol': 'edge_event',
            'declared': dict(decision),
            'effective': dict(decision),
            'overridden': False,
            'implementation_sha256': {k: H for k in DECISION_PROVENANCE},
        }
    item.update(changes)
    return item


RUN = 'a' * 32
FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def heartbeat(camera='1', status='NORMAL', run=RUN, **changes):
    """An explicit-event heartbeat (POST /api/edge_result) whose settings agree with SETTINGS and DECISION."""
    active = status == 'FALL'
    payload = {
        'cam_id': camera,
        'seq': 1,
        'status': status,
        'input_valid': True,
        'window_eligible': True,
        'bbox': [0, 0, 10, 10],
        'track_id': 1,
        'score': 0.99 if active else 0.1,
        'threshold': 0.95,
        'confirm_slots': 3,
        'consecutive_high': 3 if active else 0,
        'window_min_valid_slots': 10,
        'window_valid_slots': 20,
        'people': 1,
        'event_protocol': 'edge_event',
        'contract': 'fallscope-edge-event-v1',
        'run_id': run,
        'decision_state': 'ALERT' if active else 'IDLE',
        'heartbeat_seq': 1,
        'outbox_pending': 0,
        'outbox_oldest_s': 0,
        'outbox_blocked': 0,
        'outbox_error': None,
        'decision_overridden': False,
        'decision_high_slots': 3 if active else 0,
        'decision_valid_slots': 5,
    }
    payload.update(changes)
    return payload


def event_messages(camera='1', run=RUN, wall=None):
    """The open, update and close messages of fixtures/edge_event_v1.json for one camera and run."""
    wall = time.time() if wall is None else wall
    rows = json.loads((FIXTURES / 'edge_event_v1.json').read_text())
    for index, row in enumerate(rows):
        row.update(camera_id=camera, run_id=run, event_uid=run + ':1', decided_at=wall + index, sent_at=wall + index)
        row['clock'] = {'offset_s': 0.0, 'rtt_s': 0.01}
    return rows
