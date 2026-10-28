"""Device-side event delivery: the outbox worker that sends stored messages until the bridge acknowledges them."""

import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from edge_event_contract import CONTRACT


def request_json(url, headers, body=None):
    encoded = json.dumps(body, allow_nan=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=encoded, headers={'Content-Type': 'application/json', **headers})
    with urllib.request.urlopen(req, timeout=1) as response:
        raw = response.read(8193)
        if len(raw) > 8192:
            raise ValueError('event HTTP response too large')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('HTTP response must be an object')
        return result


class OutboxWorker:
    def __init__(self, box, server, camera, headers, request_fn=request_json):
        self.box, self.server, self.camera, self.headers = box, server.rstrip('/'), camera, headers
        self.request = request_fn
        self.stop = threading.Event()
        self.guard = threading.Lock()
        self.calibration = None
        self.next_clock = 0
        self.last_prune = 0
        self.thread = threading.Thread(target=self.run, name='edge-events', daemon=True)

    def clock(self):
        with self.guard:
            sample = self.calibration
        if not sample or time.monotonic() - sample[0] > 10:
            return {'offset_s': 0.0, 'rtt_s': 60.0}
        return dict(sample[1])

    def calibrate(self):
        start = time.time()
        mono = time.monotonic()
        data = self.request(
            self.server + '/api/edge_event/context?cam=' + urllib.parse.quote(self.camera), self.headers
        )
        if not isinstance(data, dict):
            raise ValueError('context must be an object')
        end = time.time()
        rtt = time.monotonic() - mono
        value = data.get('server_time')
        if (
            data.get('contract') != CONTRACT
            or data.get('camera_id') != self.camera
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or abs((end - start) - rtt) > 0.1
        ):
            raise ValueError('invalid clock/context response')
        with self.guard:
            self.calibration = (time.monotonic(), {'offset_s': value - (start + end) / 2, 'rtt_s': rtt})

    def send_once(self, now=None):
        simulated = now is not None
        now = time.time() if now is None else now
        payload = self.box.next_due(now)
        if payload is None:
            return False
        try:
            receipt = self.request(self.server + '/api/edge_event', self.headers, payload)
            self.box.complete(payload, receipt, now if simulated else time.time())
        except urllib.error.HTTPError as exc:
            permanent = 400 <= exc.code < 500 and exc.code not in (408, 425, 429)
            self.box.fail(payload, now if simulated else time.time(), f'event HTTP {exc.code}', permanent=permanent)
        except (OSError, ValueError, TypeError):
            self.box.fail(payload, now if simulated else time.time(), 'event transport/receipt unavailable')
        return True

    def run(self):
        try:
            while not self.stop.is_set():
                mono = time.monotonic()
                if mono >= self.next_clock:
                    self.next_clock = mono + 2
                    try:
                        self.calibrate()
                    except (OSError, ValueError, TypeError):
                        pass
                sent = self.send_once()
                if mono - self.last_prune >= 60:
                    self.box.prune(time.time())
                    self.last_prune = mono
                if not sent:
                    self.stop.wait(0.05)
        except Exception:
            logging.exception('Event outbox worker stopped; messages retained')
            self.stop.set()

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=4)
        if self.thread.is_alive():
            raise RuntimeError('outbox worker did not stop; database left open')
