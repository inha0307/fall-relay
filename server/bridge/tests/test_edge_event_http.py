#!/usr/bin/env python3
"""Real loopback HTTP + actual bridge restart with the device's decision engine and outbox; no camera or models."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
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

import psycopg
from _pg import Schema, url
from edge_event_runtime import request_json
from edge_outbox import EdgeOutbox

HERE = BRIDGE
sys.path.insert(0, str(BRIDGE.parents[1] / 'edge'))  # the device's decision engine and event bridge
from decision_engine import DecisionEngine, DecisionParams  # noqa: E402
from event_bridge_v2 import EventBridgeV2  # noqa: E402


def main(root):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = f'http://127.0.0.1:{port}'
    secret = secrets.token_urlsafe(32)
    config = {
        'schema_version': 1,
        'host': '127.0.0.1',
        'port': port,
        'cameras': [{'id': '1'}, {'id': '2'}],
        'database_url_env': 'FALLRELAY_HTTP_TEST_DATABASE_URL',
        'archive_dir': str(root / 'decision_logs'),
        'clip_dir': None,
        'write_token_env': 'FALLRELAY_HTTP_TEST_WRITE_TOKEN',
        'require_camera_tokens': True,
        'require_media_token': True,
        'features': {},
    }
    config_path = root / 'config.json'
    config_path.write_text(json.dumps(config))
    process = None
    runtime = None
    box = None
    log = (root / 'server.log').open('w')

    def start():
        nonlocal process
        process = subprocess.Popen(
            [sys.executable, str(HERE / 'video_bridge.py'), '--config', str(config_path)],
            env={**os.environ, 'FALLRELAY_HTTP_TEST_WRITE_TOKEN': secret, 'FALLRELAY_HTTP_TEST_DATABASE_URL': url()},
            stdout=log,
            stderr=log,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError('synthetic server exited')
            try:
                request_json(server + '/api/live', {'X-Write-Token': secret})
                return
            except (OSError, ValueError):
                time.sleep(0.05)
        raise AssertionError('server startup timed out')

    def stop():
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    try:
        start()
        token = request_json(server + '/api/admin/cameras/1/token', {'X-Write-Token': secret}, {})['token']
        headers = {'X-Camera-Token': token}
        lost = True

        def lose_once(url, auth, payload=None):
            nonlocal lost
            result = request_json(url, auth, payload)
            if lost and url.endswith('/api/edge_event'):
                lost = False
                raise OSError('synthetic response loss after real HTTP commit')
            return result

        engine = DecisionEngine(DecisionParams(theta=0.95, k=3, n=5, m=3, cooldown_s=0.0))
        runtime = EventBridgeV2(root / 'outbox.sqlite3', server, '1', 'b' * 32, engine, headers, request_fn=lose_once)
        runtime.worker.calibrate()
        for index in range(35):  # 3.5 s of high scores: the event opens and is updated while it stays open
            sensor = 10**9 + index * 10**8
            runtime.step(sensor, time.time(), 0.99, True, 4, engine.step(sensor / 1e9, 0.99, True, False, None))
        with runtime.box.lock:
            assert runtime.box.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] >= 2
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and runtime.box.status()['outbox_pending']:
            time.sleep(0.02)
        assert runtime.box.status()['outbox_pending'] == 0
        assert lost is False
        listing = request_json(server + '/api/events', {'X-Write-Token': secret})['events']
        assert len(listing) == 1 and listing[0]['source'] == 'edge_event' and listing[0]['ended_at'] is None
        with runtime.box.lock:
            original = json.loads(
                runtime.box.db.execute('SELECT payload FROM outbox WHERE message_seq=1').fetchone()[0]
            )
        stop()
        start()
        listing = request_json(server + '/api/events', {'X-Write-Token': secret})['events']
        assert listing[0]['ended_at'] is None
        receipt = request_json(server + '/api/edge_event', headers, original)
        assert receipt['duplicate'] is True
        runtime.close()
        runtime = None
        box = EdgeOutbox(root / 'outbox.sqlite3')
        assert box.status()['outbox_pending'] == 0
        event = request_json(server + '/api/events', {'X-Write-Token': secret})['events'][0]
        assert event['ended_at'] is not None and event['close_reason'] == 'runtime_stop'
        with psycopg.connect(url()) as db:
            assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM edge_event_receipts').fetchone()[0] >= 3
        try:
            request_json(server + '/api/edge_event', {}, original)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError('missing camera token accepted')
        print(
            'PASS real loopback HTTP: device decision engine/outbox → authenticated bridge, lost-response retry, bridge restart, retained open event and persisted runtime_stop close; one event/alert'
        )
    finally:
        if runtime:
            runtime.close()
        if box:
            box.close()
        stop()
        log.close()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as folder, Schema():
        main(Path(folder))
