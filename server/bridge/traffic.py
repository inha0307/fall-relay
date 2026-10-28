"""Bytes each camera's devices send to the bridge, by message kind, recorded every metrics interval.

Counted on arrival, after the camera token is accepted, as the HTTP/1.1 request size: request line, headers and
body. TLS, TCP/IP and the SSH tunnel are not included; the Pi agent's interface counters cover the wire.
"""

import threading

KINDS = ('result', 'event', 'clip', 'health', 'command', 'context', 'video', 'diagnostics')
ROUTES = (
    ('/api/edge_result', 'result'),
    ('/api/edge_event/context', 'context'),
    ('/api/edge_event', 'event'),
    ('/api/edge_clip', 'clip'),
    ('/api/device_health', 'health'),
    ('/api/device_commands/', 'command'),
    ('/api/decision_logs/upload', 'diagnostics'),
    ('/push_frame', 'video'),
)


def kind_of(path):
    return next((kind for prefix, kind in ROUTES if path.startswith(prefix)), None)


def request_bytes(request):
    """Request line + headers + body as sent over HTTP/1.1 (body size from Content-Length)."""
    line = len(request.method) + len(request.url.path) + (len(request.url.query) + 1 if request.url.query else 0) + 12
    headers = sum(len(k) + len(v) + 4 for k, v in request.headers.raw)
    try:
        body = max(0, int(request.headers.get('content-length') or 0))
    except ValueError:
        body = 0
    return line + headers + 2 + body


class TrafficMeter:
    def __init__(self):
        self.lock, self.counts = threading.Lock(), {}

    def count(self, camera, request):
        """Count an accepted device request once (a route may check the camera token twice)."""
        kind = kind_of(request.url.path)
        if kind is None or getattr(request.state, 'traffic_counted', False):
            return
        request.state.traffic_counted = True
        n = request_bytes(request)
        with self.lock:
            entry = self.counts.setdefault((str(camera), kind), [0, 0])
            entry[0] += 1
            entry[1] += n

    def drain(self):
        with self.lock:
            counts, self.counts = self.counts, {}
        return [(camera, kind, requests, nbytes) for (camera, kind), (requests, nbytes) in sorted(counts.items())]


def record(db, lock, rows, now, interval_s, retention_days):
    with lock:
        db.executemany(
            'INSERT INTO ingress_traffic(ts,camera_id,kind,requests,bytes,interval_s) VALUES (?,?,?,?,?,?)',
            [(now, camera, kind, requests, nbytes, interval_s) for camera, kind, requests, nbytes in rows],
        )
        db.execute('DELETE FROM ingress_traffic WHERE ts < ?', (now - retention_days * 86400,))
        db.commit()


def summary(db, lock, since, until, cameras, interval_s):
    """Per camera: average kbit/s and bytes by kind over the intervals the bridge was observing."""
    with lock:
        rows = db.execute(
            'SELECT camera_id, kind, SUM(requests), SUM(bytes) FROM ingress_traffic WHERE ts>=? AND ts<? GROUP BY 1,2',
            (since, until),
        ).fetchall()
        observed = dict(
            db.execute(
                'SELECT camera_id, COUNT(*) FROM metrics WHERE ts>=? AND ts<? GROUP BY 1', (since, until)
            ).fetchall()
        )
    out = []
    for cam in cameras:
        seconds = observed.get(cam, 0) * interval_s
        kinds = {
            kind: {'requests': req, 'bytes': nb, 'kbps': round(nb * 8 / seconds / 1000, 2) if seconds else None}
            for c, kind, req, nb in map(tuple, rows)
            if c == cam
        }
        total = sum(k['bytes'] for k in kinds.values())
        out.append(
            {
                'camera_id': cam,
                'observed_s': seconds,
                'bytes': total,
                'kbps': round(total * 8 / seconds / 1000, 2) if seconds else None,
                'by_kind': kinds,
            }
        )
    return out


def device_network(db, lock, since, until):
    """Per device and interface: average send/receive kbit/s from the agent's cumulative counters.

    Only pairs of consecutive samples at most three report intervals apart count, and a counter that went down
    (device reboot) is skipped, so gaps and restarts neither inflate nor dilute the rate.
    """
    import json

    with lock:
        rows = db.execute(
            'SELECT device_id, received_at, payload FROM hardware_samples WHERE received_at>=? AND received_at<? '
            'ORDER BY device_id, received_at',
            (since, until),
        ).fetchall()
    totals, previous = {}, {}
    for device, t, text in map(tuple, rows):
        item = json.loads(text)
        for name, counters in (item.get('network') or {}).items():
            key, now = (device, name), (t, counters.get('tx_bytes'), counters.get('rx_bytes'))
            last = previous.get(key)
            previous[key] = now
            if last is None or None in now or None in last or not 0 < t - last[0] <= 3 * item['interval_s']:
                continue
            tx, rx = now[1] - last[1], now[2] - last[2]
            if tx < 0 or rx < 0:
                continue
            entry = totals.setdefault(
                key, {'camera_id': item['camera_id'], 'seconds': 0.0, 'tx_bytes': 0, 'rx_bytes': 0}
            )
            entry['seconds'] += t - last[0]
            entry['tx_bytes'] += tx
            entry['rx_bytes'] += rx
    return [
        {
            'device_id': device,
            'interface': name,
            'camera_id': v['camera_id'],
            'observed_s': round(v['seconds'], 1),
            'tx_bytes': v['tx_bytes'],
            'rx_bytes': v['rx_bytes'],
            'tx_kbps': round(v['tx_bytes'] * 8 / v['seconds'] / 1000, 2),
            'rx_kbps': round(v['rx_bytes'] * 8 / v['seconds'] / 1000, 2),
        }
        for (device, name), v in sorted(totals.items())
        if v['seconds'] > 0
    ]
