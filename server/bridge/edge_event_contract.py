"""Versioned semantic contract for explicit edge events (transport not enabled here)."""

import hashlib
import json
import math
import re

CONTRACT = 'fallscope-edge-event-v1'
CAMERA = re.compile(r'^[A-Za-z0-9_-]{1,32}$')
RUN = re.compile(r'^[0-9a-f]{32}$')
CLOSE_REASONS = {'recovered', 'timeout', 'track_lost', 'runtime_stop'}
FIELDS = {
    'contract',
    'event_uid',
    'message_seq',
    'camera_id',
    'run_id',
    'kind',
    'state',
    'onset_sensor_ns',
    'decided_sensor_ns',
    'decided_at',
    'sent_at',
    'clock',
    'max_score',
    'confirm',
    'track_id',
    'close_reason',
    'attempt',
}


def number(value, name, low=None, high=None, integer=False):
    try:
        finite = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f'{name} must be finite')
    if integer and not isinstance(value, int):
        raise ValueError(f'{name} must be an integer')
    if low is not None and value < low or high is not None and value > high:
        raise ValueError(f'{name} out of range')
    return value


def validate_edge_event(payload):
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError('missing or unknown edge event fields')
    try:
        raw = json.dumps(payload, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError('edge event must be finite JSON') from exc
    if len(raw.encode()) > 32768:
        raise ValueError('edge event exceeds 32 KiB')
    if payload['contract'] != CONTRACT:
        raise ValueError('unsupported edge event contract')
    camera, run, uid = (payload[k] for k in ('camera_id', 'run_id', 'event_uid'))
    if not isinstance(camera, str) or not CAMERA.fullmatch(camera):
        raise ValueError('invalid camera_id')
    if not isinstance(run, str) or not RUN.fullmatch(run):
        raise ValueError('invalid run_id')
    if not isinstance(uid, str) or not re.fullmatch(re.escape(run) + r':[1-9][0-9]{0,14}', uid):
        raise ValueError('event_uid must be run_id:event_seq')
    seq = number(payload['message_seq'], 'message_seq', 1, 2**53 - 1, integer=True)
    kind, state = payload['kind'], payload['state']
    if not isinstance(kind, str) or kind not in ('open', 'update', 'close'):
        raise ValueError('invalid kind')
    allowed = {'open': {'ALERT'}, 'update': {'ALERT', 'POST_FALL'}, 'close': {'COOLDOWN'}}
    if not isinstance(state, str) or state not in allowed[kind] or (seq == 1) != (kind == 'open'):
        raise ValueError('kind, state and message_seq disagree')
    reason = payload['close_reason']
    if kind == 'close':
        if not isinstance(reason, str) or reason not in CLOSE_REASONS:
            raise ValueError('close_reason required')
    elif reason is not None:
        raise ValueError('close_reason only allowed on close')
    onset = number(payload['onset_sensor_ns'], 'onset_sensor_ns', 0, 2**63 - 1, integer=True)
    number(payload['decided_sensor_ns'], 'decided_sensor_ns', onset, 2**63 - 1, integer=True)
    number(payload['decided_at'], 'decided_at', 0)
    number(payload['sent_at'], 'sent_at', 0)
    number(payload['max_score'], 'max_score', 0, 1)
    number(payload['track_id'], 'track_id', 0, 2**31 - 1, integer=True)
    number(payload['attempt'], 'attempt', 1, 2**31 - 1, integer=True)
    clock = payload['clock']
    if not isinstance(clock, dict) or set(clock) != {'offset_s', 'rtt_s'}:
        raise ValueError('invalid clock')
    number(clock['offset_s'], 'offset_s')
    number(clock['rtt_s'], 'rtt_s', 0, 60)
    confirm = payload['confirm']
    if not isinstance(confirm, dict) or set(confirm) != {'k', 'n', 'm'}:
        raise ValueError('invalid confirm')
    for key in ('k', 'n', 'm'):
        number(confirm[key], key, 1, 300, integer=True)
    if not confirm['k'] <= confirm['m'] <= confirm['n']:
        raise ValueError('confirm requires k <= m <= n')
    return json.loads(raw)


def semantic_hash(payload):
    value = validate_edge_event(payload)
    # A retry updates its transport timing/count, never its immutable message meaning.
    for key in ('sent_at', 'attempt'):
        value.pop(key)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    ).hexdigest()
