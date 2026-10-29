#!/usr/bin/env python3
"""Hardware-free detection workflow: synthetic decision scores through the deployed edge code to an alert.

Two synthetic cameras run the same modules the Raspberry Pi runs after inference (v2 decision engine,
EventBridgeV2 + SQLite outbox, edge_result heartbeat with the 13-joint skeleton) against the deployed
bridge. Camera 1 falls, lies down and gets up; camera 2 stays normal. The demo then uploads a generated
fall clip the way the device does, and walks the alert through acknowledgement and an on-site outcome.
No camera, pose model or classifier runs: scores and keypoints are generated, so nothing here measures
detection accuracy or device speed.

The bridge runs on PostgreSQL, as in the laboratory deployment: the demo creates a schema of its own
(demo_<random>) in the database named by FALLRELAY_DATABASE_URL, initialises it empty, and drops it when
it stops. Nothing outside that schema is read or changed.
"""

from __future__ import annotations
import argparse
import base64
import json
import os
import platform
import random
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import threading
import sys
import time
import urllib.error
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent
EDGE = HERE.parents[1] / 'edge'
CAMERAS = ('1', '2')
SLOT_NS = 100_000_000


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Client:
    def __init__(self, base):
        self.base = base

    def request(self, method, path, body=None, headers=None, expected=200, raw=False):
        headers = dict(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body, allow_nan=False).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                status, data = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, data = exc.code, exc.read()
        require(status == expected, f'{method} {path}: expected {expected}, got {status}: {data[:300]!r}')
        return data if raw else json.loads(data)


def edge_modules():
    """The Pi-side modules: edge first so its decision_engine (the deployed one) wins."""
    sys.path.insert(0, str(EDGE))
    from decision_engine import DecisionEngine, DecisionParams
    from event_bridge_v2 import EventBridgeV2
    from features_v2 import SlotFeaturizer
    import fall_relay_edge_runtime as runtime

    return DecisionEngine, DecisionParams, EventBridgeV2, SlotFeaturizer, runtime


def head_table():
    """Calibrated head-point table if present; otherwise a neutral one (no offset).

    The calibrated table is NTU-derived and not distributed (NOTICE). Keypoints here are synthetic,
    so the neutral table only moves the drawn head point; messages keep the same shape.
    """
    import numpy as np
    from head_point import TABLE_PATH, load_table

    return load_table() if TABLE_PATH.exists() else {"pooled": np.zeros(2), "subsets": {}}


def keypoints(lying: bool):
    """Generated COCO-17 keypoints in the 640x360 pose frame: a standing or a lying figure."""
    stand = [
        (320, 70),
        (326, 64),
        (314, 64),
        (334, 68),
        (306, 68),
        (345, 100),
        (295, 100),
        (355, 140),
        (285, 140),
        (360, 178),
        (280, 178),
        (335, 185),
        (305, 185),
        (338, 245),
        (302, 245),
        (340, 305),
        (300, 305),
    ]
    if lying:  # rotate about the hip centre so the figure lies on the floor
        cx, cy = 320, 300
        stand = [(cx + (y - 185) * 1.1, cy - (x - 320) * 0.4) for x, y in stand]
    return [[float(x), float(y), 0.9] for x, y in stand]


def synthetic_frame(text, k17):
    """A generated JPEG (no real person): the figure drawn on a plain background, labelled SYNTHETIC."""
    import cv2
    import numpy as np

    image = np.full((360, 640, 3), 230, np.uint8)
    cv2.putText(image, text, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 180), 1)
    for x, y, _ in k17:
        cv2.circle(image, (int(x), int(y)), 4, (90, 60, 10), -1)
    ok, jpeg = cv2.imencode('.jpg', image)
    require(ok, 'JPEG generation failed')
    return jpeg.tobytes()


def run_camera(cam, slots, server, headers, output, modules, pace_hz=None, stop=None):
    """Run one synthetic camera: slots = [(score, upright, lying)] on a 10 Hz grid. Returns the open message (or None).

    Slots are sent faster than real time unless pace_hz is given (the --serve keep-alive streams in real time
    until `stop` is set)."""
    DecisionEngine, DecisionParams, EventBridgeV2, SlotFeaturizer, runtime = modules
    params = DecisionParams(theta=0.95, k=3, n=5, m=3, cooldown_s=0.0)  # deployed operating point
    engine = DecisionEngine(params)
    run_id = uuid.uuid4().hex
    bridge = EventBridgeV2(output / f'edge_cam{cam}_outbox.sqlite3', server, cam, run_id, engine, headers)
    manifest = json.loads((EDGE / 'bundle_manifest.json').read_text())
    settings = {
        'inference_hz': 10,
        'window_slots': 20,
        'min_valid_pose_slots': 10,
        'fall_threshold': 0.95,
        'fall_confirm_slots': 3,
    }
    edge_config = output / f'edge_config_cam{cam}.json'
    edge_config.write_text('{"synthetic": true}\n')
    identity = runtime.run_identity(run_id, manifest, edge_config, runtime.__file__, settings, params, bridge)

    class Core:  # the attributes edge_result() reads from EdgeRuntimeV2Core
        def __init__(self):
            self.engine, self.history_valid, self.min_valid, self.featurizer = (
                engine,
                [True] * 20,
                10,
                SlotFeaturizer(head_table()),
            )

    from features_v2 import ORDER

    core, opened = Core(), None
    t0 = time.monotonic_ns()
    try:
        for i, (score, upright, lying) in enumerate(slots):
            if stop is not None and stop.is_set():
                break
            sensor = t0 + i * SLOT_NS
            events = engine.step(sensor / 1e9, score, True, upright, lying)
            bridge.step(sensor, time.time(), score, True, 7, events)
            r = {
                'state': engine.state,
                'eligible': True,
                'valid': True,
                'k17': keypoints(not upright),
                'box': [280.0, 60.0, 360.0, 310.0],
                'people': 1,
                'track_id': 7,
                'score': score,
                'detector_score': 0.9,
            }
            payload = runtime.edge_result(r, core, params, cam, i, identity, (2.0, 2.0), 50.0, bridge, ORDER)
            reply = json.loads(
                urllib.request.urlopen(
                    urllib.request.Request(
                        server + '/api/edge_result',
                        json.dumps(payload).encode(),
                        {'Content-Type': 'application/json', **headers},
                    ),
                    timeout=5,
                ).read()
            )
            require(reply.get('ok') is True, f'heartbeat refused: {reply}')
            if any(ev['kind'] == 'open' for ev in events):
                opened = {'run_id': run_id, 'onset_sensor_ns': bridge.active['onset'], 'decided_sensor_ns': sensor}
            time.sleep(
                1 / pace_hz if pace_hz else 0.02
            )  # .02: keep the heartbeat stream fresh without waiting real time
        deadline = time.monotonic() + 10
        while bridge.box.status()['outbox_pending'] and time.monotonic() < deadline:
            time.sleep(0.05)
        require(bridge.box.status()['outbox_pending'] == 0, f'camera {cam}: outbox not drained: {bridge.box.status()}')
    finally:
        bridge.close()
    return opened


# --serve keeps the fixture alive so every screen has content: (device, camera, model, camera module,
# cooling, decisions per second). The second device runs below the 10 Hz design rate, as the Pi 4 did.
SYNTHETIC_DEVICES = (
    ('synthetic-pi5', '1', 'Raspberry Pi 5 Model B (synthetic)', 'Camera Module 3', 'active cooler', 10.0),
    ('synthetic-pi4', '2', 'Raspberry Pi 4 Model B (synthetic)', 'IMX519', 'fan', 7.3),
)


def telemetry(device, started, rng):
    device_id, cam, model, camera, cooling, hz = device
    pi5 = hz >= 10
    return {
        'schema_version': 1,
        'device_id': device_id,
        'camera_id': cam,
        'measured_at': time.time(),
        'interval_s': 5,
        'spec': {
            'model': model,
            'cpu_cores': 4,
            'os': 'Raspberry Pi OS 12 (synthetic fixture)',
            'camera': camera,
            'cooling': cooling,
            'metadata_source': 'demo.py synthetic fixture',
            'runtime_versions': {'python': platform.python_version()},
        },
        'metrics': {
            'cpu_pct': round((26 if pi5 else 39) + rng.uniform(-3, 3), 1),
            'temperature_c': round((61 if pi5 else 49) + rng.uniform(-1.5, 1.5), 1),
            'clock_mhz': 2400 if pi5 else 1500,
            'ram_total_bytes': 8 * 2**30,
            'ram_available_bytes': 6 * 2**30,
            'ram_used_bytes': int((2.1 if pi5 else 1.6) * 2**30),
            'disk_total_bytes': 64 * 2**30,
            'disk_free_bytes': 41 * 2**30,
            'uptime_s': 5 * 3600 + time.time() - started,
        },
        'throttle': {'raw': 0},
        'errors': [],
        'collection_ms': 4,
        'processes': [
            {
                'service': f'detection-cam{cam} (synthetic)',
                'running': True,
                'pid': 1000 + int(cam),
                'cpu_pct': round((24 if pi5 else 37) + rng.uniform(-2, 2), 1),
                'rss_bytes': (226 if pi5 else 221) * 2**20,
            }
        ],
        'control': {'commands': ['restart_inference', 'stop_inference', 'start_inference', 'logs']},
    }


def keep_alive(server, tokens, output, modules, stop):
    """--serve: normal activity on both synthetic cameras and telemetry from their synthetic devices until `stop`."""

    def normal():
        while True:
            yield 0.05, True, False

    threads = [
        threading.Thread(
            target=run_camera,
            daemon=True,
            name=f'synthetic-cam{device[1]}',
            args=(device[1], normal(), server, tokens[device[1]], output / 'serve', modules, device[5], stop),
        )
        for device in SYNTHETIC_DEVICES
    ]
    (output / 'serve').mkdir(exist_ok=True)
    for thread in threads:
        thread.start()
    started, rng = time.time(), random.Random(20261022)
    while not stop.is_set():
        for device in SYNTHETIC_DEVICES:
            headers = {'Content-Type': 'application/json', **tokens[device[1]]}
            try:
                reply = json.loads(
                    urllib.request.urlopen(
                        urllib.request.Request(
                            server + '/api/device_health', json.dumps(telemetry(device, started, rng)).encode(), headers
                        ),
                        timeout=5,
                    ).read()
                )
                for item in reply.get('commands') or []:  # acknowledge remote actions without doing anything
                    result = {
                        'device_id': device[0],
                        'ok': True,
                        'output': f'synthetic device: "{item.get("command")}" accepted; nothing was run',
                    }
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f'{server}/api/device_commands/{int(item["id"])}/result',
                            json.dumps(result).encode(),
                            headers,
                        ),
                        timeout=5,
                    ).read()
            except (OSError, ValueError):
                pass  # the bridge may be stopping
        stop.wait(5)


def scenario(client, output, write, server, database):
    modules = edge_modules()
    cameras = {
        cam: {'X-Camera-Token': client.request('POST', f'/api/admin/cameras/{cam}/token', {}, write)['token']}
        for cam in CAMERAS
    }
    client.camera_tokens = cameras
    client.request('POST', '/api/edge_result', {'cam_id': '1'}, expected=403)
    client.request('POST', '/api/edge_clip', {'camera_id': '1'}, expected=422)
    client.request('GET', '/api/alerts', expected=403)

    # Camera 1: 2 s normal, 0.5 s high scores (fall), 3 s lying, 4 s upright -> recovered. Camera 2: normal.
    fall = (
        [(0.01, True, False)] * 20
        + [(0.99, False, False)] * 5
        + [(0.02, False, True)] * 30
        + [(0.01, True, False)] * 40
    )
    normal = [(0.01, True, False)] * len(fall)
    opened = run_camera('1', fall, server, cameras['1'], output, modules)
    require(opened is not None, 'camera 1 did not open a fall event')
    require(run_camera('2', normal, server, cameras['2'], output, modules) is None, 'camera 2 opened an event')

    # Device clip: the window around the open decision (3 s before, 2 s after), generated frames only.
    frames = []
    for i in range(25):
        k17 = keypoints(lying=i >= 15)
        frames.append(
            {'t': time.time() - 5 + i * 0.2, 'jpeg': base64.b64encode(synthetic_frame('SYNTHETIC CAM 1', k17)).decode()}
        )
    upload = {
        'camera_id': '1',
        'run_id': opened['run_id'],
        'onset_sensor_ns': opened['onset_sensor_ns'],
        'frames': frames,
    }
    client.request('POST', '/api/edge_clip', upload, cameras['2'], expected=403)  # another camera's token
    clip_reply = client.request('POST', '/api/edge_clip', upload, cameras['1'])

    responder1 = {**write, 'X-Alert-Actor': 'DEMO-RESP-1', 'X-Alert-Role': 'responder', 'X-Alert-Cameras': '1'}
    responder2 = {**write, 'X-Alert-Actor': 'DEMO-RESP-2', 'X-Alert-Role': 'responder', 'X-Alert-Cameras': '2'}
    viewer = {**write, 'X-Alert-Actor': 'DEMO-VIEW', 'X-Alert-Role': 'viewer', 'X-Alert-Cameras': '1'}
    alerts = client.request('GET', '/api/alerts', headers=responder1)['alerts']
    require(
        len(alerts) == 1 and alerts[0]['type'] == 'fall' and alerts[0]['camera_id'] == '1',
        f'expected one fall alert: {alerts}',
    )
    require(client.request('GET', '/api/alerts', headers=responder2)['alerts'] == [], 'camera scope leaked')
    alert = alerts[0]['id']
    deadline = time.monotonic() + 30
    while True:
        try:
            clip = client.request('GET', f'/api/alerts/{alert}/clip', headers=responder1, raw=True)
            break
        except RuntimeError:
            require(time.monotonic() < deadline, 'fall clip was not encoded')
            time.sleep(0.2)
    (output / 'fall_clip.webm').write_bytes(clip)
    client.request('POST', f'/api/alerts/{alert}/ack', {}, viewer, expected=403)
    client.request(
        'POST', f'/api/alerts/{alert}/resolve', {'label': 'fall'}, responder1, expected=409
    )  # not acknowledged yet
    client.request('POST', f'/api/alerts/{alert}/ack', {}, responder1)
    client.request('POST', f'/api/alerts/{alert}/ack', {}, {**responder1, 'X-Alert-Actor': 'DEMO-RESP-3'}, expected=409)
    client.request(
        'POST',
        f'/api/alerts/{alert}/resolve',
        {'label': 'fall', 'note': 'Synthetic fixture; not an observation.'},
        responder1,
    )
    detail = client.request('GET', f'/api/alerts/{alert}', headers=responder1)
    require(
        detail['state'] == 'resolved' and detail['resolution'] == 'fall' and detail['acknowledged_by'] == 'DEMO-RESP-1',
        f'alert not resolved: {detail}',
    )

    live = {row['id']: row for row in client.request('GET', '/api/live', headers=write)['cameras']}
    for cam in CAMERAS:
        result = live[cam]['connection']['last_edge_result'] or {}
        require(not live[cam]['video_connected'], f'camera {cam} should send no video')
        require(len(result.get('skeleton') or []) == 13, f'camera {cam}: 13-joint skeleton missing')
    import psycopg

    with psycopg.connect(database) as db:
        events = db.execute('SELECT camera_id,source,close_reason,last_message_seq FROM events ORDER BY id').fetchall()
        timeline = [
            {'action': action, 'actor': actor, 'to_state': state}
            for action, actor, state in db.execute(
                'SELECT action,actor,to_state FROM alert_log WHERE alert_id=%s ORDER BY id', (alert,)
            )
        ]
    require(
        events == [('1', 'edge_event', 'recovered', events[0][3])] and events[0][3] >= 3, f'unexpected events: {events}'
    )

    report = {
        'synthetic_only': True,
        'real_model_inference': False,
        'database': 'postgresql, temporary schema dropped at exit',
        'events': [{'camera_id': c, 'source': s, 'close_reason': r, 'messages': m} for c, s, r, m in events],
        'alert': {'id': alert, 'state': detail['state'], 'resolution': detail['resolution'], 'timeline': timeline},
        'clip': {'frames_uploaded': clip_reply['frames'], 'webm_bytes': len(clip)},
        'cameras': {
            cam: {
                'video_connected': live[cam]['video_connected'],
                'skeleton_joints': len(live[cam]['connection']['last_edge_result']['skeleton']),
            }
            for cam in CAMERAS
        },
        'checks': [
            'unauthenticated result, clip and alert requests rejected',
            'deployed decision engine + outbox: open/update/close as one event, camera 2 none',
            'heartbeats carry the 13-joint skeleton and no video reaches the server',
            'fall clip accepted only with the owning camera token and encoded once',
            'alert visible only within the responder camera scope',
            'viewer cannot acknowledge; resolve before acknowledge refused; second acknowledgement refused',
            'acknowledged then resolved with an on-site outcome',
        ],
        'limitations': 'Generated scores, keypoints and frames: no model accuracy, device speed or real motion. '
        'Slots are replayed faster than real time on one host clock.',
    }
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--output', type=Path, required=True, help='new directory; never an existing operational directory'
    )
    parser.add_argument('--port', type=int, default=0, help='loopback port; 0 chooses a free port')
    parser.add_argument('--serve', action='store_true', help='keep isolated bridge available after fixture completes')
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    require(0 <= args.port <= 65535, 'invalid port')
    conninfo = os.environ.get('FALLRELAY_DATABASE_URL')
    require(bool(conninfo), 'set FALLRELAY_DATABASE_URL to a PostgreSQL database in which this role may create schemas')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', args.port))
        port = sock.getsockname()[1]
        with socket.socket() as web_sock:
            web_sock.bind(('127.0.0.1', 0))
            web_port = web_sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    config = {
        'schema_version': 1,
        'host': '127.0.0.1',
        'port': port,
        'cors_origins': [f'http://127.0.0.1:{web_port}', f'http://localhost:{web_port}'],
        'database_url_env': 'FALLRELAY_DEMO_DATABASE_URL',
        'archive_dir': str(output / 'decision_logs'),
        'clip_dir': str(output / 'clips'),
        'clip_pre_s': 3.0,
        'clip_post_s': 2.0,
        'clip_retention_days': 3,
        'require_camera_tokens': True,
        'require_media_token': True,
        'write_token_env': 'FALLRELAY_DEMO_WRITE_TOKEN',
        'features': {'alerts': True},
        'cameras': [{'id': cam, 'label': f'SYNTHETIC CAM {cam}', 'location': 'Fixture only'} for cam in CAMERAS],
    }
    config_path = output / 'server.json'
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    env_path = output / 'demo.env'
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(
            f'FALLRELAY_DEMO_WRITE_TOKEN={token}\nFALLRELAY_WRITE_TOKEN={token}\nPIPELINE_SERVER_URL=http://127.0.0.1:{port}\nNEXT_PUBLIC_PIPELINE_URL=http://127.0.0.1:{port}\nFALLRELAY_DEMO_WEB_PORT={web_port}\n'
        )
    schema, database = create_schema(conninfo, output)
    with env_path.open('a') as handle:
        handle.write(f'FALLRELAY_APP_SCHEMA={schema}\n')
    env = {**os.environ, 'FALLRELAY_DEMO_WRITE_TOKEN': token, 'FALLRELAY_DEMO_DATABASE_URL': database}
    with (output / 'bridge.log').open('w') as log:
        process = subprocess.Popen(
            [sys.executable, str(HERE / 'video_bridge.py'), '--config', str(config_path)],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            client = Client(f'http://127.0.0.1:{port}')
            write = {'X-Write-Token': token}
            deadline = time.monotonic() + 15
            while True:
                require(process.poll() is None, 'demo server exited; inspect bridge.log')
                try:
                    client.request('GET', '/api/cameras', headers=write)
                    break
                except (OSError, RuntimeError):
                    if time.monotonic() > deadline:
                        raise RuntimeError('demo startup timed out; inspect bridge.log')
                    time.sleep(0.1)
            report = scenario(client, output, write, client.base, database)
            print(json.dumps(report, indent=2), flush=True)
            print(f'Artifacts: {output}', flush=True)
            if args.serve:
                stop = threading.Event()
                threading.Thread(
                    target=keep_alive,
                    args=(client.base, client.camera_tokens, output, edge_modules(), stop),
                    daemon=True,
                ).start()
                print(
                    f'Isolated bridge: {client.base}; Ctrl+C to stop. Synthetic cameras keep sending normal activity, and two synthetic devices report.',
                    flush=True,
                )
                try:
                    process.wait()
                finally:
                    stop.set()
        except KeyboardInterrupt:
            pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            drop_schema(conninfo, schema)


def create_schema(conninfo, output):
    """Create an empty bridge schema of this demo's own in the given database; return (name, scoped conninfo)."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from postgres.schema import initialize

    require('options' not in conninfo_to_dict(conninfo), 'FALLRELAY_DATABASE_URL must not set options (search_path)')
    schema = 'demo_' + uuid.uuid4().hex[:12]
    with psycopg.connect(conninfo) as connection:
        require(
            connection.execute(
                'SELECT has_database_privilege(current_user, current_database(), %s)', ('CREATE',)
            ).fetchone()[0],
            'the PostgreSQL role may not create schemas in this database',
        )
        initialize(connection, schema)
    print(f'PostgreSQL schema {schema} created; it is dropped when the demo stops.', flush=True)
    return schema, make_conninfo(conninfo, options='-c search_path=' + schema)


def drop_schema(conninfo, schema):
    import psycopg
    from psycopg import sql

    with psycopg.connect(conninfo) as connection:
        connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
    print(f'PostgreSQL schema {schema} dropped.', flush=True)


if __name__ == '__main__':
    main()
