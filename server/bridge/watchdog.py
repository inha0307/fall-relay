"""Opt-in persistent camera/device alerts. Unknown readings never count as healthy.

A camera is offline when its edge decisions stop arriving. Video is optional: skeleton-only devices
send no images, and a debug JPEG stream is never required for a camera to count as healthy.

Recovery timers run on the monotonic clock of this process: wall-clock corrections (a VM or WSL guest can be
stepped back by seconds) must not reset or shorten them. A restart starts every timer again."""

import json
import time
from db_backend import lock_key


class Watchdog:
    def __init__(self, events, offline_after_s=29, recover_s=3, thermal_recover_s=600):
        self.db, self.lock = events._db, events._lock
        self.offline_after_s = offline_after_s
        self.recover_s = recover_s
        self.thermal_recover_s = thermal_recover_s
        self.last_status = []
        self.mono = {}  # camera -> {'tick': monotonic time of the last scan, kind: monotonic time healthy since}
        self.error = None

    def _open(self, cam, kind, now, detail):
        row = self.db.execute(
            "SELECT id FROM alerts WHERE camera_id=? AND type=? AND state NOT IN ('resolved','expired')", (cam, kind)
        ).fetchone()
        if row:
            return row[0]
        id = self.db.execute(
            'INSERT INTO alerts(type,camera_id,created_at,note) VALUES (?,?,?,?)', (kind, cam, now, '')
        ).lastrowid
        self.db.execute(
            "INSERT INTO alert_log(alert_id,at,actor,action,to_state,detail) VALUES (?,?,'watchdog','created','pending',?)",
            (id, now, json.dumps(detail)),
        )
        self.db.execute(
            "INSERT INTO alert_dispatch_jobs(job_key,alert_id,escalation_level,channel,state,next_attempt_at,created_at) VALUES (?,?,0,'sse','queued',?,?)",
            (f'watchdog:{id}:sse', id, now, now),
        )
        return id

    def _recover(self, cam, kind, now):
        rows = self.db.execute(
            "SELECT id,state FROM alerts WHERE camera_id=? AND type=? AND state NOT IN ('resolved','expired')",
            (cam, kind),
        ).fetchall()
        for row in rows:
            self.db.execute(
                "UPDATE alerts SET state='resolved',resolved_at=?,resolved_by='watchdog',resolution='recovered' WHERE id=?",
                (now, row['id']),
            )
            self.db.execute(
                "INSERT INTO alert_log(alert_id,at,actor,action,from_state,to_state,detail) VALUES (?,?,'watchdog','resolved',?,'resolved',?)",
                (row['id'], now, row['state'], json.dumps({'label': 'recovered'})),
            )
            self.db.execute(
                "UPDATE alert_dispatch_jobs SET state='cancelled' WHERE alert_id=? AND state IN ('queued','processing','retry')",
                (row['id'],),
            )

    def tick(self, samples, now=None, mono=None):
        """now: wall time recorded in alerts. mono: monotonic time for timers; tests that pass `now` alone use it for both."""
        mono = (now if now is not None else time.monotonic()) if mono is None else mono
        now = time.time() if now is None else now
        result = []
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                lock_key(self.db, 'alert-log')
                for sample in samples:
                    cam = sample['camera_id']
                    key = 'watchdog:' + cam
                    saved = self.db.execute('SELECT value FROM alert_settings WHERE key=?', (key,)).fetchone()
                    state = json.loads(saved[0]) if saved else {}
                    clock = self.mono.setdefault(cam, {})
                    continuous = 'tick' in clock and 0 <= mono - clock['tick'] <= 2
                    clock['tick'] = mono
                    state['last_tick'] = now
                    offline = sample['inference_age_s'] >= self.offline_after_s
                    hardware = sample.get('hardware')
                    fresh = hardware is not None and not hardware['stale']
                    temp = hardware['metrics'].get('temperature_c') if fresh else None
                    throttle = hardware['throttle'].get('current') if fresh else None
                    has_throttle = any(throttle.values()) if isinstance(throttle, dict) else None
                    known = temp is not None and has_throttle is not None
                    hot = (temp is not None and temp >= 80) or has_throttle is True
                    missing = sample.get('hardware_age_s', sample['uptime_s']) >= self.offline_after_s and not fresh
                    warning = hot or missing
                    streams_healthy = sample.get('inference_seen', True) and sample['inference_age_s'] <= sample.get(
                        'inference_fresh_s', 3
                    )
                    healthy = fresh and known and not hot
                    detail = {
                        'video_seen': sample.get('video_seen', True),
                        'inference_seen': sample.get('inference_seen', True),
                        'video_missing': sample['video_age_s'] >= self.offline_after_s,
                        'inference_missing': sample['inference_age_s'] >= self.offline_after_s,
                        'hardware_fresh': fresh,
                        'temperature_c': temp,
                        'throttled': throttle.get('throttled') if isinstance(throttle, dict) else None,
                        'device_flags_active': has_throttle,
                        'hardware_missing': missing,
                    }
                    for kind, bad, good, hold in [
                        ('camera_offline', offline, streams_healthy, self.recover_s),
                        ('device_warning', warning, healthy, self.thermal_recover_s),
                    ]:
                        latch = state.setdefault(kind, {'latched': False, 'healthy_since': None})
                        if bad:
                            if not latch['latched']:
                                self._open(cam, kind, now, detail)
                                latch['latched'] = True
                            latch['healthy_since'] = None
                            clock[kind] = None
                        elif good:
                            if not continuous or clock.get(kind) is None:
                                clock[kind] = mono
                                latch['healthy_since'] = now
                            if mono - clock[kind] >= hold:
                                self._recover(cam, kind, now)
                                latch['latched'] = False
                        else:
                            latch['healthy_since'] = None
                            clock[kind] = None
                    self.db.execute(
                        'INSERT INTO alert_settings(key,value,updated_at,updated_by) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                        (key, json.dumps(state), now, 'watchdog'),
                    )
                    result.append(
                        {
                            'camera_id': cam,
                            **detail,
                            'video_age_s': sample['video_age_s'],
                            'inference_age_s': sample['inference_age_s'],
                            'warning': warning,
                            'offline': offline,
                        }
                    )
                self.db.commit()
                self.last_status = result
                self.error = None
            except Exception:
                self.db.rollback()
                raise
        return result
