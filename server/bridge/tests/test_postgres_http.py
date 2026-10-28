"""Real HTTP/PostgreSQL tests. Generated data stays in a disposable schema.

The SSE client here is a protocol fixture, not a measured browser receipt.
"""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import base64

from _pg import Schema
from edge_fixtures import DECISION, heartbeat, identity as fixture_identity

HERE = BRIDGE


def main():
    process = None
    with tempfile.TemporaryDirectory() as folder, Schema(prefix='test_http') as schema:
        root = Path(folder)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        url = 'http://127.0.0.1:' + str(port)
        secret = secrets.token_urlsafe(32)
        ready = root / 'ready'
        env = {
            **os.environ,
            'PG_HTTP_WRITE': secret,
            'FALLRELAY_INGRESS_READY_FILE': str(ready),
            'PG_HTTP_DATABASE_URL': schema.url,
        }
        config = {
            'schema_version': 1,
            'host': '127.0.0.1',
            'port': port,
            'cameras': [{'id': '1'}, {'id': '2'}],
            'archive_dir': str(root / 'decision_logs'),
            'clip_dir': str(root / 'clips'),
            'clip_pre_s': 3,
            'clip_post_s': 2,
            'clip_fps': 10,
            'write_token_env': 'PG_HTTP_WRITE',
            'require_camera_tokens': True,
            'require_media_token': True,
            'database_url_env': 'PG_HTTP_DATABASE_URL',
            'metrics_interval_s': 1,
            'features': {'alerts': True, 'watchdog': False},
        }
        path = root / 'config.json'
        path.write_text(json.dumps(config))
        admin = {
            'X-Write-Token': secret,
            'X-Actor': 'test-admin',
            'X-Alert-Actor': 'test-admin',
            'X-Alert-Role': 'admin',
            'X-Alert-Cameras': '*',
        }

        def call(route, body=None, headers=None, method=None, raw=False):
            data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(
                url + route,
                data=data,
                method=method,
                headers={**(headers or {}), **({'Content-Type': 'application/json'} if isinstance(body, dict) else {})},
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    content = response.read()
                    return content if raw else json.loads(content)
            except urllib.error.HTTPError as error:
                print('HTTP', error.code, route, error.read()[:300], flush=True)
                raise

        def start():
            nonlocal process
            process = subprocess.Popen(
                [sys.executable, str(HERE / 'video_bridge.py'), '--config', str(path)], env=env, stdout=log, stderr=log
            )
            for _ in range(150):
                if process.poll() is not None:
                    raise AssertionError('isolated bridge exited')
                try:
                    call('/api/system', headers=admin)
                    return
                except OSError:
                    time.sleep(0.05)
            raise AssertionError('isolated bridge startup timed out')

        def stop():
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

        with (root / 'server.log').open('w+') as log:
            try:
                start()
                try:
                    call('/api/admin/cameras/1/token', {}, admin)
                except urllib.error.HTTPError as error:
                    assert error.code == 503
                else:
                    raise AssertionError('cutover gate accepted a write')
                ready.touch()
                tokens = {cam: call('/api/admin/cameras/' + cam + '/token', {}, admin)['token'] for cam in ('1', '2')}
                device = {'X-Camera-Token': tokens['1']}
                model_run = uuid.uuid4().hex
                identity = fixture_identity(model_run, decision=DECISION, bundle='synthetic-pg-test')
                report = heartbeat('2', run=model_run, model_identity=identity)
                camera2 = {'X-Camera-Token': tokens['2']}
                assert call('/api/edge_result', report, camera2)['model_run_id'] == model_run
                compact = {k: v for k, v in report.items() if k != 'model_identity'}
                compact['model_run_id'] = model_run
                assert call('/api/edge_result', compact, camera2)['model_run_id'] == model_run
                fixture = json.loads((FIXTURES / 'edge_event_v1.json').read_text())
                rid = uuid.uuid4().hex
                for index, item in enumerate(fixture):
                    item.update(
                        run_id=rid,
                        event_uid=rid + ':1',
                        camera_id='1',
                        decided_at=time.time() + index * 0.01,
                        sent_at=time.time(),
                    )
                    item['clock'] = {'offset_s': 0.0, 'rtt_s': 0.01}
                first = call('/api/edge_event', fixture[0], device)
                assert first['ok'] and not first['duplicate']
                stop()
                start()
                assert call('/api/edge_result', compact, camera2)['model_run_id'] == model_run
                retried = call('/api/edge_event', fixture[0], device)
                assert retried['duplicate'] and retried['event_id'] == first['event_id']
                for item in fixture[1:]:
                    assert call('/api/edge_event', item, device)['ok']
                print('PASS HTTP commit/response-loss retry across actual process restart, ordered event close')

                request = urllib.request.Request(url + '/api/alerts/stream?device=fixture-client', headers=admin)
                with urllib.request.urlopen(request, timeout=10) as response:
                    snapshot = None
                    while snapshot is None:
                        line = response.readline().decode()
                        if line.startswith('data: '):
                            snapshot = json.loads(line[6:])
                alert = next(a for a in snapshot['alerts'] if a['id'] == first['alert_id'])
                import cv2
                import numpy as np

                ok, encoded = cv2.imencode('.jpg', np.full((360, 640, 3), 100, dtype=np.uint8))
                assert ok
                jpeg = base64.b64encode(encoded.tobytes()).decode()
                clip = {
                    'camera_id': '1',
                    'run_id': rid,
                    'onset_sensor_ns': fixture[0]['onset_sensor_ns'],
                    'frames': [{'t': fixture[0]['decided_at'] + i * 0.1, 'jpeg': jpeg} for i in range(25)],
                }
                assert call('/api/edge_clip', clip, device)['ok']
                for _ in range(100):
                    try:
                        saved = call('/api/alerts/' + str(alert['id']) + '/clip', headers=admin, raw=True)
                        assert saved.startswith(b'\x1aE\xdf\xa3')
                        break
                    except urllib.error.HTTPError as error:
                        assert error.code == 404
                        time.sleep(0.1)
                else:
                    raise AssertionError('test clip did not become ready')
                assert (
                    call(
                        '/api/alerts/' + str(alert['id']) + '/received',
                        {'delivery_id': alert['delivery_id'], 'device_id': 'fixture-client'},
                        admin,
                    )['state']
                    == 'delivered'
                )
                assert call('/api/alerts/' + str(alert['id']) + '/ack', {}, admin)['state'] == 'acknowledged'
                assert (
                    call('/api/alerts/' + str(alert['id']) + '/resolve', {'label': 'fall'}, admin)['state']
                    == 'resolved'
                )
                sample = {
                    'schema_version': 1,
                    'device_id': 'fixture-pi',
                    'camera_id': '1',
                    'interval_s': 5,
                    'measured_at': time.time(),
                    'metrics': {},
                    'control': {'commands': ['decision_log_start', 'decision_log_stop', 'decision_log_collect']},
                }
                assert call('/api/device_health', sample, device)['ok']
                command = call(
                    '/api/devices/fixture-pi/commands',
                    {'command': 'decision_log_start', 'params': {'duration_s': 5}},
                    admin,
                )['command']
                taken = call('/api/device_health', sample, device)['commands']
                assert taken[0]['id'] == command['id']
                assert call(
                    '/api/device_commands/' + str(command['id']) + '/result',
                    {'device_id': 'fixture-pi', 'ok': True, 'output': 'synthetic test only'},
                    device,
                )['ok']

                sid = uuid.uuid4().hex
                raw = (
                    json.dumps({'session_id': sid, 'camera_id': '1', 'run_id': rid, 'wall': time.time(), 'score': 0.5})
                    + '\n'
                ).encode()
                packed = gzip.compress(raw)
                meta = {
                    'session_id': sid,
                    'run_id': rid,
                    'device_id': 'fixture-pi',
                    'camera_id': '1',
                    'size': len(packed),
                    'sha256': hashlib.sha256(packed).hexdigest(),
                    'raw_bytes': len(raw),
                    'records': 1,
                    'started_at': time.time() - 1,
                    'ended_at': time.time(),
                }
                blob = (json.dumps(meta) + '\n').encode() + packed
                upload_headers = {**device, 'X-Log-Camera': '1'}
                assert call('/api/decision_logs/upload', blob, upload_headers)['ok']
                assert call('/api/decision_logs/upload', blob, upload_headers)['duplicate']
                downloaded = call('/api/decision_logs/fixture-pi/' + sid + '/download', headers=admin, raw=True)
                assert downloaded == packed
                assert (
                    call('/api/decision_logs/fixture-pi/' + sid + '/manifest', headers=admin)['sha256']
                    == meta['sha256']
                )
                time.sleep(1.3)
                assert call('/api/system', headers=admin)['database_backend'] == 'postgresql'
                assert call('/api/events', headers=admin)['total'] == 1
                call('/api/dashboard', headers=admin)
                call('/api/model_runs', headers=admin)
                call('/api/devices', headers=admin)
                print(
                    'PASS SSE protocol fixture, receipt/ACK/resolution, device commands, diagnostic upload/download and dashboard'
                )
                stop()
                log.seek(0)
                text = log.read()
                assert 'Traceback' not in text and 'ERROR:' not in text, text
            except Exception:
                stop()
                log.seek(0)
                print(log.read())
                raise
            finally:
                stop()


if __name__ == '__main__':
    main()
