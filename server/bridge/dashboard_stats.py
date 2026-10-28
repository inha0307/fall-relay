"""Operational statistics for the Reports page, computed from what the bridge already stores.

- Alerts (alerts table): counts, time to acknowledge / resolve, on-site outcomes, hour of day.
- Monitored time and availability (metrics table, one row per camera every metrics interval): a row counts as
  monitored when the camera's edge decisions were arriving. Time the bridge itself was down has no rows, so
  availability describes the time the server was observing, not wall-clock uptime.
- Edge performance (metrics rows while connected): decision rate and per-interval latency percentiles.
- Device hardware (hardware_samples): temperature and how often a power/thermal limit was active.

False alarms per monitored hour divide the alerts resolved as false alarms by monitored hours; alerts nobody has
resolved yet are counted separately and are not assumed to be either outcome.
"""

from __future__ import annotations

import time
from db_backend import has_table

FALSE_ALARMS = ('fa_sitting', 'fa_lying', 'fa_bending', 'fa_other')
LIMIT_FLAGS = ('undervoltage', 'frequency_capped', 'throttled', 'soft_temperature_limit')


def _pct(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def _r(v, digits=1):
    return None if v is None else round(v, digits)


def summary(db, lock, cameras, since, until=None, interval_s=10.0, design_hz=10.0, tz_offset_s=None):
    until = time.time() if until is None else until
    tz = -time.timezone if tz_offset_s is None else tz_offset_s  # local day / hour boundaries
    with lock:
        keys = ('id', 'type', 'camera_id', 'created_at', 'state', 'acknowledged_at', 'resolved_at', 'resolution')
        alerts = [
            dict(zip(keys, tuple(r)))
            for r in db.execute(
                f"SELECT {','.join(keys)} FROM alerts WHERE created_at>=? AND created_at<?", (since, until)
            )
        ]
        metrics = db.execute(
            'SELECT ts,camera_id,edge_connected,edge_hz,latency_p50_ms,latency_p95_ms FROM metrics '
            'WHERE ts>=? AND ts<? ORDER BY camera_id,ts',
            (since, until),
        ).fetchall()

        def field(path):
            return "(payload::jsonb #> '{" + path.replace('.', ',') + "}')"

        hardware = (
            db.execute(
                'SELECT device_id,'
                + ','.join(
                    field(path)
                    for path in ('camera_id', 'metrics.temperature_c', *(f'throttle.current.{f}' for f in LIMIT_FLAGS))
                )
                + ' FROM hardware_samples WHERE received_at>=? AND received_at<?',
                (since, until),
            ).fetchall()
            if has_table(db, 'hardware_samples')
            else []
        )

    falls = [a for a in alerts if a['type'] == 'fall']
    resolved = [a for a in falls if a['state'] == 'resolved' and a['resolution']]
    confirmed = sum(a['resolution'] == 'fall' for a in resolved)
    false_alarms = sum(a['resolution'] in FALSE_ALARMS for a in resolved)
    outcomes = {}
    for a in resolved:
        outcomes[a['resolution']] = outcomes.get(a['resolution'], 0) + 1
    ack = [a['acknowledged_at'] - a['created_at'] for a in falls if a['acknowledged_at']]
    res = [a['resolved_at'] - a['created_at'] for a in falls if a['resolved_at']]
    days = {}
    for a in alerts:
        day = time.strftime('%Y-%m-%d', time.gmtime(a['created_at'] + tz))
        bucket = days.setdefault(day, {'fall': 0, 'device': 0})
        bucket['fall' if a['type'] == 'fall' else 'device'] += 1
    hours = [0] * 24
    for a in falls:
        hours[int((a['created_at'] + tz) % 86400 // 3600)] += 1

    per_camera = {}
    # An outage is a run of observed rows without decisions; its length counts observed rows only, so time the
    # bridge itself was down never inflates it.
    for ts, cam, connected, hz, p50, p95 in map(tuple, metrics):
        if cam not in cameras:
            continue
        c = per_camera.setdefault(cam, {'rows': 0, 'up': 0, 'hz': [], 'p50': [], 'p95': [], 'outages': [], 'run': 0})
        c['rows'] += 1
        if connected:
            c['up'] += 1
            c['hz'].append(hz)
            c['p50'].append(p50)
            c['p95'].append(p95)
            if c['run']:
                c['outages'].append(c['run'] * interval_s)
                c['run'] = 0
        else:
            c['run'] += 1
    camera_rows = []
    for cam in sorted(cameras, key=str):
        c = per_camera.get(cam, {'rows': 0, 'up': 0, 'hz': [], 'p50': [], 'p95': [], 'outages': [], 'run': 0})
        outages = c['outages'] + ([c['run'] * interval_s] if c['run'] else [])
        hours_up = c['up'] * interval_s / 3600
        mine = [a for a in falls if a['camera_id'] == cam]
        fa = sum(a['state'] == 'resolved' and a['resolution'] in FALSE_ALARMS for a in mine)
        camera_rows.append(
            {
                'camera_id': cam,
                'monitored_h': _r(hours_up, 2),
                'observed_h': _r(c['rows'] * interval_s / 3600, 2),
                'availability_pct': _r(100 * c['up'] / c['rows']) if c['rows'] else None,
                'outages': len(outages),
                'outage_min': _r(sum(outages) / 60),
                'longest_outage_min': _r(max(outages) / 60 if outages else 0),
                'mean_hz': _r(sum(c['hz']) / len(c['hz']), 2) if c['hz'] else None,
                'design_rate_pct': _r(100 * sum(c['hz']) / len(c['hz']) / design_hz) if c['hz'] else None,
                'latency_p50_ms': _r(_pct(c['p50'], 0.5)),
                'latency_p95_ms': _r(_pct(c['p95'], 0.95)),
                'fall_alerts': len(mine),
                'false_alarms': fa,
                'fall_alerts_per_h': _r(len(mine) / hours_up, 2) if hours_up else None,
                'false_alarms_per_h': _r(fa / hours_up, 2) if hours_up else None,
            }
        )

    devices = {}
    for device, cam, temp, *flags in map(tuple, hardware):
        d = devices.setdefault(device, {'camera_id': cam, 'temps': [], 'samples': 0, 'limited': 0, 'unknown': 0})
        d['samples'] += 1
        if temp is not None:
            d['temps'].append(temp)
        if all(f is None for f in flags):
            d['unknown'] += 1
        elif any(flags):
            d['limited'] += 1
    device_rows = [
        {
            'device_id': k,
            'camera_id': v['camera_id'],
            'samples': v['samples'],
            'temp_mean_c': _r(sum(v['temps']) / len(v['temps'])) if v['temps'] else None,
            'temp_max_c': _r(max(v['temps'])) if v['temps'] else None,
            'limit_active_pct': _r(100 * v['limited'] / (v['samples'] - v['unknown']), 2)
            if v['samples'] > v['unknown']
            else None,
        }
        for k, v in sorted(devices.items())
    ]

    monitored = sum(r['monitored_h'] or 0 for r in camera_rows)
    observed = sum(r['observed_h'] or 0 for r in camera_rows)
    return {
        'since': since,
        'until': until,
        'interval_s': interval_s,
        'design_hz': design_hz,
        'alerts': {
            'fall': len(falls),
            'device': len(alerts) - len(falls),
            'waiting': sum(a['state'] in ('pending', 'sent', 'delivered') for a in alerts),
            'unresolved_falls': len(falls) - len(resolved),
            'ack_median_s': _r(_pct(ack, 0.5)),
            'ack_p90_s': _r(_pct(ack, 0.9)),
            'resolve_median_s': _r(_pct(res, 0.5)),
            'per_day': [{'day': k, **v} for k, v in sorted(days.items())],
            'fall_hour_of_day': hours,
        },
        'quality': {
            'resolved_falls': len(resolved),
            'confirmed': confirmed,
            'false_alarms': false_alarms,
            'uncertain': sum(a['resolution'] == 'uncertain' for a in resolved),
            'outcomes': outcomes,
            'field_precision': _r(confirmed / (confirmed + false_alarms), 3) if confirmed + false_alarms else None,
            'monitored_h': _r(monitored, 2),
            'false_alarms_per_h': _r(false_alarms / monitored, 3) if monitored else None,
            'fall_alerts_per_h': _r(len(falls) / monitored, 3) if monitored else None,
        },
        'availability_pct': _r(100 * monitored / observed) if observed else None,
        'cameras': camera_rows,
        'devices': device_rows,
    }
