"""Validated device telemetry and bounded history in the bridge database; no inference dependencies."""

from __future__ import annotations

import json
import math
import re
import time

from pi_hardware_agent import throttle_flags
from device_commands import COMMANDS
from db_backend import lock_key

METRICS = {
    'cpu_pct': (0, 100),
    'temperature_c': (-40, 150),
    'clock_mhz': (0, 10000),
    'ram_total_bytes': (0, 2**60),
    'ram_available_bytes': (0, 2**60),
    'ram_used_bytes': (0, 2**60),
    'disk_total_bytes': (0, 2**60),
    'disk_free_bytes': (0, 2**60),
    'uptime_s': (0, 1e12),
}


def number(value, low, high, key):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f'invalid {key}')
    return value


def validate(payload):
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        raise ValueError('unsupported device telemetry schema')
    device_id = payload.get('device_id', '')
    if not isinstance(device_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', device_id):
        raise ValueError('invalid device_id')
    interval = number(payload.get('interval_s'), 2, 300, 'interval_s')
    measured = number(payload.get('measured_at'), 1, 1e12, 'measured_at')
    metrics = payload.get('metrics')
    if not isinstance(metrics, dict):
        raise ValueError('metrics must be an object')
    metrics = {
        key: None if metrics.get(key) is None else number(metrics[key], *limits, key) for key, limits in METRICS.items()
    }
    raw = payload.get('throttle', {}).get('raw') if isinstance(payload.get('throttle'), dict) else None
    if raw is not None:
        number(raw, 0, 2**32 - 1, 'throttle.raw')
        if not isinstance(raw, int):
            raise ValueError('throttle.raw must be an integer')
    spec = payload.get('spec', {})
    if not isinstance(spec, dict) or len(json.dumps(spec, allow_nan=False)) > 8192:
        raise ValueError('invalid spec')
    # Only documented metadata may be served to the UI; never accept arbitrary text/HTML.
    clean_spec = {}
    for key in (
        'model',
        'os',
        'kernel',
        'architecture',
        'python',
        'camera',
        'cooling',
        'power_supply_rating',
        'metadata_source',
    ):
        value = spec.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 256):
            raise ValueError(f'invalid spec.{key}')
        clean_spec[key] = value
    cores = spec.get('cpu_cores')
    clean_spec['cpu_cores'] = None if cores is None else number(cores, 1, 4096, 'cpu_cores')
    hashes = spec.get('model_sha256', {})
    if (
        not isinstance(hashes, dict)
        or len(hashes) > 8
        or any(
            not isinstance(k, str)
            or len(k) > 64
            or (v is not None and (not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v)))
            for k, v in hashes.items()
        )
    ):
        raise ValueError('invalid model hashes')
    clean_spec['model_sha256'] = hashes
    versions = spec.get('runtime_versions', {})
    if (
        not isinstance(versions, dict)
        or len(versions) > 16
        or any(
            not isinstance(k, str) or len(k) > 64 or (v is not None and (not isinstance(v, str) or len(v) > 64))
            for k, v in versions.items()
        )
    ):
        raise ValueError('invalid runtime versions')
    clean_spec['runtime_versions'] = versions
    processes = payload.get('processes', [])
    if not isinstance(processes, list) or len(processes) > 8:
        raise ValueError('invalid processes')
    clean_processes = []
    for item in processes:
        if not isinstance(item, dict) or not isinstance(item.get('service'), str) or len(item['service']) > 128:
            raise ValueError('invalid process')
        result = {'service': item['service'], 'running': bool(item.get('running'))}
        for key, bounds in {'pid': (0, 2**31), 'rss_bytes': (0, 2**60), 'cpu_pct': (0, 100)}.items():
            value = item.get(key)
            result[key] = None if value is None else number(value, *bounds, f'process.{key}')
        clean_processes.append(result)
    network = payload.get('network', {})  # cumulative interface counters; absent from older agents
    if (
        not isinstance(network, dict)
        or len(network) > 8
        or any(
            not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', k) or not isinstance(v, dict)
            for k, v in network.items()
        )
    ):
        raise ValueError('invalid network')
    clean_network = {}
    for name, counters in network.items():
        clean_network[name] = {}
        for key in ('rx_bytes', 'rx_packets', 'tx_bytes', 'tx_packets'):
            value = counters.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**64):
                raise ValueError(f'invalid network.{name}.{key}')
            clean_network[name][key] = value
    control = payload.get('control')  # commands the device's agent accepts; absent = no remote control
    if control is not None and (
        not isinstance(control, dict)
        or not isinstance(control.get('commands'), list)
        or any(c not in COMMANDS for c in control['commands'])
    ):
        raise ValueError('invalid control')
    clean_control = {'commands': sorted(set(control['commands']))} if control else {'commands': []}
    if control and isinstance(control.get('decision_log'), dict):
        encoded = json.dumps(control['decision_log'], allow_nan=False)
        if len(encoded) > 20000:
            raise ValueError('log status too large')
        clean_control['decision_log'] = json.loads(encoded)
    errors = payload.get('errors', [])
    if not isinstance(errors, list) or len(errors) > 32 or any(not isinstance(e, str) or len(e) > 256 for e in errors):
        raise ValueError('invalid errors')
    return {
        'schema_version': 1,
        'device_id': device_id,
        'camera_id': str(payload.get('camera_id', '')),
        'measured_at': measured,
        'interval_s': interval,
        'spec': clean_spec,
        'metrics': metrics,
        'throttle': throttle_flags(raw),
        'processes': clean_processes,
        'network': clean_network,
        'errors': errors,
        'control': clean_control,
        'collection_ms': number(payload.get('collection_ms', 0), 0, 60000, 'collection_ms'),
    }


class HardwareStore:
    def __init__(self, database, retention_days=30, stale_after_s=20):
        """`database` is the event store's database facade; its transaction scope is the lock."""
        self.retention_days, self.stale_after_s = retention_days, stale_after_s
        self.db, self.lock = database, database.scope
        self.last_prune = 0

    def close(self):
        """The database belongs to the event store; nothing to release here."""

    def prune(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if now - self.last_prune >= 3600:
                self.db.execute(
                    'DELETE FROM hardware_samples WHERE received_at < ?', (now - self.retention_days * 86400,)
                )
                self.db.commit()
                self.last_prune = now

    def record(self, payload, now=None):
        item = validate(payload)
        now = time.time() if now is None else now
        with self.lock:
            lock_key(self.db, 'hardware:' + item['device_id'])
            previous = self.db.execute(
                'SELECT camera_id, received_at FROM hardware_devices WHERE device_id=?', (item['device_id'],)
            ).fetchone()
            if previous and previous['camera_id'] != item['camera_id']:
                raise ValueError('device_id is already bound to a different primary camera')
            if previous and now - previous['received_at'] < 1:
                return False
            text = json.dumps(item, allow_nan=False)
            self.db.execute(
                'INSERT INTO hardware_devices VALUES (?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET camera_id=excluded.camera_id,received_at=excluded.received_at,payload=excluded.payload',
                (item['device_id'], item['camera_id'], now, text),
            )
            self.db.execute(
                'INSERT INTO hardware_samples(device_id,received_at,payload) VALUES (?,?,?)',
                (item['device_id'], now, text),
            )
            self.db.commit()
            self.prune(now)
        return True

    def devices(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            rows = self.db.execute('SELECT * FROM hardware_devices ORDER BY device_id').fetchall()
        result = []
        for row in rows:
            item = json.loads(row['payload'])
            age = max(0, now - row['received_at'])
            stale_after = max(self.stale_after_s, item['interval_s'] * 3)
            item.update(
                received_at=row['received_at'], age_s=round(age, 2), stale=age > stale_after, stale_after_s=stale_after
            )
            result.append(item)
        return result

    def history(self, device_id, hours, limit=10000):
        with self.lock:
            # Only return retained samples even when no device has posted recently.
            since = max(time.time() - hours * 3600, time.time() - self.retention_days * 86400)
            rows = self.db.execute(
                'SELECT received_at, payload FROM hardware_samples WHERE device_id=? AND received_at>=? '
                'ORDER BY received_at DESC, id DESC LIMIT ?',
                (device_id, since, limit + 1),
            ).fetchall()
        truncated = len(rows) > limit
        rows = rows[:limit]
        return [{'received_at': row['received_at'], **json.loads(row['payload'])} for row in reversed(rows)], truncated
