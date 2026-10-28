#!/usr/bin/env python3
"""Device commands: store rules, the pull API (admin request, telemetry pickup, device result) and the agent side."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import time

from device_commands import CommandError, DeviceCommandStore, PICKUP_S, RESULT_S
from camera_registry import CameraRegistry
from event_store import EventStore
from pi_hardware_agent import Controller
from _pg import Schema, url
from test_hardware import call, config, sample
from video_bridge import create_app

WRITE = 'hardware-test-write'
os.environ['FALLRELAY_WRITE_TOKEN'] = WRITE
ALL = ['restart_inference', 'stop_inference', 'start_inference', 'logs']


def refused(fn, status, code=None):
    try:
        fn()
    except CommandError as exc:
        assert exc.status == status and (code is None or exc.code == code), (exc.status, exc.code)
        return
    raise AssertionError('command accepted')


def store_rules(root):
    events = EventStore(url())
    cameras = CameraRegistry([{'id': '1', 'label': 'Synthetic'}], connection=events._db, lock=events._lock)
    store = DeviceCommandStore(events)
    refused(lambda: store.request('pi', '1', 'rm -rf /', 'U1', ALL), 422)
    refused(lambda: store.request('pi', '1', 'reboot', 'U1', ALL), 403, 'not_allowed')
    refused(lambda: store.request('pi', '1', 'restart_inference', 'U1', ALL, open_event=True), 409, 'open_fall_event')
    store.request('pi', '1', 'logs', 'U1', ALL, open_event=True, now=100)  # reading logs never interrupts an event
    refused(lambda: store.request('pi', '1', 'restart_inference', 'U1', ALL, now=101), 409, 'busy')
    assert store.take('other', now=102) == []
    [taken] = store.take('pi', now=102)
    assert taken['command'] == 'logs' and store.take('pi', now=103) == []
    refused(lambda: store.complete(taken['id'], 'other', True, 'x', now=104), 404)
    done = store.complete(taken['id'], 'pi', True, 'line\x00 1', now=104)
    assert done['state'] == 'done' and done['result'] == 'line 1'
    refused(lambda: store.complete(taken['id'], 'pi', True, 'again', now=105), 409)
    store.request('pi', '1', 'restart_inference', 'U1', ALL, open_event=True, confirmed=True, now=200)
    assert store.listing('pi', now=200 + PICKUP_S + 1)[0]['state'] == 'expired'
    store.request('pi', '1', 'stop_inference', 'U1', ALL, now=400)
    store.take('pi', now=401)
    assert store.listing('pi', now=401 + RESULT_S + 1)[0]['state'] == 'failed'
    events.close()
    cameras.close()
    print(
        'PASS allow-list, open-event confirmation, one command at a time, pickup, device binding, expiry and result timeout'
    )


async def api(root):
    cfg = config(root)
    cfg['require_camera_tokens'] = True
    app = create_app(cfg)
    token = app.state.registry.issue_token('1')
    device = {'X-Camera-Token': token}
    admin = {'X-Write-Token': WRITE, 'X-Actor': 'U-0001'}
    plain = sample()
    code, reply = await call(app, 'POST', '/api/device_health', body=plain, headers=device)
    assert code == 200 and 'commands' not in reply  # agents without control get nothing
    code, reply = await call(app, 'POST', '/api/devices/pi-test/commands', body={'command': 'logs'}, headers=admin)
    assert code == 403 and reply['code'] == 'not_allowed', reply
    controlled = {**sample(), 'measured_at': time.time(), 'control': {'commands': ALL}}
    await asyncio.sleep(1.05)  # the store keeps one sample per second
    code, reply = await call(app, 'POST', '/api/device_health', body=controlled, headers=device)
    assert code == 200 and reply['commands'] == [], reply
    assert (await call(app, 'POST', '/api/devices/pi-test/commands', body={'command': 'logs'}))[0] == 403
    assert (
        await call(
            app, 'POST', '/api/devices/pi-test/commands', body={'command': 'logs'}, headers={'X-Write-Token': WRITE}
        )
    )[0] == 403
    assert (await call(app, 'POST', '/api/devices/nobody/commands', body={'command': 'logs'}, headers=admin))[0] == 404
    assert (await call(app, 'POST', '/api/devices/pi-test/commands', body={'command': 'reboot'}, headers=admin))[
        0
    ] == 403
    code, reply = await call(
        app, 'POST', '/api/devices/pi-test/commands', body={'command': 'restart_inference'}, headers=admin
    )
    assert code == 200 and reply['command']['state'] == 'queued' and reply['command']['requested_by'] == 'U-0001', reply
    await asyncio.sleep(1.05)
    code, reply = await call(
        app, 'POST', '/api/device_health', body={**controlled, 'measured_at': time.time()}, headers=device
    )
    [item] = reply['commands']
    result = {'device_id': 'pi-test', 'ok': True, 'output': 'service fall-relay-edge-cam1.service: active'}
    assert (await call(app, 'POST', f"/api/device_commands/{item['id']}/result", body=result))[0] == 403
    assert (
        await call(
            app,
            'POST',
            f"/api/device_commands/{item['id']}/result",
            body={**result, 'device_id': 'nobody'},
            headers=device,
        )
    )[0] == 404
    code, reply = await call(app, 'POST', f"/api/device_commands/{item['id']}/result", body=result, headers=device)
    assert code == 200 and reply['command']['state'] == 'done', reply
    code, reply = await call(app, 'GET', '/api/devices/pi-test/commands')
    assert code == 200 and reply['commands'][0]['result'].endswith('active')
    app.state.hardware.close()
    app.state.registry.close()
    app.state.store.close()
    print(
        'PASS admin request needs write token and actor; device picks the command up in its telemetry reply and reports with its camera token'
    )


def agent(root):
    calls = []

    def run(argv, **_):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, 'active\n' if argv[2] == 'is-active' else '', '')

    log = root / 'runtime.log'
    log.write_bytes(b''.join(b'line %d\n' % i for i in range(500)) + b'\x00')
    control = Controller({'inference_service': 'fall-relay-edge-cam1.service', 'log_file': str(log)}, run)
    assert control.commands == ALL + ['decision_log_start', 'decision_log_stop', 'decision_log_collect']
    ok, output = control.execute('restart_inference')
    assert (
        ok
        and calls[0] == ['systemctl', '--user', 'restart', 'fall-relay-edge-cam1.service']
        and output.endswith('active')
    )
    assert control.execute('reboot') == (False, 'command not allowed on this device: reboot') and len(calls) == 2
    ok, output = control.execute('logs')
    assert ok and output.splitlines()[0] == 'line 300' and output.splitlines()[-1] == 'line 499'
    rebooting = Controller({'inference_service': 'x.service', 'allow_reboot': True}, run)
    assert rebooting.execute('reboot')[0] and calls[-1] == ['sudo', '-n', 'systemctl', 'reboot']
    try:
        Controller({'inference_service': 'x; reboot'})
        raise AssertionError('bad service accepted')
    except ValueError:
        pass
    print(
        'PASS agent runs only allow-listed commands on its own service, returns the last 200 log lines, reboots only when allowed'
    )


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as folder, Schema():
        store_rules(Path(folder))
    with tempfile.TemporaryDirectory() as folder, Schema():
        asyncio.run(api(Path(folder)))
    with tempfile.TemporaryDirectory() as folder, Schema():
        agent(Path(folder))
