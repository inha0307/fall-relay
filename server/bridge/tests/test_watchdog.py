#!/usr/bin/env python3
"""Synthetic-clock offline/thermal fault injection; no Pi accuracy/performance claims."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
from _pg import Schema
import copy
from pathlib import Path
import tempfile
from alert_store import AlertStore
from watchdog import Watchdog
from test_hardware import config
from video_bridge import create_app


def main(root):
    cfg = config(root)
    cfg['features'] = {'alerts': True, 'watchdog': True}
    cfg['require_camera_tokens'] = True
    cfg['require_media_token'] = True
    app = create_app(cfg)
    events = app.state.store
    watch = Watchdog(events)
    normal = {
        'camera_id': '1',
        'uptime_s': 0,
        'video_age_s': 0,
        'inference_age_s': 0,
        'hardware_age_s': 0,
        'hardware': {'stale': False, 'metrics': {'temperature_c': 50}, 'throttle': {'current': {'throttled': False}}},
    }
    watch.tick([normal], 100)
    # A skeleton-only camera never pushes video: that alone is not an outage.
    watch.tick([{**normal, 'video_seen': False, 'video_age_s': 500}], 101)
    assert events._db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0] == 0
    bad = {**normal, 'video_age_s': 29, 'inference_age_s': 29, 'hardware_age_s': 29, 'hardware': None}
    watch.tick([bad], 129)
    watch.tick([bad], 130)
    rows = events._db.execute('SELECT id,type FROM alerts').fetchall()
    assert len(rows) == 2
    assert {r['type'] for r in rows} == {'camera_offline', 'device_warning'}
    offline = next(r['id'] for r in rows if r['type'] == 'camera_offline')
    # Manual resolution while the fault persists must not reopen every scan.
    store = AlertStore(events)
    store.transition(offline, ['1'], 'U1', 'ack', {})
    store.transition(offline, ['1'], 'U1', 'resolve', {'label': 'device_other'})
    watch = Watchdog(events)
    watch.tick([bad], 131)
    assert events._db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0] == 2
    for now in range(132, 136):
        watch.tick([normal], now)
    assert events._db.execute('SELECT state FROM alerts WHERE id=?', (offline,)).fetchone()[0] == 'resolved'
    thermal = events._db.execute("SELECT id FROM alerts WHERE type='device_warning'").fetchone()[0]
    # Unknown temperature resets the 10-minute continuous healthy timer.
    unknown = copy.deepcopy(normal)
    unknown['hardware']['metrics']['temperature_c'] = None
    for now in range(136, 200):
        watch.tick([normal], now)
    watch.tick([unknown], 200)
    for now in range(201, 801):
        watch.tick([normal], now)
    assert events._db.execute('SELECT state FROM alerts WHERE id=?', (thermal,)).fetchone()[0] == 'pending'
    watch.tick([normal], 801)
    assert events._db.execute('SELECT resolution FROM alerts WHERE id=?', (thermal,)).fetchone()[0] == 'recovered'
    # A new fault after recovery yields one new alert, never one per scan.
    bad_edge = {**normal, 'inference_age_s': 29}
    watch.tick([bad_edge], 802)
    watch.tick([bad_edge], 803)
    unseen = {**normal, 'video_seen': False, 'inference_seen': False}
    for now in range(804, 810):
        watch.tick([unseen], now)
    assert (
        events._db.execute("SELECT COUNT(*) FROM alerts WHERE type='camera_offline' AND state='pending'").fetchone()[0]
        == 1
    )
    assert events._db.execute("SELECT COUNT(*) FROM alerts WHERE type='camera_offline'").fetchone()[0] == 2
    hot = copy.deepcopy(normal)
    hot['hardware']['metrics']['temperature_c'] = 80
    watch.tick([hot], 810)
    assert events._db.execute("SELECT COUNT(*) FROM alerts WHERE type='device_warning'").fetchone()[0] == 2
    # Server restart/unobserved clock gap cannot accelerate recovery.
    watch = Watchdog(events)
    watch.tick([normal], 2000)
    watch.tick([normal], 2001)
    assert (
        events._db.execute("SELECT COUNT(*) FROM alerts WHERE type='device_warning' AND state='pending'").fetchone()[0]
        == 1
    )
    assert watch.last_status[0]['hardware_fresh'] is True
    # A wall clock stepped back by seconds (VM/WSL time correction) neither resets nor shortens the recovery timer.
    drift = Watchdog(events)
    base = 3000
    stepped = lambda k: base + 1 + k - 2.5 * (k // 30)  # wall time stepped back 2.5 s every 30 s
    for k in range(0, 598):
        drift.tick([normal], stepped(k), base + 1 + k)
    open_warnings = lambda: events._db.execute(
        "SELECT COUNT(*) FROM alerts WHERE type='device_warning' AND state!='resolved'"
    ).fetchone()[0]
    assert open_warnings() == 1
    for k in range(598, 602):
        drift.tick([normal], stepped(k), base + 1 + k)
    assert open_warnings() == 0
    events.close()
    print(
        'PASS synthetic 29s video/inference/telemetry outages, one open warning, restart latch, manual-resolution suppression, 600s fresh-only recovery, unknown reset, recurrent fault and recovery timers immune to wall-clock steps'
    )


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as folder, Schema():
        main(Path(folder))
