"""Run identity of an edge runtime: the model, code and decision rule behind every decision it reports.

The runtime builds the identity (edge/fall_relay_edge_runtime.py, run_identity); the bridge validates it here,
stores it once per run (model_run_store.py) and rejects reports that contradict it.
"""

import json
import math
import re

from decision_block import validate_decision

CONTRACT_V2 = 'fallscope-pose13-feature59-v2'
# Modules hashed in a run identity: everything between the pose model and the decision.
IMPLEMENTATIONS = {
    'pose17',
    'causal_tracker',
    'features_v2',
    'head_point',
    'shape_features_v2',
    'decision_engine',
    'edge_runtime_v2_core',
}
# Files hashed in the identity's event_decision block: the decision rule and the event transport on the device.
DECISION_PROVENANCE = {
    'decision_engine',
    'edge_outbox',
    'edge_event_runtime',
    'edge_event_contract',
    'schema_migrations',
    'edge_schema_1',
    'edge_schema_2',
}


def validate_identity(item):
    if not isinstance(item, dict) or len(json.dumps(item, allow_nan=False)) > 8192:
        raise ValueError('invalid model identity')
    if item.get('schema_version') != 1 or item.get('contract_version') != CONTRACT_V2:
        raise ValueError('unsupported run identity schema/contract')
    if not isinstance(item.get('run_id'), str) or not re.fullmatch('[a-f0-9]{32}', item['run_id']):
        raise ValueError('invalid run_id')
    if not isinstance(item.get('bundle_id'), str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', item['bundle_id']):
        raise ValueError('invalid bundle_id')
    for key in ('bundle_sha256', 'config_sha256', 'runtime_sha256'):
        if not isinstance(item.get(key), str) or not re.fullmatch('[a-f0-9]{64}', item[key]):
            raise ValueError(f'invalid {key}')
    for key, roles in (
        ('artifacts_sha256', {'pose_param', 'pose_weights', 'lpn', 'tcn'}),
        ('implementation_sha256', IMPLEMENTATIONS),
    ):
        if (
            not isinstance(item.get(key), dict)
            or set(item[key]) != roles
            or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in item[key].values())
        ):
            raise ValueError(f'invalid {key}')
    settings = item.get('settings')
    limits = {
        'inference_hz': (0.01, 60),
        'window_slots': (20, 20),
        'min_valid_pose_slots': (1, 20),
        'fall_threshold': (0, 1),
        'fall_confirm_slots': (1, 10000),
    }
    if not isinstance(settings, dict) or set(settings) != set(limits):
        raise ValueError('invalid model settings')
    for key, (low, high) in limits.items():
        v = settings[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not low <= v <= high:
            raise ValueError(f'invalid {key}')
    started = item.get('started_at')
    if not isinstance(started, (float, int)) or isinstance(started, bool) or not math.isfinite(started) or started < 1:
        raise ValueError('invalid started_at')
    if not isinstance(item.get('verification'), str) or len(item['verification']) > 256:
        raise ValueError('invalid verification description')
    if 'event_decision' in item:
        decision = item['event_decision']
        if (
            not isinstance(decision, dict)
            or set(decision) != {'event_protocol', 'declared', 'effective', 'overridden', 'implementation_sha256'}
            or decision['event_protocol'] != 'edge_event'
            or not isinstance(decision['overridden'], bool)
        ):
            raise ValueError('invalid event decision identity')
        validate_decision(decision['declared'])
        validate_decision(decision['effective'])
        if not decision['overridden'] and decision['declared'] != decision['effective']:
            raise ValueError('undeclared decision override')
        hashes = decision['implementation_sha256']
        if (
            not isinstance(hashes, dict)
            or set(hashes) != DECISION_PROVENANCE
            or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in hashes.values())
        ):
            raise ValueError('invalid event decision implementation provenance')
    return item
