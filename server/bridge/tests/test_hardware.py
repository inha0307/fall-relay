#!/usr/bin/env python3
"""Telemetry tests: flags, null readings, persistence, stale state and API auth/export."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from _pg import ENV, Schema, url
from event_store import EventStore
from hardware_store import HardwareStore, validate
from pi_hardware_agent import network_counters, throttle_flags, Sampler
from video_bridge import create_app

os.environ['FALLRELAY_WRITE_TOKEN'] = 'hardware-test-write'


async def call(app, method, path, *, query='', body=None, headers=None, json_response=True):
    data = b'' if body is None else json.dumps(body).encode()
    headers = {**({'Content-Type': 'application/json'} if body is not None else {}), **(headers or {})}
    if method == 'GET' and path.startswith('/api/') and not any(k.lower() == 'x-write-token' for k in headers):
        headers['X-Write-Token'] = os.environ.get(
            'FALLRELAY_WRITE_TOKEN', ''
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
        'headers': [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
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

    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    raw = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    return (start['status'], json.loads(raw or b'{}')) if json_response else (start['status'], raw, start['headers'])


def config(tmp):
    return {
        'database_url_env': ENV,
        'archive_dir': str(Path(tmp) / 'decision_logs'),
        'metrics_interval_s': 10,
        'metrics_retention_days': 30,
        'write_token_env': 'FALLRELAY_WRITE_TOKEN',
        'cors_origins': [],
        'edge_stale_after_s': 3,
        'video_stale_after_s': 3,
        'cameras': [{'id': '1', 'label': 'CAM 1', 'location': ''}],
    }


def sample():
    return {
        'schema_version': 1,
        'device_id': 'pi-test',
        'camera_id': '1',
        'measured_at': time.time(),
        'interval_s': 5,
        'spec': {'model': 'Raspberry Pi test', 'cpu_cores': 4},
        'metrics': {'cpu_pct': 32, 'temperature_c': 54, 'ram_total_bytes': 8192, 'ram_available_bytes': 4096},
        'throttle': {'raw': 0x50005},
        'processes': [],
        'errors': [],
        'collection_ms': 5,
    }


def pure_checks(tmp):
    flags = throttle_flags(0x50005)
    assert flags['current']['undervoltage'] and flags['current']['throttled']
    assert flags['since_boot']['undervoltage'] and not flags['current']['frequency_capped']
    assert not any(throttle_flags(0x50000)['current'].values())
    assert throttle_flags(None)['current'] is None
    print('PASS current flags, boot history and unavailable state remain distinct')
    with patch('pi_hardware_agent.command', return_value=None):
        agent = Sampler({'device_id': 'local', 'camera_id': '1', 'interval_s': 5})
        report = agent.sample()
        assert report['throttle']['raw'] is None and report['metrics']['cpu_pct'] is None
        assert report['errors']
    print('PASS unavailable firmware command and first CPU sample never become healthy zeroes')
    payload = sample()
    for bad in (float('nan'), float('inf'), -1, 101, True):
        candidate = copy.deepcopy(payload)
        candidate['metrics']['cpu_pct'] = bad
        try:
            validate(candidate)
        except ValueError:
            continue
        raise AssertionError(f'accepted bad CPU value {bad}')
    print('PASS malformed / nonfinite / out-of-range telemetry rejected')
    net = Path(tmp) / 'net'
    for name in ('eth0', 'wlan0'):
        (net / 'sys' / name / 'device').mkdir(parents=True)
    (net / 'sys' / 'docker0').mkdir()
    (net / 'dev').write_text(
        'Inter-|   Receive  |  Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets\n'
        '    lo: 900 9 0 0 0 0 0 0 900 9 0 0 0 0 0 0\n'
        '  eth0: 1000 10 0 0 0 0 0 0 52000 40 0 0 0 0 0 0\n'
        ' wlan0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n'
        'docker0: 5 1 0 0 0 0 0 0 7 1 0 0 0 0 0 0\n'
    )
    counters = network_counters(str(net / 'dev'), str(net / 'sys'))
    assert counters == {
        'eth0': {'rx_bytes': 1000, 'rx_packets': 10, 'tx_bytes': 52000, 'tx_packets': 40},
        'wlan0': {'rx_bytes': 0, 'rx_packets': 0, 'tx_bytes': 0, 'tx_packets': 0},
    }, counters
    assert validate({**payload, 'network': counters})['network'] == counters and validate(payload)['network'] == {}
    for bad in ({'eth0': {'tx_bytes': -1}}, {'eth0': {'tx_bytes': 1.5}}, {'bad name!': {}}, {'eth0': 5}):
        try:
            validate({**payload, 'network': bad})
        except ValueError:
            continue
        raise AssertionError(f'accepted bad network block {bad}')
    print('PASS physical interface counters collected, virtual ones skipped, malformed counters rejected')
    events = EventStore(url())
    store = HardwareStore(events._db, retention_days=1)
    now = time.time()
    assert store.record(payload, now=now)
    assert not store.record(payload, now=now + 0.2)
    assert not store.devices(now=now + 10)[0]['stale']
    assert store.devices(now=now + 21)[0]['stale']
    assert store.devices(now=now + 21)[0]['throttle']['current']['undervoltage']
    other = {**payload, 'camera_id': '2'}
    try:
        store.record(other, now=now + 5)
    except ValueError:
        pass
    else:
        raise AssertionError('another camera took over existing device')
    store.close()
    store = HardwareStore(events._db, retention_days=1)
    assert store.devices()[0]['device_id'] == payload['device_id']
    rows, truncated = store.history('pi-test', 1)
    assert len(rows) == 1 and not truncated
    store.db.execute(
        'INSERT INTO hardware_samples(device_id,received_at,payload) VALUES (?,?,?)',
        ('pi-test', now - 2 * 86400, json.dumps(payload)),
    )
    store.db.commit()
    rows, _ = store.history('pi-test', 168)
    assert len(rows) == 1
    store.close()
    events.close()
    print('PASS deduplication, staleness, binding, restart persistence and retention window')


async def api_checks(tmp):
    cfg = config(tmp)
    cfg['clip_dir'] = None
    cfg['require_camera_tokens'] = True
    app = create_app(cfg)
    token = app.state.registry.issue_token('1')
    auth = {'X-Camera-Token': token}
    payload = sample()
    code, _ = await call(app, 'POST', '/api/device_health', body=payload)
    assert code == 403
    code, _ = await call(app, 'POST', '/api/device_health', body=payload, headers={'X-Camera-Token': 'wrong'})
    assert code == 403
    code, body = await call(app, 'POST', '/api/device_health', body=payload, headers=auth)
    assert code == 200 and body['recorded']
    code, body = await call(app, 'POST', '/api/device_health', body=payload, headers=auth)
    assert code == 200 and not body['recorded']
    code, body = await call(app, 'GET', '/api/devices')
    assert code == 200 and len(body['devices']) == 1
    assert body['devices'][0]['throttle']['current']['undervoltage']
    device = body['devices'][0]  # the web draws devices from their profile, not from assumptions
    assert device['profile']['name'] == 'raspberry-pi' and device['profile']['design_rate_hz'] == 10.0
    assert device['camera_ids'] == [device['camera_id']] and 'restart_inference' in body['command_catalog']
    code, detail = await call(app, 'GET', '/api/devices/pi-test')
    assert code == 200 and detail['device']['device_id'] == 'pi-test' and detail['command_catalog']
    assert (await call(app, 'GET', '/api/devices/nobody'))[0] == 404
    code, body = await call(app, 'GET', '/api/devices/pi-test/metrics')
    assert len(body['samples']) == 1 and not body['truncated']
    code, raw, _ = await call(app, 'GET', '/api/devices/pi-test/metrics', query='format=csv', json_response=False)
    assert code == 200 and b'throttle_raw' in raw and b'pi-test' in raw
    for query in ('hours=nan', 'hours=-1', 'hours=9999'):
        code, _ = await call(app, 'GET', '/api/devices/pi-test/metrics', query=query)
        assert code == 422, (query, code)
    code, _ = await call(app, 'POST', '/api/device_health', body={**payload, 'camera_id': '9'}, headers=auth)
    assert code == 400
    code, _ = await call(
        app, 'POST', '/api/device_health', body={**payload, 'metrics': {'temperature_c': 999}}, headers=auth
    )
    assert code == 400
    code, _ = await call(app, 'POST', '/api/device_health', body={**payload, 'extra': 'x' * 40000}, headers=auth)
    assert code == 413
    print('PASS telemetry API authentication, malformed input, history bounds and JSON/CSV export')
    app.state.hardware.close()
    app.state.store.close()
    app.state.registry.close()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as tmp, Schema():
        pure_checks(tmp)
    with tempfile.TemporaryDirectory() as tmp, Schema():
        asyncio.run(api_checks(tmp))
    print('All hardware checks passed.')
