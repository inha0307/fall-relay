#!/usr/bin/env python3
"""Isolated production bridge with passive request/clip-ready timing logs."""

import argparse, json, sys, time, threading, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'server' / 'bridge'))
from video_bridge import create_app
import uvicorn


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', required=True)
    p.add_argument('--log-dir', required=True)
    a = p.parse_args()
    out = Path(a.log_dir)
    from server_config import load_server_config

    cfg = load_server_config(a.config)
    app = create_app(cfg)
    lock = threading.Lock()

    def log(kind, data):
        with lock:
            with (out / (kind + '.jsonl')).open('a') as f:
                f.write(json.dumps({'experiment_id': out.name, **data}, allow_nan=False) + '\n')

    class Observe:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope['type'] != 'http':
                return await self.app(scope, receive, send)
            if scope.get('path') == '/benchmark/time':
                body = json.dumps({'server_time': time.time()}).encode()
                await send(
                    {'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'application/json')]}
                )
                await send({'type': 'http.response.body', 'body': body})
                return
            entered = time.time()
            raw = bytearray()
            received = None
            status = None

            async def recv():
                nonlocal received
                msg = await receive()
                if msg['type'] == 'http.request':
                    if len(raw) < 32768:
                        raw.extend(msg.get('body', b'')[: 32768 - len(raw)])
                    if not msg.get('more_body', False):
                        received = time.time()
                return msg

            async def emit(msg):
                nonlocal status
                if msg['type'] == 'http.response.start':
                    status = msg['status']
                await send(msg)

            try:
                await self.app(scope, recv, emit)
            finally:
                path = scope['path']
                if scope['method'] == 'POST':
                    try:
                        payload = json.loads(raw)
                    except (ValueError, TypeError):
                        # Clip bodies exceed the bounded observation buffer. Correlate the small
                        # identity prefix without parsing/retaining JPEG data or losing its bytes.
                        prefix = raw.decode(errors='replace')
                        payload = {
                            k: v
                            for k, v in re.findall(
                                r'"(camera_id|cam_id|run_id|model_run_id|event_uid)"\s*:\s*"([^"\\]{1,80})"', prefix
                            )
                        }
                        onset = re.search(r'"onset_sensor_ns"\s*:\s*(\d+)', prefix)
                        if onset:
                            payload['onset_sensor_ns'] = int(onset.group(1))
                    headers = dict(scope.get('headers', []))
                    length = int(headers.get(b'content-length', b'0'))
                    n = (
                        len(scope['method'])
                        + len(scope.get('raw_path', path.encode()))
                        + (len(scope['query_string']) + 1 if scope.get('query_string') else 0)
                        + 12
                        + sum(len(k) + len(v) + 4 for k, v in scope['headers'])
                        + 2
                        + length
                    )
                    log(
                        'server_requests',
                        {
                            'path': path,
                            'method': scope['method'],
                            'entered_at': entered,
                            'body_received_at': received,
                            'response_at': time.time(),
                            'status': status,
                            'http_bytes': n,
                            **{
                                k: payload.get(k)
                                for k in (
                                    'cam_id',
                                    'camera_id',
                                    'run_id',
                                    'model_run_id',
                                    'event_uid',
                                    'message_seq',
                                    'kind',
                                    'decided_at',
                                    'clock',
                                    'onset_sensor_ns',
                                )
                            },
                        },
                    )

    def monitor():
        seen = set()
        while True:
            for path in Path(cfg['clip_dir']).glob('*.webm'):
                if path.name.endswith('.tmp.webm') or path.name in seen:
                    continue
                seen.add(path.name)
                try:
                    meta = json.loads(path.with_suffix('.json').read_text())
                except (OSError, ValueError):
                    meta = {}
                log(
                    'clip_ready',
                    {
                        'observed_at': time.time(),
                        'resolution_s': 0.1,
                        'file': path.name,
                        'bytes': path.stat().st_size,
                        **meta,
                    },
                )
            time.sleep(0.1)

    threading.Thread(target=monitor, daemon=True).start()
    uvicorn.run(Observe(app), host='127.0.0.1', port=cfg['port'], access_log=False, log_level='warning')


if __name__ == '__main__':
    main()
