#!/usr/bin/env python3
"""Dashboard statistics on a synthetic database with known answers."""

from _bridge_path import BRIDGE, FIXTURES  # noqa: F401
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time

from _pg import Schema, url
import dashboard_stats
from camera_registry import CameraRegistry
from event_store import EventStore
from test_hardware import call, config
from video_bridge import create_app

os.environ['FALLRELAY_WRITE_TOKEN'] = 'hardware-test-write'
DAY = 86400.0
T0 = 1_800_000_000.0 - (1_800_000_000.0 % DAY)


def database():
    events = EventStore(url())
    CameraRegistry([{'id': '1'}, {'id': '2'}, {'id': '3'}], connection=events._db, lock=events._lock)
    db = events._db
    rows = [  # type, camera, created, state, acked, resolved, resolution
        ('fall', '1', T0 + 3600, 'resolved', T0 + 3600 + 30, T0 + 3600 + 300, 'fall'),
        ('fall', '1', T0 + 7200, 'resolved', T0 + 7200 + 90, T0 + 7200 + 600, 'fa_sitting'),
        ('fall', '2', T0 + 7300, 'resolved', T0 + 7300 + 60, T0 + 7300 + 400, 'fa_lying'),
        ('fall', '2', T0 + DAY + 3600, 'delivered', None, None, None),
        ('camera_offline', '2', T0 + DAY + 100, 'resolved', None, T0 + DAY + 200, 'recovered'),
        ('fall', '1', T0 - 10, 'resolved', T0, T0 + 5, 'fall'),
    ]
    with db:
        for kind, camera, created, state, acked, resolved, resolution in rows:
            event_id = None
            if kind == 'fall':
                event_id = db.execute(
                    'INSERT INTO events(camera_id,started_at,max_score) VALUES (?,?,0.97)', (camera, created)
                ).lastrowid
            db.execute(
                'INSERT INTO alerts(event_id,type,camera_id,created_at,state,acknowledged_at,resolved_at,resolution) VALUES (?,?,?,?,?,?,?,?)',
                (event_id, kind, camera, created, state, acked, resolved, resolution),
            )
        metric = 'INSERT INTO metrics(ts,camera_id,video_connected,edge_connected,edge_hz,latency_p50_ms,latency_p95_ms) VALUES (?,?,1,?,?,?,?)'
        # camera 1: 2 h observed, connected except one 10-minute outage; then a 1 h bridge gap (no rows) and 1 h more
        for i in range(720):
            db.execute(metric, (T0 + i * 10, '1', int(not 300 <= i < 360), 10.0, 45.0, 60.0))
        for i in range(360):
            db.execute(metric, (T0 + 10800 + i * 10, '1', 1, 10.0, 45.0, 60.0))
        for i in range(360):  # camera 2: 1 h at 7.5 Hz
            db.execute(metric, (T0 + i * 10, '2', 1, 7.5, 120.0, 150.0))
        db.execute(metric, (T0 + 100, '3', 1, 10.0, 1.0, 1.0))  # a camera that is not asked about
        flags = lambda t: {
            'undervoltage': False,
            'frequency_capped': False,
            'throttled': t,
            'soft_temperature_limit': False,
        }
        for i, (temp, limited) in enumerate([(60, False), (70, True), (80, False), (None, None)]):
            db.execute(
                'INSERT INTO hardware_samples(device_id,received_at,payload) VALUES (?,?,?)',
                (
                    'pi',
                    T0 + i,
                    json.dumps(
                        {
                            'camera_id': '1',
                            'metrics': {'temperature_c': temp},
                            'throttle': {'current': None if limited is None else flags(limited)},
                        }
                    ),
                ),
            )
    return events


def unit():
    events = database()
    r = dashboard_stats.summary(events._db, events._lock, ['1', '2'], T0, T0 + 2 * DAY, 10.0, tz_offset_s=0)
    a, q = r['alerts'], r['quality']
    assert (a['fall'], a['device'], a['waiting'], a['unresolved_falls']) == (4, 1, 1, 1), a
    assert a['ack_median_s'] == 60 and a['resolve_median_s'] == 400, a
    day = lambda t: time.strftime('%Y-%m-%d', time.gmtime(t))
    assert a['per_day'] == [{'day': day(T0), 'fall': 3, 'device': 0}, {'day': day(T0 + DAY), 'fall': 1, 'device': 1}], a
    assert a['fall_hour_of_day'][1] == 2 and a['fall_hour_of_day'][2] == 2 and sum(a['fall_hour_of_day']) == 4
    assert (q['confirmed'], q['false_alarms'], q['field_precision']) == (1, 2, 0.333), q
    c1, c2 = r['cameras']
    assert [c['camera_id'] for c in r['cameras']] == ['1', '2']  # cameras outside the scope are left out
    assert (
        c1['observed_h'] == 3.0
        and c1['monitored_h'] == round(2 + 50 / 60, 2)
        and c1['outages'] == 1
        and c1['outage_min'] == 10
    ), c1
    assert (
        c1['availability_pct'] == round(100 * (1080 - 60) / 1080, 1)
        and c1['mean_hz'] == 10.0
        and c1['false_alarms'] == 1
    ), c1
    assert c2['monitored_h'] == 1.0 and c2['design_rate_pct'] == 75.0 and c2['false_alarms_per_h'] == 1.0, c2
    assert q['monitored_h'] == round(c1['monitored_h'] + 1.0, 2) and q['false_alarms_per_h'] == round(
        2 / q['monitored_h'], 3
    )
    [d] = r['devices']
    assert (d['temp_mean_c'], d['temp_max_c'], d['limit_active_pct']) == (70.0, 80.0, 33.33), d
    events.close()
    print(
        'PASS counts, time to acknowledge/resolve, outcomes and precision, monitored time without bridge gaps, outages, rates, device limits'
    )


async def api(root):
    app = create_app(config(root))
    code, body = await call(app, 'GET', '/api/dashboard', query='days=7')
    assert code == 200 and body['alerts']['fall'] == 0 and body['cameras'][0]['camera_id'] == '1', body
    assert (await call(app, 'GET', '/api/dashboard', query='days=1000'))[0] == 422
    app.state.hardware.close()
    app.state.registry.close()
    app.state.store.close()
    print('PASS dashboard API on an empty database and range validation')


if __name__ == '__main__':
    with Schema():
        unit()
    with tempfile.TemporaryDirectory() as folder, Schema():
        asyncio.run(api(Path(folder)))
