"""Fall-Relay server (bridge): edge results, explicit events and alerts, device telemetry and commands, optional debug video.

The Raspberry Pi owns pose estimation, classification and temporal confirmation.
This server only validates the transmitted quality contract, stores the latest
decision and skeleton per camera, stores explicit fall events and alerts, encodes the
short clip a device uploads around each detected fall (clip_recorder.py), relays a
camera JPEG stream when a device pushes one for debugging, and serves the reports API. The accepted cameras and
their push tokens come from camera_registry.py and can be changed without a restart.
It performs no inference and applies no second threshold.

Run:  python video_bridge.py --config server_config.json
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import logging
import math
import os
import hmac
import re
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

import camera_registry
from camera_registry import CameraRegistry
from clip_recorder import CLIP_SUFFIX, TMP_SUFFIX, ClipRecorder
from event_store import REVIEW_STATUSES, EventStore
from server_config import load_server_config
from hardware_store import HardwareStore, METRICS
from model_run_store import ModelRunStore
from edge_event_contract import CONTRACT as EVENT_CONTRACT, RUN as EVENT_RUN
from edge_event_store import EdgeEventStore, EventConflict
from routers.edge import edge_router
from routers.alerts import alerts_router
from alert_store import AlertStore
from bridge_read_auth import BridgeReadAuth
from watchdog import Watchdog
from device_commands import CommandError, DeviceCommandStore
from device_profiles import COMMAND_CATALOG, profile_for
import dashboard_stats
import traffic

VALID_EDGE_STATUSES = {'FALL', 'NORMAL', 'WAIT'}
EDGE_RESULT_FIELDS = (
    'event_protocol',
    'run_id',
    'contract',
    'decision_state',
    'heartbeat_seq',
    'decision_high_slots',
    'decision_valid_slots',
    'decision_overridden',
    'outbox_pending',
    'outbox_oldest_s',
    'outbox_blocked',
    'outbox_error',
    'model_identity',
    'seq',
    'score',
    'threshold',
    'consecutive_high',
    'confirm_slots',
    'input_valid',
    'window_valid_slots',
    'window_min_valid_slots',
    'window_eligible',
    'people',
    'track_id',
    'status',
    'detector_score',
    'visible_joints',
    'mean_keypoint_confidence',
    'latency_ms',
    'skeleton',
    'frame_size',
)
MAX_JPEG_BYTES = 8 * 1024 * 1024
GRID_MAX_CAMERAS = 16  # cameras multiplexed into one /video_grid connection
GRID_FPS_RANGE = (0.5, 15.0)
_STREAM_HEADERS = {
    'Cache-Control': 'no-cache, no-store, must-revalidate',
    'Pragma': 'no-cache',
    'Expires': '0',
    'X-Accel-Buffering': 'no',
}


class CutoverGate:
    """Deployment-only fence: no writes/streams before startup read checks pass."""

    reads = {
        '/api/system',
        '/api/live',
        '/api/dashboard',
        '/api/model_runs',
        '/api/cameras',
        '/api/events',
        '/api/devices',
    }

    def __init__(self, app, ready_file):
        self.app, self.ready_file = app, Path(ready_file)

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and not self.ready_file.exists():
            if scope['method'] != 'GET' or scope['path'] not in self.reads:
                response = JSONResponse(
                    {'error': 'database cutover verification in progress'},
                    status_code=503,
                    headers={'Retry-After': '2'},
                )
                return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def _rate(times: deque, now: float, window: float = 10.0) -> float:
    recent = [t for t in times if now - t <= window]
    if len(recent) < 2 or now - recent[-1] > 2.0:
        return 0.0
    return round((len(recent) - 1) / max(recent[-1] - recent[0], 1e-6), 2)


class ChannelMap:
    """Camera id -> CameraChannel, replaced copy-on-write so readers on other threads
    (housekeeping, streams) never see a dict that changes while they iterate it."""

    def __init__(self):
        self._map: dict[str, 'CameraChannel'] = {}

    def replace(self, mapping: dict) -> None:
        self._map = mapping

    def get(self, cam):
        return self._map.get(cam)

    def __getitem__(self, cam):
        return self._map[cam]

    def __contains__(self, cam) -> bool:
        return cam in self._map

    def __len__(self) -> int:
        return len(self._map)

    def __iter__(self):
        return iter(list(self._map))

    def values(self):
        return list(self._map.values())

    def items(self):
        return list(self._map.items())


class CameraChannel:
    """Latest video frame and latest Pi decision for one camera."""

    def __init__(self, camera: dict, edge_stale_after_s: float, video_stale_after_s: float):
        self.id = camera['id']
        self.label = camera['label']
        self.location = camera['location']
        self.edge_stale_after_s = edge_stale_after_s
        self.video_stale_after_s = video_stale_after_s
        self._lock = threading.Lock()
        self._frame_ready = threading.Condition(self._lock)
        self._jpeg: bytes | None = None
        self._frame_seq = 0
        self._frame_times: deque = deque(maxlen=1200)
        self._frames_total = 0
        self._last_frame_t = 0.0
        self._edge_times: deque = deque(maxlen=600)
        self._edge_total = 0
        self._last_edge_t = 0.0
        self._last_result: dict | None = None
        self._track: dict | None = None
        self._latencies: list[float] = []
        self._interval_results = 0
        self._interval_ineligible = 0
        self.stream_clients = 0
        self.push_auth: str | None = None  # how the last accepted push authenticated: 'token' / 'none'
        self.auth_rejects = 0  # pushes refused for a missing or wrong camera token
        self._bytes_in = 0  # JPEG bytes received from the Pi
        self._bytes_out = 0  # bytes sent to all viewers of this camera
        self._out_lock = threading.Lock()
        self._traffic: deque = deque(maxlen=6)  # (t, bytes_in, bytes_out), sampled by housekeeping only
        self.kbps_in = 0.0
        self.kbps_out = 0.0

    def push_frame(self, jpeg: bytes, location: str = '') -> None:
        now = time.monotonic()
        with self._frame_ready:
            self._jpeg = jpeg
            self._frame_seq += 1
            self._frames_total += 1
            self._bytes_in += len(jpeg)
            self._frame_times.append(now)
            self._last_frame_t = now
            if location:
                self.location = location
            self._frame_ready.notify_all()

    def wait_frame(self, last_seq: int, timeout: float) -> tuple[int, bytes | None]:
        with self._frame_ready:
            self._frame_ready.wait_for(lambda: self._frame_seq != last_seq, timeout=timeout)
            if time.monotonic() - self._last_frame_t > self.video_stale_after_s:
                return self._frame_seq, None
            return self._frame_seq, self._jpeg

    def latest_frame(self) -> tuple[int, bytes | None]:
        """Newest frame without waiting; None while the video is stale."""
        with self._lock:
            if not self.video_connected():
                return self._frame_seq, None
            return self._frame_seq, self._jpeg

    def video_connected(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self._last_frame_t > 0 and now - self._last_frame_t <= self.video_stale_after_s

    def set_edge_result(self, result: dict, track: dict | None) -> None:
        now = time.monotonic()
        with self._lock:
            self._last_result = result
            self._track = track
            self._edge_total += 1
            self._edge_times.append(now)
            self._last_edge_t = now
            self._interval_results += 1
            if not result.get('window_eligible'):
                self._interval_ineligible += 1
            latency = result.get('latency_ms')
            if isinstance(latency, (int, float)) and math.isfinite(latency) and len(self._latencies) < 5000:
                self._latencies.append(float(latency))

    def drain_interval(self) -> tuple[list[float], int, int]:
        """Latencies and counters accumulated since the last call (for the metrics table)."""
        with self._lock:
            out = (self._latencies, self._interval_results, self._interval_ineligible)
            self._latencies, self._interval_results, self._interval_ineligible = [], 0, 0
            return out

    def edge_connected(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self._last_edge_t > 0 and now - self._last_edge_t <= self.edge_stale_after_s

    def count_sent(self, nbytes: int) -> None:
        with self._out_lock:
            self._bytes_out += nbytes

    def sample_traffic(self) -> None:
        """Update kbps_in / kbps_out over the last ~10 s (called every 2 s by housekeeping)."""
        now = time.monotonic()
        with self._lock:
            bytes_in = self._bytes_in
        with self._out_lock:
            bytes_out = self._bytes_out
        self._traffic.append((now, bytes_in, bytes_out))
        first_t, first_in, first_out = self._traffic[0]
        if now - first_t > 0:
            self.kbps_in = round((bytes_in - first_in) * 8 / (now - first_t) / 1000, 1)
            self.kbps_out = round((bytes_out - first_out) * 8 / (now - first_t) / 1000, 1)

    def connection(self) -> dict:
        now = time.monotonic()
        with self._lock:
            edge_alive = self.edge_connected(now)
            result = dict(self._last_result) if self._last_result else None
            return {
                'camera_id': self.id,
                'label': self.label,
                'location': self.location,
                'video_path': f'/video_feed/{self.id}',
                'video_connected': self.video_connected(now),
                'video_fps': _rate(self._frame_times, now),
                'edge_inference_connected': edge_alive,
                'edge_inference_hz': _rate(self._edge_times, now),
                'edge_result_age_s': round(now - self._last_edge_t, 3) if self._last_edge_t else None,
                'fall_threshold': result.get('threshold') if result else None,
                # A stale decision is reported for diagnosis but never as live state.
                'last_edge_result': result,
                'last_edge_result_stale': bool(result) and not edge_alive,
                'push_auth': self.push_auth,
                'auth_rejects': self.auth_rejects,
                'stream_clients': self.stream_clients,
                'last_frame_age_s': round(now - self._last_frame_t, 2) if self._last_frame_t else None,
                'video_in_kbps': self.kbps_in,
                'video_out_kbps': self.kbps_out,
            }

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            tracks = [dict(self._track)] if self._track and self.edge_connected(now) else []
            return {
                'timestamp': time.time(),
                'cam_id': self.id,
                'location': self.location or self.label,
                'tracks': tracks,
                'edge_inference_connected': self.edge_connected(now),
                'performance': {
                    'received': {'fps': _rate(self._frame_times, now), 'total': self._frames_total},
                    'edge_inference': {'fps': _rate(self._edge_times, now), 'total': self._edge_total},
                },
            }


def _parse_skeleton(raw) -> list | None:
    """The edge model's 13 joints (R/L shoulder, elbow, wrist, hip, knee, ankle, head point) as [x, y, confidence]
    in stream-frame pixels, null per masked joint; None when absent or malformed."""
    if not isinstance(raw, list) or len(raw) != 13:
        return None
    out = []
    for joint in raw:
        if joint is None:
            out.append(None)
            continue
        if (
            not isinstance(joint, (list, tuple))
            or len(joint) != 3
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in joint)
        ):
            return None
        x, y, c = (float(v) for v in joint)
        if not all(math.isfinite(v) for v in (x, y, c)) or not 0.0 <= c <= 1.0 or abs(x) > 1e5 or abs(y) > 1e5:
            return None
        out.append([round(x, 1), round(y, 1), round(c, 3)])
    return out if any(out) else None


def _parse_edge_result(payload: dict) -> tuple[str, dict, dict | None]:
    """Validate a device heartbeat. Returns (displayed status, stored result, track or None)."""
    raw_score = payload.get('score')
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)) or not 0 <= raw_score <= 1:
        raise ValueError('score must be a number in [0, 1]')
    score = float(raw_score)
    track_id = int(payload.get('track_id', -1))
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError('score must be a finite value in [0, 1]')
    bbox_raw = payload.get('bbox')
    bbox = tuple(float(v) for v in bbox_raw) if isinstance(bbox_raw, (list, tuple)) and len(bbox_raw) == 4 else None
    if bbox is not None and not all(math.isfinite(v) for v in bbox):
        bbox = None
    current_valid = bool(payload.get('input_valid')) and bbox is not None
    reported_status = str(payload.get('status', 'WAIT'))
    if reported_status not in VALID_EDGE_STATUSES:
        raise ValueError('invalid edge status')
    eligible = current_valid and bool(payload.get('window_eligible', False))
    state = payload.get('decision_state')
    if state not in ('IDLE', 'SUSPECT', 'ALERT', 'POST_FALL', 'COOLDOWN') or payload.get('contract') != EVENT_CONTRACT:
        raise ValueError('invalid explicit event heartbeat')
    expected_active = state in ('ALERT', 'POST_FALL')
    if (reported_status == 'FALL') != expected_active:
        raise ValueError('heartbeat state and status disagree')
    run = payload.get('run_id')
    if not isinstance(run, str) or not EVENT_RUN.fullmatch(run):
        raise ValueError('invalid event run_id')
    for key in ('heartbeat_seq', 'outbox_pending', 'outbox_blocked', 'decision_high_slots', 'decision_valid_slots'):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f'invalid {key}')
    if (
        payload['heartbeat_seq'] < 1
        or not 0 <= payload['decision_high_slots'] <= payload['decision_valid_slots'] <= 300
    ):
        raise ValueError('invalid decision observation counts')
    age = payload.get('outbox_oldest_s')
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or age < 0:
        raise ValueError('invalid outbox age')
    if not isinstance(payload.get('decision_overridden'), bool):
        raise ValueError('invalid decision override flag')
    if payload.get('outbox_error') is not None and (
        not isinstance(payload['outbox_error'], str) or len(payload['outbox_error']) > 200
    ):
        raise ValueError('invalid outbox error')
    status = reported_status if eligible or reported_status == 'FALL' else 'WAIT'
    result = {key: payload.get(key) for key in EDGE_RESULT_FIELDS}
    result['status'] = status
    result['window_eligible'] = eligible
    result['skeleton'] = _parse_skeleton(payload.get('skeleton'))
    size = payload.get('frame_size')
    result['frame_size'] = (
        list(size)
        if isinstance(size, (list, tuple))
        and len(size) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 10000 for v in size)
        else None
    )
    track = None
    if current_valid and track_id >= 0:
        track = {
            'track_id': track_id,
            'fall_status': status,
            'fall_score': round(score, 3),
            'bbox': list(bbox),
            'consecutive_high': payload.get('consecutive_high'),
            'confirm_slots': payload.get('confirm_slots'),
            'skeleton': result['skeleton'],
        }
    return status, result, track


MEDIA_PREFIXES = ('/video_feed', '/video_grid', '/clips/')


def media_token_valid(token: str, secret: str, now: float | None = None) -> bool:
    """Token '<expiry epoch>.<hex HMAC-SHA256(secret, "media:<expiry>")>' issued by the web server
    (app/api/media-token) to signed-in users; the browser appends it as ?t= to video and clip URLs."""
    try:
        expiry_text, signature = token.split('.', 1)
        expiry = int(expiry_text)
    except (AttributeError, ValueError):
        return False
    if expiry < (time.time() if now is None else now):
        return False
    expected = hmac.new(secret.encode(), f'media:{expiry}'.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


class MediaTokenMiddleware:
    """Pure ASGI check (safe for endless MJPEG streams): video and clips need a valid ?t= token."""

    def __init__(self, app, secret: str):
        self.app = app
        self.secret = secret

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and scope['path'].startswith(MEDIA_PREFIXES) and scope['method'] != 'OPTIONS':
            from urllib.parse import parse_qs

            token = (parse_qs(scope.get('query_string', b'').decode()).get('t') or [''])[0]
            if not self.secret or not media_token_valid(token, self.secret):
                body = json.dumps(
                    {'ok': False, 'error': 'media token required (sign in to the web application)'}
                ).encode()
                await send(
                    {
                        'type': 'http.response.start',
                        'status': 401,
                        'headers': [
                            (b'content-type', b'application/json'),
                            (b'content-length', str(len(body)).encode()),
                        ],
                    }
                )
                await send({'type': 'http.response.body', 'body': body})
                return
        await self.app(scope, receive, send)


def create_app(config: dict) -> FastAPI:
    features = config.get('features', {})
    alerts_enabled = features.get('alerts', False)
    watchdog_enabled = features.get('watchdog', False)
    if watchdog_enabled and not alerts_enabled:
        raise ValueError('watchdog requires alerts')
    if alerts_enabled and (
        not config.get('require_camera_tokens')
        or not config.get('require_media_token')
        or not os.environ.get(config['write_token_env'])
    ):
        raise ValueError('alerts requires camera tokens, media authentication and a server write token')
    database_url = os.environ.get(config['database_url_env'])
    if not database_url:
        raise ValueError(f"set {config['database_url_env']} to the PostgreSQL connection string")
    store = EventStore(database_url, config['metrics_retention_days'])
    hardware = HardwareStore(
        store._db, config.get('hardware_retention_days', 30), config.get('hardware_stale_after_s', 20)
    )
    registry = CameraRegistry(
        config['cameras'], config.get('require_camera_tokens', False), connection=store._db, lock=store._lock
    )
    model_runs = ModelRunStore(store)
    commands = DeviceCommandStore(store)
    from decision_log_store import DecisionLogStore, decision_log_router

    log_archives = DecisionLogStore(store, config['archive_dir'])
    edge_events = EdgeEventStore(store, config.get('edge_event_max_live_age_s', 120), delivery_enabled=alerts_enabled)
    channels = ChannelMap()
    meter = traffic.TrafficMeter()

    def sync_channels() -> None:
        """Follow the registry: keep existing channels (and their state), add new, drop disabled."""
        current = dict(channels.items())
        mapping = {}
        for camera in registry.enabled():
            channel = current.get(camera['id'])
            if channel is None:
                channel = CameraChannel(camera, config['edge_stale_after_s'], config['video_stale_after_s'])
            else:
                channel.label = camera['label']
                channel.location = camera['location'] or channel.location
            mapping[camera['id']] = channel
        channels.replace(mapping)

    sync_channels()

    def default_cam() -> str | None:
        return next(iter(channels), None)

    write_token = os.environ.get(config['write_token_env']) or ''
    clip_dir = config.get('clip_dir')
    recorder = (
        ClipRecorder(
            clip_dir,
            config.get('clip_pre_s', 6.0),
            config.get('clip_post_s', 4.0),
            config.get('clip_fps', 10.0),
            config.get('clip_retention_days', 30),
        )
        if clip_dir
        else None
    )
    stop = threading.Event()
    started = time.time()
    watchdog = Watchdog(store) if watchdog_enabled else None
    # Bridge process figures for /api/system, refreshed by housekeeping.
    sysinfo = {'cpu_pct': None, 'clip_count': None, 'clip_bytes': None}
    cpu_prev = [time.monotonic(), sum(os.times()[:2])]

    def sample_system(scan_clips: bool) -> None:
        now, cpu = time.monotonic(), sum(os.times()[:2])
        if now > cpu_prev[0]:
            sysinfo['cpu_pct'] = round((cpu - cpu_prev[1]) / (now - cpu_prev[0]) * 100, 1)
        cpu_prev[:] = [now, cpu]
        if scan_clips and clip_dir and os.path.isdir(clip_dir):
            count = size = 0
            for entry in os.scandir(clip_dir):
                if entry.is_file() and entry.name.endswith(('.webm', '.mp4')):
                    count += 1
                    size += entry.stat().st_size
            sysinfo['clip_count'], sysinfo['clip_bytes'] = count, size

    def housekeeping() -> None:
        gate = os.environ.get('FALLRELAY_INGRESS_READY_FILE')
        while gate and not Path(gate).exists():
            if stop.wait(0.2):
                return
        next_log_prune = time.monotonic()
        next_metrics = time.monotonic() + config['metrics_interval_s']
        next_prune = time.monotonic()
        next_traffic = time.monotonic()
        next_clip_scan = time.monotonic()
        while not stop.wait(0.5):
            if time.monotonic() >= next_log_prune:
                next_log_prune = time.monotonic() + 3600
                try:
                    log_archives.prune()
                except OSError:
                    logging.exception('Decision log archive retention failed')
            if watchdog:
                wall = time.time()
                mono = time.monotonic()
                devices = hardware.devices(wall)
                by_camera = {}
                for device in devices:
                    old = by_camera.get(device['camera_id'])
                    if old is None or device['received_at'] > old['received_at']:
                        by_camera[device['camera_id']] = device
                samples = []
                for cam, channel in channels.items():
                    device = by_camera.get(cam)
                    samples.append(
                        {
                            'camera_id': cam,
                            'uptime_s': wall - started,
                            'video_seen': bool(channel._last_frame_t),
                            'inference_seen': bool(channel._last_edge_t),
                            'video_fresh_s': channel.video_stale_after_s,
                            'inference_fresh_s': channel.edge_stale_after_s,
                            'video_age_s': max(0, mono - channel._last_frame_t)
                            if channel._last_frame_t
                            else max(0, wall - started),
                            'inference_age_s': max(0, mono - channel._last_edge_t)
                            if channel._last_edge_t
                            else max(0, wall - started),
                            'hardware_age_s': device['age_s'] if device else max(0, wall - started),
                            'hardware': device,
                        }
                    )
                try:
                    watchdog.tick(samples, wall, mono)
                except Exception as exc:
                    watchdog.error = type(exc).__name__
                    print('[watchdog] scan failed: ' + watchdog.error, flush=True)
            edge_events.schedule_clips(recorder, {cam: channel.label for cam, channel in channels.items()})
            hardware.prune()
            if recorder and time.monotonic() >= next_prune:
                next_prune += 3600
                recorder.prune()
            if time.monotonic() >= next_traffic:
                next_traffic = time.monotonic() + 2.0
                for channel in channels.values():
                    channel.sample_traffic()
                scan = time.monotonic() >= next_clip_scan
                if scan:
                    next_clip_scan = time.monotonic() + 60.0
                sample_system(scan)
            if time.monotonic() >= next_metrics:
                next_metrics += config['metrics_interval_s']
                wall = time.time()
                for channel in channels.values():
                    info = channel.connection()
                    latencies, results, ineligible = channel.drain_interval()
                    store.record_metrics(
                        channel.id,
                        wall,
                        info['video_connected'],
                        info['edge_inference_connected'],
                        info['video_fps'],
                        info['edge_inference_hz'],
                        latencies,
                        results,
                        ineligible,
                    )
                traffic.record(
                    store._db,
                    store._lock,
                    meter.drain(),
                    wall,
                    config['metrics_interval_s'],
                    store.metrics_retention_days,
                )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        def keep_housekeeping():
            while not stop.is_set():
                try:
                    housekeeping()
                    return
                except Exception:
                    logging.exception('Bridge housekeeping interrupted; retrying')
                    if stop.wait(2):
                        return

        worker = threading.Thread(target=keep_housekeeping, name='bridge-housekeeping', daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=2)
            if recorder:
                recorder.close()
            store.close()
            registry.close()
            hardware.close()

    app = FastAPI(title='Fall-Relay server', lifespan=lifespan)
    if os.environ.get('FALLRELAY_INGRESS_READY_FILE'):
        app.add_middleware(CutoverGate, ready_file=os.environ.get('FALLRELAY_INGRESS_READY_FILE'))
    app.state.channels = channels
    app.state.store = store
    app.state.schema_versions = store.schema_versions
    app.state.features = features
    app.state.registry = registry
    app.state.recorder = recorder
    app.state.hardware = hardware
    app.state.model_runs = model_runs
    app.state.commands = commands
    app.state.traffic = meter
    app.state.edge_events = edge_events
    app.state.watchdog = watchdog
    # Reads go through the authenticated web server whenever a server token exists (alerts require one).
    if alerts_enabled or write_token:
        app.add_middleware(BridgeReadAuth, secret=write_token)
    if config.get('require_media_token'):
        if not write_token:
            print(
                '[bridge] require_media_token is on but the write token is empty: all video and clips are refused',
                flush=True,
            )
        app.add_middleware(MediaTokenMiddleware, secret=write_token)
    if config.get('cors_origins'):
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config['cors_origins'],
            allow_methods=['GET', 'POST', 'PATCH', 'DELETE'],
            allow_headers=['*'],
        )

    def unknown(cam: str) -> JSONResponse:
        return JSONResponse({'ok': False, 'error': f'unknown camera: {cam}'}, status_code=404)

    def authorize_push(channel: CameraChannel, request: Request) -> JSONResponse | None:
        """Camera token check for Pi pushes; None when the push may proceed."""
        result = registry.verify(channel.id, request.headers.get('x-camera-token'))
        if result in camera_registry.REJECTED:
            channel.auth_rejects += 1
            return JSONResponse({'ok': False, 'error': f'camera token {result}'}, status_code=403)
        channel.push_auth = result
        meter.count(channel.id, request)
        return None

    def authorize_event(cam, request):
        channel = channels.get(cam)
        if channel is None:
            return unknown(cam)
        refused = authorize_push(channel, request)
        if refused is not None:
            return refused
        if channel.push_auth != camera_registry.AUTH_OK:
            return JSONResponse(
                {'ok': False, 'error': 'explicit events require a configured camera token'}, status_code=403
            )
        return None

    app.include_router(
        edge_router(
            edge_events,
            authorize_event,
            lambda: edge_events.schedule_clips(recorder, {cam: channel.label for cam, channel in channels.items()}),
            recorder,
            {cam: channel.label for cam, channel in channels.items()},
        )
    )

    def write_authorized(request: Request) -> bool:
        supplied = request.headers.get('x-write-token', '')
        return bool(write_token) and hmac.compare_digest(supplied, write_token)

    app.include_router(
        alerts_router(AlertStore(store), write_authorized, alerts_enabled, channels, recorder, clip_dir, watchdog)
    )

    def write_refused() -> JSONResponse:
        return JSONResponse({'ok': False, 'error': 'write token required'}, status_code=403)

    @app.post('/push_frame')
    async def push_frame(request: Request, cam: str | None = Query(default=None), location: str = Query(default='')):
        cam = cam or default_cam()
        channel = channels.get(cam)
        if channel is None:
            return JSONResponse({'ok': False, 'error': f'unknown camera: {cam}'}, status_code=400)
        refused = authorize_push(channel, request)
        if refused:
            return refused
        body = await request.body()
        if not body or len(body) > MAX_JPEG_BYTES or not body.startswith(b'\xff\xd8'):
            return JSONResponse({'ok': False, 'error': 'expected a JPEG body'}, status_code=400)
        channel.push_frame(body, location)
        if recorder:
            recorder.add_frame(cam, body)
        return {'ok': True}

    def mjpeg(channel: CameraChannel, fps: float | None = None):
        channel.stream_clients += 1
        try:
            seq, last_sent = -1, 0.0
            min_interval = 1.0 / fps if fps else 0.0
            while True:
                seq, frame = channel.wait_frame(seq, timeout=1.0)
                if frame is None:
                    continue
                now = time.monotonic()
                if now - last_sent < min_interval:  # optional rate limit: drop frames, no re-encoding
                    continue
                last_sent = now
                part = b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
                channel.count_sent(len(part))
                yield part
        finally:
            channel.stream_clients -= 1

    def stream_fps(fps: float | None) -> float | None:
        return None if fps is None else max(GRID_FPS_RANGE[0], min(float(fps), GRID_FPS_RANGE[1]))

    @app.get('/video_feed')
    def video_feed_default():
        channel = channels.get(default_cam())
        if channel is None:
            return unknown('default')
        return StreamingResponse(
            mjpeg(channel), headers=_STREAM_HEADERS, media_type='multipart/x-mixed-replace; boundary=frame'
        )

    @app.get('/video_feed/{cam}')
    def video_feed(cam: str, fps: float | None = None):
        channel = channels.get(cam)
        if channel is None:
            return unknown(cam)
        return StreamingResponse(
            mjpeg(channel, stream_fps(fps)),
            headers=_STREAM_HEADERS,
            media_type='multipart/x-mixed-replace; boundary=frame',
        )

    @app.get('/video_grid')
    async def video_grid(cams: str, fps: float = 5.0):
        """Several cameras in ONE connection (browsers allow ~6 per host over HTTP/1.1).

        Each part carries X-Camera and Content-Length headers; only frames that changed since
        the last tick are sent, at most `fps` per camera. Runs on the event loop, not a thread.
        """
        ids = [c for c in cams.split(',') if c]
        if not ids or len(ids) > GRID_MAX_CAMERAS or len(set(ids)) != len(ids):
            return JSONResponse(
                {'ok': False, 'error': f'cams must list 1-{GRID_MAX_CAMERAS} distinct cameras'}, status_code=400
            )
        missing = [c for c in ids if c not in channels]
        if missing:
            return unknown(missing[0])
        selected = [channels[c] for c in ids]
        interval = 1.0 / stream_fps(fps)

        async def frames():
            sent = {channel.id: -1 for channel in selected}
            for channel in selected:
                channel.stream_clients += 1
            try:
                next_t = time.monotonic()
                while True:
                    for channel in selected:
                        seq, jpeg = channel.latest_frame()
                        if jpeg is not None and seq != sent[channel.id]:
                            sent[channel.id] = seq
                            part = (
                                b'--frame\r\nContent-Type: image/jpeg\r\nX-Camera: '
                                + channel.id.encode()
                                + b'\r\nContent-Length: '
                                + str(len(jpeg)).encode()
                                + b'\r\n\r\n'
                                + jpeg
                                + b'\r\n'
                            )
                            channel.count_sent(len(part))
                            yield part
                    next_t = max(next_t + interval, time.monotonic())
                    await asyncio.sleep(next_t - time.monotonic())
            finally:
                for channel in selected:
                    channel.stream_clients -= 1

        return StreamingResponse(
            frames(), headers=_STREAM_HEADERS, media_type='multipart/x-mixed-replace; boundary=frame'
        )

    @app.post('/api/edge_result')
    def edge_result(payload: dict, request: Request):
        cam = str(payload.get('cam_id') or default_cam())
        channel = channels.get(cam)
        if channel is None:
            return JSONResponse({'ok': False, 'error': 'unknown camera'}, status_code=400)
        refused = authorize_push(channel, request)
        if refused:
            return refused
        if payload.get('event_protocol') != 'edge_event':
            return JSONResponse(
                {'ok': False, 'error': 'heartbeats must declare event_protocol edge_event'}, status_code=409
            )
        refused = authorize_event(cam, request)
        if refused is not None:
            return refused
        try:
            reference = payload.get('model_run_id')
            if reference is not None:
                if not isinstance(reference, str) or not EVENT_RUN.fullmatch(reference):
                    raise ValueError('invalid model_run_id')
                declared = payload.get('model_identity')
                if declared is not None:
                    if not isinstance(declared, dict) or declared.get('run_id') != reference:
                        raise ValueError('model run reference mismatch')
                else:
                    declared = model_runs.resolve(cam, reference)
                    if declared is None:
                        return JSONResponse(
                            {'ok': False, 'code': 'model_identity_required', 'error': 'register the full run identity'},
                            status_code=409,
                        )
                payload = {**payload, 'model_identity': declared}
            status, result, track = _parse_edge_result(payload)
            if result.get('model_identity') is not None:
                from model_contract import validate_identity

                settings = validate_identity(result['model_identity'])['settings']
                expected_settings = dict(settings)
                declaration = result['model_identity'].get('event_decision')
                if not declaration:
                    raise ValueError('run identity lacks event decision provenance')
                if payload.get('decision_overridden') != declaration['overridden']:
                    raise ValueError('decision override declaration mismatch')
                expected_settings.update(
                    fall_threshold=declaration['effective']['theta'],
                    fall_confirm_slots=declaration['effective']['k'],
                )
                if result['model_identity']['run_id'] != payload.get('run_id'):
                    raise ValueError('heartbeat run identity mismatch')
                for key, config_key in (
                    ('threshold', 'fall_threshold'),
                    ('confirm_slots', 'fall_confirm_slots'),
                    ('window_min_valid_slots', 'min_valid_pose_slots'),
                ):
                    if payload.get(key) != expected_settings[config_key]:
                        raise ValueError(f'{key} disagrees with immutable run settings')
                valid_slots = payload.get('window_valid_slots')
                if (
                    isinstance(valid_slots, bool)
                    or not isinstance(valid_slots, int)
                    or not 0 <= valid_slots <= settings['window_slots']
                ):
                    raise ValueError('invalid window_valid_slots')
                if result['window_eligible'] and valid_slots < settings['min_valid_pose_slots']:
                    raise ValueError('eligible window has too few valid pose slots')
            model_run_id = (
                model_runs.observe(cam, result['model_identity']) if result.get('model_identity') is not None else None
            )
            run = model_run_id or payload.get('run_id')
            if run:
                if not isinstance(run, str) or not EVENT_RUN.fullmatch(run):
                    raise ValueError('invalid heartbeat run_id')
                edge_events.heartbeat(cam, run)
        except EventConflict as exc:
            return JSONResponse({'ok': False, 'error': str(exc), 'code': exc.code}, status_code=409)
        except (TypeError, ValueError) as exc:
            return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)
        channel.set_edge_result(result, track)
        return {'ok': True, 'cam_id': cam, 'fall_status': status, 'model_run_id': model_run_id}

    @app.post('/api/device_health')
    async def device_health(request: Request):
        raw = await request.body()
        if len(raw) > 32768:
            return JSONResponse({'error': 'telemetry too large'}, status_code=413)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError('telemetry must be an object')
            channel = channels.get(str(payload.get('camera_id', '')))
            if channel is None:
                return JSONResponse({'error': 'unknown camera'}, status_code=400)
            refused = authorize_push(channel, request)
            if refused:
                return refused
            recorded = await run_in_threadpool(hardware.record, payload)
        except (ValueError, TypeError, OverflowError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        reply = {'ok': True, 'recorded': recorded}
        if payload.get('control'):  # only agents that accept commands are handed any
            reply['commands'] = await run_in_threadpool(commands.take, payload['device_id'])
        return reply

    def device_of(device_id: str) -> dict | None:
        return next((d for d in hardware.devices() if d['device_id'] == device_id), None)

    def command_error(exc: CommandError) -> JSONResponse:
        return JSONResponse({'ok': False, 'error': str(exc), 'code': exc.code}, status_code=exc.status)

    @app.post('/api/device_commands/{command_id}/result')
    async def device_command_result(command_id: int, request: Request):
        raw = await request.body()
        if len(raw) > 262144:
            return JSONResponse({'error': 'result too large'}, status_code=413)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError('result object required')
        except ValueError:
            return JSONResponse({'error': 'invalid result JSON'}, status_code=422)
        device = await run_in_threadpool(device_of, str(body.get('device_id', '')))
        channel = channels.get(device['camera_id']) if device else None
        if channel is None:
            return JSONResponse({'error': 'unknown device'}, status_code=404)
        refused = authorize_push(channel, request)  # the device's own camera token
        if refused:
            return refused
        try:
            return {
                'ok': True,
                'command': await run_in_threadpool(
                    commands.complete, command_id, device['device_id'], body.get('ok'), body.get('output', '')
                ),
            }
        except CommandError as exc:
            return command_error(exc)

    @app.get('/api/devices/{device_id}/commands')
    def device_command_list(device_id: str):
        return {'commands': commands.listing(device_id)}

    @app.post('/api/devices/{device_id}/commands')
    async def device_command_request(device_id: str, request: Request):
        # Administrators only: the web server checks the role, adds the write token and the actor's user code.
        if not write_authorized(request):
            return write_refused()
        actor = request.headers.get('x-actor', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', actor):
            return JSONResponse({'ok': False, 'error': 'trusted actor required'}, status_code=403)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({'ok': False, 'error': 'invalid JSON'}, status_code=422)
        device = await run_in_threadpool(device_of, device_id)
        if device is None or not isinstance(body, dict):
            return JSONResponse({'ok': False, 'error': 'unknown device'}, status_code=404)
        open_event = await run_in_threadpool(store.has_open_event, device['camera_id'])
        try:
            row = await run_in_threadpool(
                commands.request,
                device_id,
                device['camera_id'],
                body.get('command'),
                actor,
                device.get('control', {}).get('commands', []) if not device['stale'] else [],
                open_event,
                body.get('confirm_open_event') is True,
                params=body.get('params'),
            )
        except CommandError as exc:
            if exc.code == 'not_allowed' and device['stale']:
                exc = CommandError(409, 'the device is not reporting; it cannot pick up commands now', 'offline')
            return command_error(exc)
        return {'ok': True, 'command': row}

    app.include_router(
        decision_log_router(log_archives, authorize_push, channels, device_of, write_authorized, write_refused)
    )

    def described(device):
        # What the web needs to draw a device without assuming its kind (device_profiles.py).
        return {
            **device,
            'camera_ids': [device['camera_id']],
            'profile': profile_for(device, config.get('device_profiles', {})),
        }

    @app.get('/api/devices')
    def devices():
        return {
            'devices': [described(d) for d in hardware.devices()],
            'retention_days': hardware.retention_days,
            'command_catalog': COMMAND_CATALOG,
        }

    @app.get('/api/devices/{device_id}')
    def device_detail(device_id: str):
        device = next((d for d in hardware.devices() if d['device_id'] == device_id), None)
        if device is None:
            return JSONResponse({'error': 'unknown device'}, status_code=404)
        return {
            'device': described(device),
            'retention_days': hardware.retention_days,
            'command_catalog': COMMAND_CATALOG,
        }

    @app.get('/api/model_runs')
    def model_run_history(limit: int = Query(default=20, ge=1, le=100)):
        return {
            'runs': model_runs.listing(limit),
            'verification': 'authenticated edge reports; server checks identity consistency, not remote model attestation',
        }

    @app.get('/api/devices/{device_id}/metrics')
    def device_metrics(device_id: str, hours: float = Query(default=1, ge=0.01, le=168), format: str = 'json'):
        if format not in ('json', 'csv'):
            return JSONResponse({'error': 'format must be json or csv'}, status_code=400)
        rows, truncated = hardware.history(device_id, hours)
        if format == 'json':
            return {'device_id': device_id, 'samples': rows, 'truncated': truncated}
        columns = ['device_id', 'camera_id', 'measured_at', 'received_at', *METRICS, 'collection_ms', 'throttle_raw']
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{k: row[k] for k in ('device_id', 'camera_id', 'measured_at', 'received_at', 'collection_ms')},
                    **row['metrics'],
                    'throttle_raw': row['throttle']['raw'],
                }
            )
        return Response(
            output.getvalue(),
            media_type='text/csv',
            headers={
                'Content-Disposition': 'attachment; filename="device_metrics.csv"',
                'X-Results-Truncated': str(truncated).lower(),
            },
        )

    @app.get('/api/cameras')
    def api_cameras(include_disabled: bool = False):
        cameras = []
        for channel in channels.values():
            info = channel.connection()
            cameras.append(
                {
                    'id': channel.id,
                    'label': channel.label,
                    'location': info['location'],
                    'stream_url': f'/video_feed/{channel.id}',
                    'status_url': f'/api/status/{channel.id}',
                    'video_connected': info['video_connected'],
                    'edge_inference_connected': info['edge_inference_connected'],
                    'stream_clients': channel.stream_clients,
                    'enabled': True,
                }
            )
        if include_disabled:
            # Names of disabled cameras are still needed to label their stored events.
            cameras += [
                {
                    'id': c['id'],
                    'label': c['label'],
                    'location': c['location'],
                    'enabled': False,
                    'video_connected': False,
                    'edge_inference_connected': False,
                    'stream_clients': 0,
                }
                for c in registry.list()
                if not c['enabled']
            ]
        return {'active': sum(1 for c in cameras if c['video_connected']), 'cameras': cameras}

    @app.get('/api/system')
    def api_system():
        """Bridge process health: the load test showed this single process's CPU is the limit."""
        rss = threads = None
        try:
            for line in Path('/proc/self/status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    rss = int(line.split()[1]) * 1024
                elif line.startswith('Threads:'):
                    threads = int(line.split()[1])
        except OSError:
            pass
        db_bytes = store._db.execute('SELECT pg_database_size(current_database())').fetchone()[0]
        live = channels.values()
        return {
            'uptime_s': round(time.time() - started),
            'cpu_pct': sysinfo['cpu_pct'],
            'rss_bytes': rss,
            'threads': threads,
            'db_bytes': db_bytes,
            'database_backend': 'postgresql',
            'clip_count': sysinfo['clip_count'],
            'clip_bytes': sysinfo['clip_bytes'],
            'clip_retention_days': config.get('clip_retention_days', 30) if clip_dir else None,
            'stream_clients': sum(c.stream_clients for c in live),
            'video_in_kbps': round(sum(c.kbps_in for c in live), 1),
            'video_out_kbps': round(sum(c.kbps_out for c in live), 1),
            'camera_count': len(live),
        }

    @app.get('/api/live')
    async def api_live():
        """Everything /monitor needs for all cameras in one poll (tracks + connection state)."""
        cameras = []
        for channel in channels.values():
            info = channel.connection()
            cameras.append(
                {
                    'id': channel.id,
                    'label': channel.label,
                    'location': info['location'],
                    'video_connected': info['video_connected'],
                    'edge_inference_connected': info['edge_inference_connected'],
                    'tracks': channel.status()['tracks'],
                    'connection': info,
                }
            )
        return {
            'timestamp': time.time(),
            'cameras': cameras,
            'camera_count': len(cameras),
            'video_connected_count': sum(1 for c in cameras if c['video_connected']),
            'edge_connected_count': sum(1 for c in cameras if c['edge_inference_connected']),
        }

    @app.get('/api/status')
    def api_status_default():
        channel = channels.get(default_cam())
        return unknown('default') if channel is None else channel.status()

    @app.get('/api/status/{cam}')
    def api_status(cam: str):
        channel = channels.get(cam)
        return unknown(cam) if channel is None else channel.status()

    @app.get('/api/connection')
    def api_connection():
        per_camera = {cam: channel.connection() for cam, channel in channels.items()}
        default = default_cam()
        summary = {
            'mode': 'camera_video_edge_inference',
            'default_camera_id': default,
            'cameras': per_camera,
            'edge_connected_count': sum(1 for c in per_camera.values() if c['edge_inference_connected']),
            'camera_count': len(per_camera),
        }
        # Backward-compatible top-level fields describe the default camera.
        if default is not None:
            summary.update(
                {
                    k: per_camera[default][k]
                    for k in (
                        'camera_id',
                        'video_path',
                        'edge_inference_connected',
                        'edge_inference_hz',
                        'fall_threshold',
                        'last_edge_result',
                    )
                }
            )
        return summary

    @app.get('/api/connection/{cam}')
    def api_connection_cam(cam: str):
        channel = channels.get(cam)
        return unknown(cam) if channel is None else channel.connection()

    def admin_view(camera: dict) -> dict:
        channel = channels.get(camera['id'])
        info = channel.connection() if channel else {}
        return {
            **camera,
            'video_connected': info.get('video_connected', False),
            'edge_inference_connected': info.get('edge_inference_connected', False),
            'push_auth': info.get('push_auth'),
            'auth_rejects': info.get('auth_rejects', 0),
        }

    async def json_object(request: Request) -> dict | None:
        try:
            body = await request.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    @app.get('/api/admin/cameras')
    def admin_cameras(request: Request):
        if not write_authorized(request):
            return write_refused()
        return {'cameras': [admin_view(c) for c in registry.list()], 'require_tokens': registry.require_tokens}

    @app.post('/api/admin/cameras')
    async def admin_create_camera(request: Request):
        if not write_authorized(request):
            return write_refused()
        body = await json_object(request)
        if body is None:
            return JSONResponse({'ok': False, 'error': 'expected a JSON object'}, status_code=400)
        try:
            camera = await run_in_threadpool(
                registry.create, str(body.get('id', '')), body.get('label', ''), body.get('location', '')
            )
        except KeyError as exc:
            return JSONResponse({'ok': False, 'error': str(exc.args[0])}, status_code=409)
        except ValueError as exc:
            return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)
        sync_channels()
        return JSONResponse({'ok': True, 'camera': admin_view(camera)}, status_code=201)

    @app.patch('/api/admin/cameras/{cam}')
    async def admin_update_camera(cam: str, request: Request):
        if not write_authorized(request):
            return write_refused()
        body = await json_object(request)
        if body is None:
            return JSONResponse({'ok': False, 'error': 'expected a JSON object'}, status_code=400)
        try:
            camera = await run_in_threadpool(registry.update, cam, body)
        except ValueError as exc:
            return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)
        if camera is None:
            return unknown(cam)
        sync_channels()
        return {'ok': True, 'camera': admin_view(camera)}

    @app.post('/api/admin/cameras/{cam}/token')
    def admin_issue_token(cam: str, request: Request):
        """New push token for a camera. The plain value is returned only in this response."""
        if not write_authorized(request):
            return write_refused()
        token = registry.issue_token(cam)
        if token is None:
            return unknown(cam)
        channel = channels.get(cam)
        if channel:
            channel.auth_rejects = 0
        return {'ok': True, 'camera_id': cam, 'token': token, 'header': 'X-Camera-Token'}

    @app.delete('/api/admin/cameras/{cam}/token')
    def admin_clear_token(cam: str, request: Request):
        if not write_authorized(request):
            return write_refused()
        if not registry.clear_token(cam):
            return unknown(cam)
        return {'ok': True, 'camera_id': cam}

    @app.get('/api/events')
    def api_events(
        since: float | None = None,
        until: float | None = None,
        camera: str | None = None,
        review_status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        if review_status is not None and review_status not in (*REVIEW_STATUSES, 'false_alarm'):
            return JSONResponse({'ok': False, 'error': 'invalid review_status'}, status_code=400)
        listing = store.list_events(since, until, camera, review_status, limit, offset)
        for event in listing['events']:
            # 'recording' while the post-fall frames are still being collected or encoded.
            status = recorder.clip_status(event['id']) if recorder else None
            name = recorder.clip_name(event['id']) if status == 'ready' else None
            event['clip_status'] = status
            event['clip_url'] = f'/clips/{name}' if name else None
        return listing

    @app.get('/api/dashboard')
    def api_dashboard(days: float = Query(default=7, ge=0.04, le=90)):
        until, cameras, interval = (
            time.time(),
            [c['id'] for c in registry.enabled()],
            float(config['metrics_interval_s']),
        )
        stats = dashboard_stats.summary(store._db, store._lock, cameras, until - days * 86400, until, interval)
        stats['traffic'] = traffic.summary(store._db, store._lock, until - days * 86400, until, cameras, interval)
        stats['network'] = traffic.device_network(hardware.db, hardware.lock, until - days * 86400, until)
        return stats

    @app.get('/api/metrics')
    def api_metrics(hours: float = 1.0, camera: str | None = None):
        hours = max(0.01, min(float(hours), 24 * 30))
        return {'metrics': store.metrics(time.time() - hours * 3600, camera)}

    @app.get('/api/clips')
    def list_clips():
        state = {
            'recording_enabled': bool(recorder and recorder.enabled),
            'recording_error': recorder.error if recorder else 'clip_dir is not configured',
        }
        if not clip_dir or not os.path.isdir(clip_dir):
            return {'clips': [], **state}
        clips = []
        for name in sorted(os.listdir(clip_dir), reverse=True):
            if not name.endswith(('.mp4', CLIP_SUFFIX)) or name.endswith(('.tmp.mp4', TMP_SUFFIX)):
                continue
            meta = {}
            sidecar = os.path.join(clip_dir, os.path.splitext(name)[0] + '.json')
            if os.path.isfile(sidecar):
                try:
                    meta = json.loads(Path(sidecar).read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    meta = {}
            clips.append(
                {
                    'filename': name,
                    'video_url': f'/clips/{name}',
                    'fall_time': meta.get('fall_time'),
                    'track_id': meta.get('track_id'),
                    'camera_no': meta.get('camera_no'),
                    'event_id': meta.get('event_id'),
                    'camera_label': meta.get('camera_label'),
                    'size_bytes': os.path.getsize(os.path.join(clip_dir, name)),
                }
            )
        return {'clips': clips, **state}

    if clip_dir and os.path.isdir(clip_dir):
        from fastapi.staticfiles import StaticFiles

        app.mount('/clips', StaticFiles(directory=clip_dir), name='clips')

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--config',
        type=Path,
        default=Path(os.environ.get('FALLRELAY_SERVER_CONFIG', Path(__file__).resolve().parent / 'server_config.json')),
    )
    args = parser.parse_args()
    config = load_server_config(args.config)
    import uvicorn

    if config['host'] not in ('127.0.0.1', 'localhost', '::1') and not os.environ.get(config['write_token_env']):
        raise SystemExit(
            f"refusing to listen on {config['host']} without a server token: set {config['write_token_env']} (deploy.env)"
        )

    print(
        f"[bridge] cameras={[c['id'] for c in config['cameras']]} listen={config['host']}:{config['port']}", flush=True
    )
    # Open MJPEG/grid streams never end on their own; without a limit a restart waits for every viewer.
    uvicorn.run(
        create_app(config), host=config['host'], port=config['port'], access_log=False, timeout_graceful_shutdown=3
    )


if __name__ == '__main__':
    main()
